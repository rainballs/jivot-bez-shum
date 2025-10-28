# shop/econt_views.py
from django.contrib import messages
from django.conf import settings
from django.shortcuts import redirect, render
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.views.decorators.http import require_http_methods, require_GET
from django.utils.html import escape
from .models import Order, DeliveryMethod
from .econt_service import create_econt_label
import json
from django.views.decorators.http import require_http_methods
import logging
import stripe
from .views import get_single_product
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from .econt_service import get_cities, get_offices_by_city_id


def _get_current_order(request):
    oid = request.session.get("current_order_id")
    return Order.objects.filter(pk=oid).first() if oid else None


def _looks_like_address(s: str) -> bool:
    if not s:
        return False
    s = s.strip()
    # Must contain at least one letter and one digit and length >= 6
    has_letter = any(c.isalpha() for c in s)
    has_digit = any(c.isdigit() for c in s)
    return has_letter and has_digit and len(s) >= 6


logger = logging.getLogger("stripe")


@require_http_methods(["GET"])
def econt_collect(request):
    """
    Stripe success redirect. Verify payment server-side and mark Order.paid,
    then route to the correct Econt page (address/office).
    """
    session_id = request.GET.get("session_id") or request.session.get("stripe_session_id")
    if not session_id:
        messages.error(request, "Липсва session_id от Stripe. Моля, свържете се с нас.")
        logger.error("Stripe success redirect without session_id.")
        return redirect("checkout_info")

    try:
        sess = stripe.checkout.Session.retrieve(
            session_id,
            expand=["payment_intent", "line_items"],
            api_key=settings.STRIPE_SECRET_KEY,
        )
        logger.info("✅ Success redirect: session %s, status=%s, payment_status=%s",
                    sess.id, sess.get("status"), sess.get("payment_status"))
    except Exception as e:
        logger.error("Stripe retrieve failed for %s: %s", session_id, e)
        messages.error(request, "Грешка при потвърждение на плащане.")
        return redirect("checkout_info")

    if sess.get("payment_status") != "paid":
        messages.warning(request, "Плащането все още не е потвърдено от Stripe.")
        return redirect("checkout_info")

    order_id = (sess.get("metadata") or {}).get("order_id")
    if not order_id:
        logger.error("Stripe session %s has no metadata.order_id", sess.id)
        messages.error(request, "Липсва информация за поръчката.")
        return redirect("checkout_info")

    try:
        order = Order.objects.get(pk=order_id)
    except Order.DoesNotExist:
        logger.error("Order %s not found for Stripe session %s", order_id, sess.id)
        messages.error(request, "Поръчката не беше намерена.")
        return redirect("checkout_info")

    if not order.paid:
        order.paid = True
        order.save(update_fields=["paid"])
        try:
            from .utils import send_order_notification
            send_order_notification(order, event="paid")
        except Exception as e:
            logger.error("send_order_notification failed for order %s: %s", order.pk, e)
        # Do NOT create label here; user still needs to enter address/office details.

    # keep session hints
    request.session["current_order_id"] = order.pk
    request.session["stripe_session_id"] = sess.id

    # Route to the correct Econt page; no collect.html!
    if order.delivery_method == DeliveryMethod.TO_ADDRESS:
        return redirect("econt_collect_address")
    else:
        return redirect("econt_collect_office")


@require_http_methods(["GET"])
def econt_collect_address(request):
    """COD → address flow: show only the address form."""
    order = _get_current_order(request)
    if not order:
        messages.error(request, "Няма активна поръчка.")
        return redirect("checkout_info")
    return render(request, "econt/address.html", {"order": order})


@require_http_methods(["GET"])
def econt_collect_office(request):
    """COD → office/APS flow: show only the office form."""
    order = _get_current_order(request)
    if not order:
        messages.error(request, "Няма активна поръчка.")
        return redirect("checkout_info")
    return render(request, "econt/office.html", {"order": order})


@require_http_methods(["POST"])
def econt_submit(request):
    order = _get_current_order(request)
    if not order:
        messages.error(request, "Няма активна поръчка.")
        return redirect("checkout_info")

    # Basic fields
    order.full_name = (request.POST.get("full_name") or order.full_name or "").strip()
    order.phone = (request.POST.get("phone") or order.phone or "").strip()  # <- fix plural
    order.city = (request.POST.get("city") or order.city or "").strip()  # <- now comes from hidden field

    # Mode comes from hidden input on each page
    to_office = request.POST.get("to_office") == "1"
    back_name = "econt_collect_office" if to_office else "econt_collect_address"

    office_code = (request.POST.get("receiver_office_code") or "").strip()
    r_street = (request.POST.get("receiver_street") or "").strip()
    r_num = (request.POST.get("receiver_num") or "").strip()
    r_postcode = (request.POST.get("receiver_postcode") or "").strip()
    r_entrance = (request.POST.get("receiver_entrance") or "").strip()
    r_floor = (request.POST.get("receiver_floor") or "").strip()
    r_apartment = (request.POST.get("receiver_apartment") or "").strip()

    # Minimal sanity checks
    if not order.full_name:
        messages.error(request, "Моля, въведете име и фамилия.")
        return redirect(back_name)
    if not order.phone:
        messages.error(request, "Моля, въведете телефон.")
        return redirect(back_name)
    if not order.city:
        messages.error(request, "Моля, въведете град.")
        return redirect(back_name)

    overrides = {}

    if to_office:
        if not office_code:
            messages.error(request, "Въведете код на офис.")
            return redirect(back_name)
        overrides["receiver_office_code"] = office_code
        order.econt_office_code = office_code
        order.delivery_method = DeliveryMethod.TO_OFFICE
    else:
        if not r_street or not r_num:
            messages.error(request, "За доставка до адрес попълнете „Улица“ и „№“.")
            return redirect(back_name)
        overrides.update({
            "receiver_street": r_street,
            "receiver_num": r_num,
            "receiver_postcode": r_postcode or None,
            "receiver_entrance": r_entrance or None,
            "receiver_floor": r_floor or None,
            "receiver_apartment": r_apartment or None,
        })
        order.econt_office_code = ""
        order.delivery_method = DeliveryMethod.TO_ADDRESS

    order.save()

    result = create_econt_label(order, overrides=overrides)

    if not result.get("ok"):
        msg = result.get("error") or "Неуспешно създаване на товарителница."
        if "Empty response" in msg:
            msg += " (проверете съвпадението град ↔ офис или попълнете улица и №)."
        messages.error(request, f"Грешка при Еконт: {msg}")
        return redirect(back_name)

    messages.success(request, "Товарителницата е създадена успешно.")
    return redirect("thank_you")


@require_GET
def api_econt_cities(request):
    """
    GET /api/econt/cities/?q=burg
    """
    q = (request.GET.get("q") or "").strip()
    try:
        items = get_cities(country_code="BGR", name_query=q)
        return JsonResponse({"ok": True, "items": items})
    except Exception as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=502)


@require_GET
def api_econt_offices(request):
    """
    GET /api/econt/offices/?cityID=47
    Strictly follows getOffices(countryCode=BGR, cityID=<id>)
    """
    city_id = request.GET.get("cityID")
    if not city_id:
        return JsonResponse({"ok": False, "error": "Missing cityID"}, status=400)
    try:
        items = get_offices_by_city_id(int(city_id), country_code="BGR")
        return JsonResponse({"ok": True, "items": items})
    except Exception as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=502)

# shop/econt_views.py
from django.contrib import messages
from django.conf import settings
from django.shortcuts import redirect, render
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.views.decorators.http import require_http_methods, require_GET
from django.utils.html import escape
from .models import Order, DeliveryMethod, PaymentMethod
from .econt_service import create_econt_label
import json
from django.views.decorators.http import require_http_methods
import logging
import stripe

from .utils import maybe_send_order_email
from .views import get_single_product
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from .econt_service import get_cities, get_offices_by_city_id
import re


def _split_street_num(line: str) -> tuple[str, str]:
    """
    Split 'ул. Обориште 70', 'ul Oborishte №70', 'бул. Витоша 12А', 'ул Пирин 5/7' -> ('ул. Обориште', '70'), etc.
    """
    if not line:
        return "", ""
    s = str(line).strip()

    # Try explicit № first
    m = re.search(r'(?:№\s*)(\d+[A-Za-zА-Яа-я\-\/]*)\s*$', s)
    if not m:
        # Fallback: last numeric token at end
        m = re.search(r'\s(\d+[A-Za-zА-Яа-я\-\/]*)\s*$', s)

    if m:
        num = m.group(1)
        street = s[:m.start(1)].rstrip(' ,№')
        return street.strip(), num.strip()

    return s, ""


def _get_billing(order, name, default=""):
    """
    Read a billing field defensively:
    - supports both billing_postcode and billing_postal_code
    - returns empty string if missing
    """
    # exact name first
    if hasattr(order, name):
        return getattr(order, name) or default
    # common alias for postcode/postal_code
    if name == "billing_postcode" and hasattr(order, "billing_postal_code"):
        return getattr(order, "billing_postal_code") or default
    if name == "billing_postal_code" and hasattr(order, "billing_postcode"):
        return getattr(order, "billing_postcode") or default
    return default


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
    then route to the correct Econt page (address/office). We do NOT create
    an Econt label here because the user still needs to enter address/office.
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
            api_key=settings.STRIPE_SECRET_LIVE_KEY,
        )
        logger.info(
            "✅ Stripe success: session=%s status=%s payment_status=%s",
            sess.get("id"), sess.get("status"), sess.get("payment_status"),
        )
    except Exception as e:
        logger.error("Stripe retrieve failed for %s: %s", session_id, e)
        messages.error(request, "Грешка при потвърждение на плащане.")
        return redirect("checkout_info")

    if sess.get("payment_status") != "paid":
        messages.warning(request, "Плащането все още не е потвърдено от Stripe.")
        return redirect("checkout_info")

    order_id = (sess.get("metadata") or {}).get("order_id")
    if not order_id:
        logger.error("Stripe session %s has no metadata.order_id", sess.get("id"))
        messages.error(request, "Липсва информация за поръчката.")
        return redirect("checkout_info")

    try:
        order = Order.objects.get(pk=order_id)
    except Order.DoesNotExist:
        logger.error("Order %s not found for Stripe session %s", order_id, sess.get("id"))
        messages.error(request, "Поръчката не беше намерена.")
        return redirect("checkout_info")

    # Idempotent mark-as-paid
    if not order.paid:
        order.paid = True
        order.save(update_fields=["paid"])
        try:
            from .utils import maybe_send_order_email
            maybe_send_order_email(order)

        except Exception as e:
            logger.error("send_order_notification failed for order %s: %s", order.pk, e)

    # keep hints in the session
    request.session["current_order_id"] = order.pk
    request.session["stripe_session_id"] = sess.get("id")

    # Route to the chosen delivery flow (no intermediate collect.html)
    if order.delivery_method == DeliveryMethod.TO_ADDRESS:
        return redirect("econt_collect_address")
    return redirect("econt_collect_office")


@require_http_methods(["GET"])
def econt_collect_address(request):
    order, err = _ensure_paid_from_stripe(request)
    if not order:
        messages.error(request, err or "Няма активна поръчка.")
        return redirect("checkout_info")

    # Route to office page if needed
    if order.delivery_method == DeliveryMethod.TO_OFFICE:
        return redirect("econt_collect_office")

    prefill = {
        "full_name": order.full_name or "",
        "phone": order.phone or "",
        "city": order.city or "",
        "receiver_street": "",
        "receiver_num": "",
        "receiver_postcode": order.postal_code or "",
    }

    # If the checkbox was selected on info.html
    if getattr(order, "ship_same_as_billing", False):
        prefill["full_name"] = order.billing_full_name or prefill["full_name"]
        prefill["phone"] = order.billing_phone or prefill["phone"]
        prefill["city"] = order.billing_city or prefill["city"]
        prefill["receiver_postcode"] = order.billing_postcode or prefill["receiver_postcode"]

        # IMPORTANT: use the billing street line here
        street, num = _split_street_num(order.billing_street or order.billing_address_line or "")
        prefill["receiver_street"] = street
        prefill["receiver_num"] = num

    if err:
        messages.warning(request, err)

    return render(request, "econt/address.html", {"order": order, "prefill": prefill})


@require_http_methods(["GET"])
def econt_collect_office(request):
    """Card success or COD → show the OFFICE/APS form, and if card, mark paid."""
    order, err = _ensure_paid_from_stripe(request)
    if not order:
        messages.error(request, err or "Няма активна поръчка.")
        return redirect("checkout_info")

    # If user accidentally landed here but chose address, route them correctly
    if order.delivery_method == DeliveryMethod.TO_ADDRESS:
        return redirect("econt_collect_address")

    if err:
        messages.warning(request, err)
    return render(request, "econt/office.html", {"order": order})


@require_http_methods(["POST"])
def econt_submit(request):
    order = _get_current_order(request)
    if not order:
        messages.error(request, "Няма активна поръчка.")
        return redirect("checkout_info")

    # Basic fields
    order.full_name = (request.POST.get("full_name") or order.full_name or "").strip()
    order.phone = (request.POST.get("phone") or order.phone or "").strip()  # <- singular
    order.city = (request.POST.get("city") or order.city or "").strip()  # <- now posted from office.html

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
    maybe_send_order_email(order)

    if not result.get("ok"):
        msg = result.get("error") or "Неуспешно създаване на товарителница."
        if "Empty response" in msg:
            msg += " (проверете съвпадението град ↔ офис или попълнете улица и №)."
        messages.error(request, f"Грешка при Еконт: {msg}")
        return redirect(back_name)

    # messages.success(request, "Товарителницата е създадена успешно.")
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


def _ensure_paid_from_stripe(request):
    """
    If we arrived here after Stripe success, verify the session and idempotently
    mark the order as paid. For COD (cash on delivery) we skip Stripe entirely.
    Returns (order, error_msg_or_None).
    """
    current = _get_current_order(request)

    # If current order exists and it's COD → never touch Stripe
    if current and getattr(current, "payment_method", None) == PaymentMethod.COD:
        return current, None

    session_id = request.GET.get("session_id") or request.session.get("stripe_session_id")
    if not session_id:
        # No Stripe context → just return whatever we have (COD / revisit)
        return current, None

    try:
        sess = stripe.checkout.Session.retrieve(
            session_id,
            expand=["payment_intent"],
            api_key=settings.STRIPE_SECRET_LIVE_KEY,
        )
    except Exception as e:
        return None, f"Грешка при потвърждение на плащане: {e}"

    if sess.get("payment_status") != "paid":
        return None, "Плащането все още не е потвърдено от Stripe."

    order_id = (sess.get("metadata") or {}).get("order_id")
    if not order_id:
        return None, "Липсва информация за поръчката (order_id)."

    # IMPORTANT: only trust Stripe if it matches the order in our session (if any)
    if current and str(current.pk) != str(order_id):
        # Mismatch → ignore this Stripe session; treat like COD/revisit
        return current, None

    try:
        order = Order.objects.get(pk=order_id)
    except Order.DoesNotExist:
        return None, "Поръчката не беше намерена."

    # Extra guard: never flip to paid if order method is COD
    if getattr(order, "payment_method", None) == PaymentMethod.COD:
        request.session["current_order_id"] = order.pk
        request.session["stripe_session_id"] = sess.get("id")
        return order, None

    if not order.paid:
        order.paid = True
        order.save(update_fields=["paid"])
        try:
            from .utils import maybe_send_order_email
            maybe_send_order_email(order)
        except Exception:
            pass

    request.session["current_order_id"] = order.pk
    request.session["stripe_session_id"] = sess.get("id")
    return order, None

# shop/econt_views.py
from django.contrib import messages
from django.conf import settings
from django.shortcuts import redirect, render
from django.http import HttpResponse, HttpResponseBadRequest
from django.views.decorators.http import require_http_methods
from django.utils.html import escape
from .models import Order
from .econt_service import create_econt_label
import json
from django.views.decorators.http import require_http_methods
import logging
import stripe


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
    Stripe success redirect. Verify payment server-side and then show the Econt form.
    """
    # 1) Get the session id returned by Stripe (query param) or from our session
    session_id = request.GET.get("session_id") or request.session.get("stripe_session_id")
    if not session_id:
        messages.error(request, "Липсва session_id от Stripe. Моля, свържете се с нас.")
        logger.error("Stripe success redirect without session_id.")
        return redirect("checkout_info")

    # 2) Retrieve the session from Stripe and validate payment
    try:
        sess = stripe.checkout.Session.retrieve(
            session_id,
            expand=["payment_intent", "line_items"],
            api_key=settings.STRIPE_SECRET_KEY,
        )
        logger.info(
            "✅ Success redirect: session %s, status=%s, payment_status=%s",
            sess.id, sess.get("status"), sess.get("payment_status")
        )
        logger.debug("Full session: %s", json.dumps(sess, indent=2, default=str))
    except Exception as e:
        logger.error("Stripe retrieve failed for %s: %s", session_id, e)
        messages.error(request, "Грешка при потвърждение на плащане.")
        return redirect("checkout_info")

    if sess.get("payment_status") != "paid":
        messages.warning(request, "Плащането все още не е потвърдено от Stripe.")
        return redirect("checkout_info")

    # 3) Resolve the order from metadata
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

    # 4) Mark as paid idempotently
    if not order.paid:
        order.paid = True
        order.save(update_fields=["paid"])
        logger.info("💰 Marked order %s as PAID from success redirect.", order.pk)
        try:
            from .utils import send_order_notification
            send_order_notification(order, event="paid")
        except Exception as e:
            logger.error("send_order_notification failed for order %s: %s", order.pk, e)

    # 5) Keep references in session for the Econt form + submit step
    request.session["current_order_id"] = order.pk
    request.session["stripe_session_id"] = sess.id  # harmless to refresh

    # 6) Render the Econt form (let POST /econt/submit/ create the label)
    return render(request, "econt/collect.html", {
        "order": order,
        "stripe_session_id": sess.id,  # in case you want to show/track it
    })


@require_http_methods(["POST"])
def econt_submit(request):
    order = _get_current_order(request)
    if not order:
        messages.error(request, "Няма активна поръчка.")
        return redirect("checkout_info")

    # Basic fields
    order.full_name = request.POST.get("full_name", order.full_name).strip()
    order.phones = request.POST.get("phone", order.phone).strip()
    order.city = request.POST.get("city", order.city).strip()

    # which route?
    to_office = request.POST.get("to_office") == "1"

    # office (new name)
    office_code = (request.POST.get("receiver_office_code") or "").strip()

    # structured address (new names)
    r_street = (request.POST.get("receiver_street") or "").strip()
    r_num = (request.POST.get("receiver_num") or "").strip()
    r_postcode = (request.POST.get("receiver_postcode") or "").strip()
    r_entrance = (request.POST.get("receiver_entrance") or "").strip()
    r_floor = (request.POST.get("receiver_floor") or "").strip()
    r_apartment = (request.POST.get("receiver_apartment") or "").strip()

    # Minimal sanity checks
    if not order.full_name:
        messages.error(request, "Моля, въведете име и фамилия.")
        return redirect("econt_collect")
    if not order.phone:
        messages.error(request, "Моля, въведете телефон.")
        return redirect("econt_collect")
    if not order.city:
        messages.error(request, "Моля, въведете град.")
        return redirect("econt_collect")

    overrides = {}

    if to_office:
        overrides["receiver_office_code"] = office_code
        # keep it on the order for convenience
        order.econt_office_code = office_code
    else:
        if not r_street or not r_num:
            messages.error(request, "За доставка до адрес попълнете „Улица“ и „№“.")
            return redirect("econt_collect")
        overrides.update({
            "receiver_street": r_street,
            "receiver_num": r_num,
            "receiver_postcode": r_postcode or None,
            "receiver_entrance": r_entrance or None,
            "receiver_floor": r_floor or None,
            "receiver_apartment": r_apartment or None,
        })
        # clear any office selection stored on the order
        order.econt_office_code = ""

    order.save()

    result = create_econt_label(order, overrides=overrides)

    if not result.get("ok"):
        msg = result.get("error") or "Неуспешно създаване на товарителница."
        if "Empty response" in msg:
            msg += " (проверете съвпадението град ↔ офис или попълнете улица и №)."
        messages.error(request, f"Грешка при Еконт: {msg}")
        return redirect("econt_collect")

    messages.success(request, "Товарителницата е създадена успешно.")
    return redirect("thank_you")

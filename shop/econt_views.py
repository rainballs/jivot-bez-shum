# shop/econt_views.py
from django.contrib import messages
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods
from django.utils.html import escape
from .models import Order
from .econt_service import create_econt_label


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


@require_http_methods(["GET"])
def econt_collect(request):
    order = _get_current_order(request)
    if not order:
        messages.error(request, "Няма активна поръчка.")
        return redirect("checkout_info")
    if order.econt_shipment_num:
        return redirect("thank_you")
    return render(request, "econt/collect.html", {"order": order})


@require_http_methods(["POST"])
def econt_submit(request):
    order = _get_current_order(request)
    if not order:
        messages.error(request, "Няма активна поръчка.")
        return redirect("checkout_info")

    # Basic fields
    order.full_name = request.POST.get("full_name", order.full_name).strip()
    order.phone = request.POST.get("phone", order.phone).strip()
    order.city = request.POST.get("city", order.city).strip()

    to_office = request.POST.get("to_office") == "1"
    office_code = (request.POST.get("office_code") or "").strip()

    # Structured address fields (to-door)
    street = (request.POST.get("street") or "").strip()
    street_num = (request.POST.get("street_num") or "").strip()
    post_code = (request.POST.get("post_code") or "").strip()
    entrance = (request.POST.get("entrance") or "").strip()
    floor = (request.POST.get("floor") or "").strip()
    apartment = (request.POST.get("apartment") or "").strip()

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
        if not office_code.isdigit():
            messages.error(request, "Кодът на офиса трябва да е числов (напр. 1501).")
            return redirect("econt_collect")
        order.econt_office_code = office_code
        # clear address string for clarity
        order.address = ""
    else:
        # require at least street + number
        if not street or not street_num:
            messages.error(request, "За доставка до адрес попълнете „Улица“ и „№“.")
            return redirect("econt_collect")
        order.econt_office_code = ""
        # keep a human-readable address string in your order
        order.address = f"{street} {street_num}".strip()
        # hand structured parts to the service (no DB fields needed)
        overrides.update({
            "receiver_street": street,
            "receiver_num": street_num,
            "receiver_postcode": post_code,
            "receiver_entrance": entrance,
            "receiver_floor": floor,
            "receiver_apartment": apartment,
        })

    order.save()

    # Create label (pass overrides for structured address)
    from .econt_service import create_econt_label
    result = create_econt_label(order, overrides=overrides)

    if not result.get("ok"):
        msg = result.get("error") or "Неуспешно създаване на товарителница."
        if "Empty response" in msg:
            msg += " (проверете съвпадението град ↔ офис или попълнете улица и №)."
        messages.error(request, f"Грешка при Еконт: {msg}")
        return redirect("econt_collect")

    messages.success(request, "Товарителницата е създадена успешно.")
    return redirect("thank_you")

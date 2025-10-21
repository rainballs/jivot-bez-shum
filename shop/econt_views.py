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

    # Save basic edits
    order.full_name = request.POST.get("full_name", order.full_name).strip()
    order.phone = request.POST.get("phone", order.phone).strip()
    order.city = request.POST.get("city", order.city).strip()
    address_in = (request.POST.get("address") or "").strip()
    to_office = request.POST.get("to_office") == "1"
    office_code = (request.POST.get("office_code") or "").strip()

    # Validate
    if not order.full_name:
        messages.error(request, "Моля, въведете име и фамилия.")
        return redirect("econt_collect")
    if not order.phone:
        messages.error(request, "Моля, въведете телефон.")
        return redirect("econt_collect")
    if not order.city:
        messages.error(request, "Моля, въведете град.")
        return redirect("econt_collect")

    if to_office:
        if not office_code.isdigit():
            messages.error(request, "Кодът на офиса трябва да е числов (напр. 1501).")
            return redirect("econt_collect")
        # Important: city must match the office city; add a hint:
        messages.info(request, "Уверете се, че кодът на офиса е от същия град: " + escape(order.city))
        order.econt_office_code = office_code
        order.address = ""  # not needed for office delivery
    else:
        if not _looks_like_address(address_in):
            messages.error(request, "Въведете валиден адрес (напр. „ул. Александър Велики 12, ет. 3“).")
            return redirect("econt_collect")
        order.address = address_in
        order.econt_office_code = ""

    order.save()

    # Create label
    result = create_econt_label(order)
    if not result.get("ok"):
        msg = result.get("error") or "Неуспешно създаване на товарителница."
        if "Empty response" in msg:
            msg += " (проверете съвпадението град ↔ офис и че адресът е пълен)."
        messages.error(request, f"Грешка при Еконт: {msg}")
        return redirect("econt_collect")

    messages.success(request, "Товарителницата е създадена успешно.")
    return redirect("thank_you")

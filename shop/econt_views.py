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
    order.phones = list(request.POST.get("phone", order.phone).strip())
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
        if not office_code.isdigit():
            messages.error(request, "Кодът на офиса трябва да е числов (напр. 1501).")
            return redirect("econt_collect")
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

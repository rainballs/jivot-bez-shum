# shop/econt_views.py
from django.contrib import messages
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods
from .models import Order
from .econt_service import create_econt_label


def _get_current_order(request):
    oid = request.session.get("current_order_id")
    return Order.objects.filter(pk=oid).first() if oid else None


@require_http_methods(["GET"])
def econt_collect(request):
    """
    Page shown AFTER Stripe success or right after COD selection.
    Lets customer confirm address or choose 'to office' code, then posts to /econt/submit/.
    """
    order = _get_current_order(request)
    if not order:
        messages.error(request, "Няма активна поръчка.")
        return redirect("checkout_info")

    # If already have a shipment, skip to thank-you
    if order.econt_shipment_num:
        return redirect("thank_you")

    return render(request, "econt/collect.html", {
        "order": order,
    })


@require_http_methods(["POST"])
def econt_submit(request):
    """
    Takes the shipping choice and creates the Econt label.
    """
    order = _get_current_order(request)
    if not order:
        messages.error(request, "Няма активна поръчка.")
        return redirect("checkout_info")

    # Save/override fields from the form (keeps it simple)
    order.full_name = request.POST.get("full_name", order.full_name)
    order.phone = request.POST.get("phone", order.phone)
    order.city = request.POST.get("city", order.city)
    order.address = request.POST.get("address", getattr(order, "address", ""))  # or address_line field
    to_office = request.POST.get("to_office") == "1"
    office_code = (request.POST.get("office_code") or "").strip()
    order.econt_office_code = request.POST.get("office_code", "") if to_office else ""

    if to_office:
        if not office_code.isdigit():
            messages.error(request, "Кодът на офиса трябва да е числов (напр. 1501).")
            return redirect("econt_collect")
        order.econt_office_code = office_code
        # shipping to office → address not required
    else:
        order.econt_office_code = ""

    order.save()

    # Now create the label (paid card → COD=0, COD → we’ll send total)
    result = create_econt_label(order)
    if not result.get("ok"):
        msg = result.get("error") or "Неуспешно създаване на товарителница."
        # Common cause hint:
        if "Invalid XML" in msg or "Empty response" in msg:
            msg += " (вероятно невалиден код на офис или липсващо поле)"
        messages.error(request, f"Грешка при Еконт: {msg}")
        return redirect("econt_collect")

    messages.success(request, "Товарителницата е създадена успешно.")
    return redirect("thank_you")

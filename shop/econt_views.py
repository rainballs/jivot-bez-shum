# shop/econt_views.py
"""Delivery-data collection and Econt nomenclature endpoints. NOTHING in here creates a shipment."""
import logging

from django.contrib import messages
from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from .address import split_street_num
from .checkout import (
    apply_selection,
    ensure_editable,
    get_session_order,
    save_delivery_and_quote,
    throttled,
)
from .econt_service import get_cities, get_offices_by_city_id
from .models import DeliveryMethod

logger = logging.getLogger("shop")


def _prefill(order) -> dict:
    prefill = {
        "full_name": order.full_name or "",
        "phone": order.phone or "",
        "city": order.city or "",
        "receiver_street": order.receiver_street or "",
        "receiver_num": order.receiver_num or "",
        "receiver_postcode": order.postal_code or "",
        "receiver_quarter": order.receiver_quarter or "",
        "receiver_other": order.receiver_other or "",
    }
    if getattr(order, "ship_same_as_billing", False):
        prefill["full_name"] = order.billing_full_name or prefill["full_name"]
        prefill["phone"] = order.billing_phone or prefill["phone"]
        prefill["city"] = order.billing_city or prefill["city"]
        prefill["receiver_postcode"] = order.billing_postcode or prefill["receiver_postcode"]
        if not prefill["receiver_street"]:
            street, num = split_street_num(order.billing_street or "")
            prefill["receiver_street"] = street
            prefill["receiver_num"] = num
    return prefill


@require_http_methods(["GET"])
def econt_collect(request):
    """Legacy Stripe success URL. Payment is recorded by the webhook / thank_you page, never here."""
    target = reverse("thank_you")
    qs = request.GET.urlencode()
    return redirect(f"{target}?{qs}" if qs else target)


@require_http_methods(["GET"])
def econt_collect_address(request):
    order = get_session_order(request)
    if not order:
        messages.error(request, "Няма активна поръчка.")
        return redirect("checkout_info")
    if order.delivery_method == DeliveryMethod.TO_OFFICE:
        return redirect("econt_collect_office")
    return render(request, "econt/address.html", {"order": order, "prefill": _prefill(order)})


@require_http_methods(["GET"])
def econt_collect_office(request):
    order = get_session_order(request)
    if not order:
        messages.error(request, "Няма активна поръчка.")
        return redirect("checkout_info")
    if order.delivery_method == DeliveryMethod.TO_ADDRESS:
        return redirect("econt_collect_address")
    return render(request, "econt/office.html", {"order": order})


@require_http_methods(["POST"])
def econt_submit(request):
    """Legacy non-AJAX form: save + validate + quote delivery, then go to the summary. Never creates a label."""
    order = get_session_order(request)
    if not order:
        messages.error(request, "Няма активна поръчка.")
        return redirect("checkout_info")

    to_office = request.POST.get("to_office") == "1"
    back_name = "econt_collect_office" if to_office else "econt_collect_address"

    if not ensure_editable(order):
        messages.error(request, "Поръчката вече е потвърдена и не може да се променя.")
        return redirect("checkout_summary")

    error, touched = apply_selection(order, request.POST)
    if error:
        messages.error(request, error)
        return redirect(back_name)
    if touched:
        order.save(update_fields=sorted(touched))

    error = save_delivery_and_quote(order, request.POST, to_office)
    if error:
        messages.error(request, error)
        return redirect(back_name)
    return redirect("checkout_summary")


# --------------------------------------------------------------------------- nomenclature proxies
@require_GET
def api_econt_cities(request):
    """GET /api/econt/cities/?q=burg  (public, cached and rate limited: it proxies a third-party API)"""
    q = (request.GET.get("q") or "").strip()[:60]
    if throttled(request, "econt_api", 120, 60):
        return JsonResponse({"ok": False, "error": "Твърде много заявки."}, status=429)
    key = f"econt:cities:{q.lower()}"
    items = cache.get(key)
    if items is None:
        try:
            items = get_cities(country_code="BGR", name_query=q)
        except Exception:
            logger.exception("Econt getCities failed")
            return JsonResponse({"ok": False, "error": "Еконт временно не отговаря."}, status=502)
        cache.set(key, items, 600)
    return JsonResponse({"ok": True, "items": items})


@require_GET
def api_econt_offices(request):
    """GET /api/econt/offices/?cityID=47"""
    city_id = request.GET.get("cityID") or ""
    if not city_id.isdigit() or len(city_id) > 8:
        return JsonResponse({"ok": False, "error": "Invalid cityID"}, status=400)
    if throttled(request, "econt_api", 120, 60):
        return JsonResponse({"ok": False, "error": "Твърде много заявки."}, status=429)
    key = f"econt:offices:{city_id}"
    items = cache.get(key)
    if items is None:
        try:
            items = get_offices_by_city_id(int(city_id), country_code="BGR")
        except Exception:
            logger.exception("Econt getOffices failed")
            return JsonResponse({"ok": False, "error": "Еконт временно не отговаря."}, status=502)
        cache.set(key, items, 600)
    return JsonResponse({"ok": True, "items": items})


# --------------------------------------------------------------------------- inline (AJAX) flow
@require_GET
def econt_partial_address(request):
    """Return the address form HTML for the current order (from session)."""
    order = get_session_order(request)
    if not order:
        return JsonResponse({"ok": False, "html": "<p class='small muted'>Няма активна поръчка.</p>"})
    html = render_to_string("econt/_address_inline.html", {"order": order, "prefill": _prefill(order)},
                            request=request)
    return JsonResponse({"ok": True, "html": html})


@require_GET
def econt_partial_office(request):
    order = get_session_order(request)
    if not order:
        return JsonResponse({"ok": False, "html": "<p class='small muted'>Няма активна поръчка.</p>"})
    html = render_to_string("econt/_office_inline.html", {"order": order}, request=request)
    return JsonResponse({"ok": True, "html": html})


@require_POST
def econt_submit_inline(request):
    order = get_session_order(request)
    if not order:
        return JsonResponse({"ok": False, "error": "Няма активна поръчка."}, status=400)

    if not ensure_editable(order):
        return JsonResponse({"ok": False, "error": "Поръчката вече е потвърдена и не може да се променя."},
                            status=409)

    if throttled(request, "quote", 40, 600):
        return JsonResponse({"ok": False, "error": "Твърде много опити. Опитайте по-късно."}, status=429)

    # The submit carries the customer's FINAL selections (payment / delivery / quantity), so the price quote and the
    # stored order can never disagree with what is on screen, whatever happened to the background autosaves.
    error, touched = apply_selection(order, request.POST)
    if error:
        return JsonResponse({"ok": False, "error": error})
    if touched:
        order.save(update_fields=sorted(touched))

    to_office = request.POST.get("to_office") == "1"
    error = save_delivery_and_quote(order, request.POST, to_office)
    if error:
        return JsonResponse({"ok": False, "error": error})

    return JsonResponse({"ok": True, "next": "summary", "redirect": reverse("checkout_summary")})

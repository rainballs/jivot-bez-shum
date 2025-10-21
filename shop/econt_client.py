# shop/econt_client.py
import base64
import logging
from typing import Optional, Tuple
import requests
from django.conf import settings
from lxml import etree

log = logging.getLogger(__name__)


class EcontClient:
    def __init__(self):
        base = settings.ECONT["BASE_URL"].rstrip("/")
        self.create_label_url = f"{base}/createLabel"
        self.headers = {"Content-Type": "application/xml; charset=utf-8"}
        self.auth = (settings.ECONT["USER"], settings.ECONT["PASS"])

    def _post_xml(self, url: str, xml_bytes: bytes):
        log.info("ECONT POST %s\n%s", url, xml_bytes.decode("utf-8", errors="ignore"))
        r = requests.post(url, data=xml_bytes, headers=self.headers, auth=self.auth, timeout=30)
        log.info("ECONT RESP %s %s\n%s", url, r.status_code, (r.text or "")[:3000])
        r.raise_for_status()
        if not r.text.strip():
            # Make the failure explicit (this is what you saw)
            raise RuntimeError("Empty response from Econt (check office code / required fields).")
        return r.text


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


def receiver_phone_fmt(phone: str) -> str:
    """Return +359XXXXXXXXX (digits only), tolerant to input."""
    if not phone:
        return ""
    digits = "".join(ch for ch in phone if ch.isdigit())
    if digits.startswith("359"):
        digits = digits[3:]
    elif digits.startswith("0"):
        digits = digits[1:]
    return "+359" + digits


def parse_label_response(xml_text: str) -> Tuple[Optional[str], Optional[bytes], Optional[str]]:
    """
    (shipment_num, pdf_bytes, error) — defensive against minor schema variants.
    """
    try:
        root = etree.fromstring(xml_text.encode("utf-8"))
    except Exception as e:
        return None, None, f"Invalid XML from Econt: {e}"

    num = None
    for xp in ["//shipmentNumber", "//shipment/num", "//num"]:
        node = root.xpath(xp)
        if node and (node[0].text or "").strip():
            num = node[0].text.strip()
            break

    pdf_b64 = None
    for xp in ["//labelPDF", "//pdf", "//label"]:
        node = root.xpath(xp)
        if node and (node[0].text or "").strip():
            pdf_b64 = node[0].text.strip()
            break

    pdf_bytes = None
    if pdf_b64:
        try:
            pdf_bytes = base64.b64decode(pdf_b64)
        except Exception:
            pdf_bytes = None

    # collect any error messages
    err = None
    errs = root.xpath("//error | //errors/error | //message[@type='error']")
    if errs:
        msgs = []
        for n in errs:
            t = (n.text or "").strip()
            if t:
                msgs.append(t)
        if msgs:
            err = "; ".join(msgs)

    return num, pdf_bytes, err

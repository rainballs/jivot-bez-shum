# shop/checkout.py
"""Request-level helpers shared by the checkout views (session order, throttling, input handling)."""
from __future__ import annotations

import ipaddress
import logging
import re

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.utils import timezone

from . import payments
from .econt_service import quote_shipping
from .address import street_variants
from .forms import phone_validator
from .models import DeliveryMethod, Order, PaymentMethod, PaymentStatus

log = logging.getLogger("shop.checkout")

SESSION_KEY = "current_order_id"
# Billing fields accepted from the browser and their maximum lengths (mirrors the Order model).
BILLING_LIMITS = {
    "billing_full_name": 200, "billing_email": 254, "billing_phone": 32,
    "billing_city": 120, "billing_street": 255, "billing_postcode": 16,
}
OFFICE_CODE_RE = re.compile(r"^[0-9A-Za-z\-]{1,16}$")


def get_session_order(request) -> Order | None:
    """The ONLY way views find 'the current order': the server-side session. Never trust ids from GET/POST."""
    oid = request.session.get(SESSION_KEY)
    return Order.objects.filter(pk=oid).first() if oid else None


def client_ip(request) -> str:
    if getattr(settings, "TRUST_X_FORWARDED_FOR", False):
        xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if xff:
            return xff.split(",")[-1].strip()  # the entry appended by OUR proxy, not client-supplied ones
    return request.META.get("REMOTE_ADDR", "unknown")


PROXY_LIMIT_MULTIPLIER = 50


def _is_proxy_address(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_loopback or addr.is_private


def throttled(request, scope: str, limit: int, window: int) -> bool:
    """
    Fixed-window per-IP counter (django cache). Returns True when the caller is over the limit.

    Behind a reverse proxy REMOTE_ADDR is the proxy for EVERY visitor. Unless TRUST_X_FORWARDED_FOR gives us the
    real client address, a loopback/private source is treated as "unknown, shared" and gets a much higher limit,
    so a busy shop is never locked out by its own rate limiter (it still stops floods).
    """
    ip = client_ip(request)
    if _is_proxy_address(ip) and not getattr(settings, "TRUST_X_FORWARDED_FOR", False):
        limit *= PROXY_LIMIT_MULTIPLIER
    key = f"thr:{scope}:{ip}"
    try:
        cache.add(key, 0, window)
        return cache.incr(key) > limit
    except Exception:  # cache outage must not break checkout
        return False


def ensure_editable(order: Order) -> bool:
    """Orders with a live/paid payment or a confirmed COD are frozen. A pending card attempt is released first."""
    if order.payment_status == PaymentStatus.PENDING and order.cod_confirmed_at is None:
        payments.release_pending_payment(order.pk)
        order.refresh_from_db()
    return not order.is_locked


def parse_quantity(raw) -> int | None:
    try:
        q = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return q if 1 <= q <= settings.MAX_ORDER_QUANTITY else None


def parse_payment_method(raw) -> str | None:
    """Only the two methods the UI offers are accepted (arbitrary strings used to be stored as-is)."""
    return {"card": PaymentMethod.CARD, "cod": PaymentMethod.COD}.get((raw or "").strip().lower())


def parse_delivery_method(raw) -> str | None:
    return {"address": DeliveryMethod.TO_ADDRESS, "office": DeliveryMethod.TO_OFFICE}.get((raw or "").strip().lower())


def _field(post, name: str, maxlen: int) -> str:
    v = (post.get(name) or "").strip()
    if len(v) > maxlen:
        raise ValueError(f"Полето е твърде дълго ({name}).")
    return v


QUOTE_FIELDS = {"shipping_quoted_at"}
TOTAL_FIELDS = {"quantity", "subtotal_eur", "shipping_eur", "total_eur"}


def apply_selection(order: Order, post) -> tuple[str | None, set[str]]:
    """
    Apply quantity / payment / delivery selections sent by the browser. Everything is whitelisted and range-checked.
    Returns (error_text_or_None, names_of_fields_actually_changed) so callers can save ONLY those fields
    (a full-row save from a stale instance used to overwrite concurrent changes such as the payment method).
    """
    touched: set[str] = set()
    if post.get("quantity"):
        q = parse_quantity(post.get("quantity"))
        if q is None:
            return f"Количеството трябва да е между 1 и {settings.MAX_ORDER_QUANTITY}.", touched
        if q != order.quantity:
            order.set_quantity(q)
            order.recompute_totals()
            touched |= TOTAL_FIELDS | QUOTE_FIELDS
    if post.get("payment_method"):
        pm = parse_payment_method(post.get("payment_method"))
        if pm is None:
            return "Невалиден начин на плащане.", touched
        if pm != order.payment_method:
            order.payment_method = pm
            touched |= {"payment_method"} | QUOTE_FIELDS  # COD adds a COD fee to the courier price
    if post.get("delivery_method"):
        dm = parse_delivery_method(post.get("delivery_method"))
        if dm is None:
            return "Невалиден начин на доставка.", touched
        if dm != order.delivery_method:
            order.delivery_method = dm
            touched |= {"delivery_method"} | QUOTE_FIELDS
    if touched & QUOTE_FIELDS:
        order.invalidate_quote()
    return None, touched


def save_delivery_and_quote(order: Order, post, to_office: bool) -> str | None:
    """
    Persist contact + delivery data (structured), validate the address with Econt and store the live price.
    Returns None on success or a customer-facing error message (order left un-quoted).
    """
    try:
        full_name = _field(post, "full_name", 150) or _field(post, "billing_full_name", 150) or order.full_name
        phone = _field(post, "phone", 32) or _field(post, "billing_phone", 32) or order.phone
        city = _field(post, "city", 120) or _field(post, "billing_city", 120) or order.city
        email = _field(post, "billing_email", 254) or order.email

        for name, maxlen in BILLING_LIMITS.items():
            if name == "billing_email":
                continue  # handled below together with the contact e-mail
            v = post.get(name)
            if v is not None and v.strip():
                setattr(order, name, _field(post, name, maxlen))
        if _field(post, "billing_email", 254):
            order.billing_email = email

        office_code = _field(post, "receiver_office_code", 16)
        street = _field(post, "receiver_street", 255)
        num = _field(post, "receiver_num", 32)
        postcode = _field(post, "receiver_postcode", 16)
        entrance = _field(post, "receiver_entrance", 16)
        floor = _field(post, "receiver_floor", 16)
        apartment = _field(post, "receiver_apartment", 16)
        quarter = _field(post, "receiver_quarter", 64)
        other = _field(post, "receiver_other", 128)
    except ValueError as e:
        return str(e)

    if not full_name:
        return "Моля, въведете име и фамилия."
    if not phone:
        return "Моля, въведете телефон."
    try:
        phone_validator(phone)
    except ValidationError:
        return "Моля, въведете валиден телефон (пример: +359 888 123 456)."
    if not city:
        return "Моля, въведете град."
    try:
        validate_email(email or "")
    except ValidationError:
        return "Моля, въведете валиден имейл адрес."

    order.full_name, order.phone, order.city, order.email = full_name, phone, city, email

    if to_office:
        if not office_code or not OFFICE_CODE_RE.match(office_code):
            return "Изберете офис."
        order.delivery_method = DeliveryMethod.TO_OFFICE
        order.econt_office_code = office_code
        order.receiver_street = order.receiver_num = ""
        order.receiver_entrance = order.receiver_floor = order.receiver_apartment = ""
        order.receiver_quarter = order.receiver_other = ""
        order.address_line = ""
        if postcode:
            order.postal_code = postcode
    else:
        if not street or not num:
            return "За доставка до адрес попълнете „Улица“ и „№“."
        if not re.fullmatch(r"\d{4}", postcode or order.postal_code or ""):
            return "Пощенският код трябва да е 4 цифри."
        order.delivery_method = DeliveryMethod.TO_ADDRESS
        order.econt_office_code = ""
        order.receiver_street, order.receiver_num = street, num
        order.receiver_entrance, order.receiver_floor, order.receiver_apartment = entrance, floor, apartment
        order.receiver_quarter, order.receiver_other = quarter, other
        order.address_line = f"{street} {num}".strip()
        order.postal_code = postcode or order.postal_code

    order.invalidate_quote()

    # Econt's validator is picky about street spellings: try them in order and keep the one Econt accepted, so that
    # the label created later uses exactly the validated value.
    quote, first_error = None, None
    variants = street_variants(order.receiver_street) if not to_office else [""]
    for variant in variants:
        if not to_office:
            order.receiver_street = variant
        quote = quote_shipping(order)
        if quote["ok"]:
            break
        first_error = first_error or quote["error"]
        if quote.get("retryable"):
            break  # Econt unreachable/slow: another spelling will not help
    if not quote["ok"]:
        if not to_office:
            order.receiver_street = variants[0]  # keep what the customer typed so they can correct it
        order.save()
        order.log_event("quote_failed", (first_error or quote["error"])[:200])
        return first_error or quote["error"]
    order.save()

    order.shipping_eur = quote["ship_eur"]
    order.shipping_quoted_at = timezone.now()
    order.recompute_totals()
    # narrow save: the quote call above can take seconds; do not overwrite fields other requests changed meanwhile
    order.save(update_fields=[
        "shipping_eur", "shipping_quoted_at", "subtotal_eur", "total_eur",
    ])
    order.log_event("quoted", "delivery validated and priced", method=order.delivery_method,
                    ship_eur=str(order.shipping_eur))
    return None


def readiness_error(order: Order) -> str | None:
    """Why this order cannot go to payment / COD confirmation yet (customer-facing, Bulgarian)."""
    if not order.items.exists():
        return "Количката е празна."
    if not order.delivery_ready:
        return "Моля, попълнете и потвърдете данните за доставка, за да изчислим цената."
    try:
        validate_email(order.email or "")
    except ValidationError:
        return "Моля, въведете валиден имейл адрес."
    return None

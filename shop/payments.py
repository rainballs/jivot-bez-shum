# shop/payments.py
"""
Stripe Checkout handling.

Rules:
  * The amount we expect (EUR cents) is computed server-side from the order's OrderItem snapshot + the Econt-quoted
    shipping, and stored on a PaymentAttempt BEFORE the customer is redirected.
  * An order becomes PAID only through `record_session_payment`, which verifies mode, payment_status, currency,
    amount, order association and livemode against OUR records. Both the signed webhook and the browser return
    page call the same function; it is idempotent and safe under concurrency (row locks).
  * Payment never implies shipping logic: this module only moves the order to shipment PENDING; the label is built
    by `fulfillment`, from verified payment state.
"""
from __future__ import annotations

import logging
from decimal import Decimal, ROUND_HALF_UP

import stripe
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import fulfillment
from .models import (
    CARD_METHODS,
    Order,
    PaymentAttempt,
    PaymentMethod,
    PaymentStatus,
    ShipmentStatus,
    StripeEvent,
)

log = logging.getLogger("shop.payments")

STRIPE_CURRENCY = "eur"
SESSION_TTL_SECONDS = 3600  # Stripe: min 30 min, max 24 h. Short window limits "late pay after switching to COD".


class PaymentNotAllowed(Exception):
    """Order is not in a state where a card payment may be started."""


def _api_key() -> str:
    return settings.STRIPE_SECRET_LIVE_KEY


def stripe_is_live() -> bool:
    return _api_key().startswith(("sk_live", "rk_live"))


def to_minor_units(amount: Decimal) -> int:
    return int((Decimal(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100).to_integral_value())


def expected_amount_minor(order: Order) -> int:
    """Goods (OrderItem snapshot) + quoted delivery, in EUR cents. The ONLY source for what we charge."""
    goods = sum(to_minor_units(i.unit_price_eur) * int(i.quantity) for i in order.items.all())
    return goods + to_minor_units(order.shipping_eur or Decimal("0"))


def build_line_items(order: Order) -> list[dict]:
    items = []
    for it in order.items.select_related("product").all():
        cents = to_minor_units(it.unit_price_eur)
        if cents < 1:
            raise PaymentNotAllowed("product price too small")
        items.append({
            "price_data": {
                "currency": STRIPE_CURRENCY,
                "product_data": {"name": it.product.name},
                "unit_amount": cents,
            },
            "quantity": int(it.quantity),
        })
    ship = to_minor_units(order.shipping_eur or Decimal("0"))
    if ship > 0:
        items.append({
            "price_data": {
                "currency": STRIPE_CURRENCY,
                "product_data": {"name": "Доставка"},
                "unit_amount": ship,
            },
            "quantity": 1,
        })
    return items


def check_can_pay(order: Order) -> None:
    if not order.is_card:
        raise PaymentNotAllowed("order payment method is not card")
    if order.payment_status == PaymentStatus.PAID:
        raise PaymentNotAllowed("order is already paid")
    if order.cod_confirmed_at or order.shipment_status != ShipmentStatus.NONE:
        raise PaymentNotAllowed("order was already confirmed as cash on delivery")
    if not order.delivery_ready:
        raise PaymentNotAllowed("delivery data is incomplete or the Econt price has not been calculated")
    if not order.items.exists() or not (order.shipping_eur and order.shipping_eur > 0):
        raise PaymentNotAllowed("order has no items or no shipping price")


def create_checkout_session(order_id: int, site_url: str) -> PaymentAttempt:
    """Create (or reuse) the Stripe Checkout Session for an order. Raises PaymentNotAllowed / stripe errors."""
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order_id)
        check_can_pay(order)
        amount = expected_amount_minor(order)

        open_attempts = list(order.payment_attempts.select_for_update().filter(status=PaymentAttempt.Status.OPEN))
        now = timezone.now()
        for a in open_attempts:
            fresh = (now - a.created_at).total_seconds() < SESSION_TTL_SECONDS - 300
            if a.amount_minor == amount and fresh and a.checkout_url:
                return a  # double click / back button: same session, no second attempt
        for a in open_attempts:  # price/quantity/... changed: the old session must not be payable any more
            _expire_attempt(a)

        n = order.payment_attempts.count() + 1
        session = stripe.checkout.Session.create(
            api_key=_api_key(),
            mode="payment",
            payment_method_types=["card"],
            line_items=build_line_items(order),
            metadata={"order_id": str(order.pk), "order_public_id": str(order.public_id)},
            client_reference_id=str(order.public_id),
            payment_intent_data={"metadata": {"order_id": str(order.pk)}},
            success_url=site_url + "/checkout/thank-you/?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=site_url + "/checkout/",
            customer_email=order.email or None,
            expires_at=int(now.timestamp()) + SESSION_TTL_SECONDS,
            idempotency_key=f"order-{order.public_id}-{amount}-{n}",
        )
        attempt = PaymentAttempt.objects.create(
            order=order, stripe_session_id=session["id"], amount_minor=amount,
            currency=STRIPE_CURRENCY, checkout_url=session.get("url") or "",
        )
        if order.payment_method != PaymentMethod.CARD:
            order.payment_method = PaymentMethod.CARD
            order.save(update_fields=["payment_method"])
        order.transition_payment(PaymentStatus.PENDING)
        order.log_event("payment_attempt_created", session["id"], amount_minor=amount)
        return attempt


def _expire_attempt(attempt: PaymentAttempt) -> bool:
    """Make a Checkout Session un-payable. Returns False if it could not be expired (maybe already paid)."""
    try:
        stripe.checkout.Session.expire(attempt.stripe_session_id, api_key=_api_key())
    except stripe.InvalidRequestError:
        # already complete or expired: look at the truth
        try:
            s = stripe.checkout.Session.retrieve(attempt.stripe_session_id, api_key=_api_key())
        except Exception:
            log.exception("could not retrieve session %s", attempt.stripe_session_id)
            return False
        if s.get("payment_status") == "paid":
            record_session_payment(s, source="expire-check")
            return False
        if s.get("status") != "expired":
            return False  # complete (processing) or still OPEN: not safely expired, keep the order locked
    except Exception:
        log.exception("could not expire session %s", attempt.stripe_session_id)
        return False
    attempt.status = PaymentAttempt.Status.SUPERSEDED
    attempt.save(update_fields=["status", "updated_at"])
    return True


def release_pending_payment(order_id: int) -> bool:
    """
    Customer came back to edit a pending order: expire open Stripe sessions so a late payment is impossible,
    then unlock. Returns True if the order is editable afterwards.
    """
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order_id)
        if order.payment_status == PaymentStatus.PAID:
            return False
        ok = True
        for a in order.payment_attempts.select_for_update().filter(status=PaymentAttempt.Status.OPEN):
            ok = _expire_attempt(a) and ok
        order.refresh_from_db()
        if ok and order.payment_status == PaymentStatus.PENDING:
            order.transition_payment(PaymentStatus.UNPAID)
        return ok and order.payment_status != PaymentStatus.PAID


# ---------------------------------------------------------------------------------- verification
def _obj_id(v):
    if isinstance(v, dict):
        return v.get("id") or ""
    return v or ""


def _flag(order: Order | None, reason: str):
    log.error("stripe payment problem order=%s: %s", getattr(order, "pk", None), reason)
    if order is not None:
        order.flag_review(reason)


def record_session_payment(session, source: str) -> tuple[str, Order | None, str]:
    """
    Verify a *paid* Checkout Session against our records and move the order to PAID (idempotent).
    Returns (result, order, detail) with result in:
      applied | already_applied | duplicate_payment | rejected | not_paid | unknown_order
    """
    sid = session.get("id") or ""
    meta = session.get("metadata") or {}
    pi = _obj_id(session.get("payment_intent"))

    with transaction.atomic():
        attempt = PaymentAttempt.objects.select_for_update().filter(stripe_session_id=sid).first()
        oid = attempt.order_id if attempt else meta.get("order_id")
        try:
            order = Order.objects.select_for_update().filter(pk=int(oid)).first() if oid else None
        except (TypeError, ValueError):
            order = None
        if order is None:
            log.error("stripe session %s refers to unknown order %r", sid, oid)
            return "unknown_order", None, f"unknown order {oid!r}"

        if meta.get("order_id") and str(meta["order_id"]) != str(order.pk):
            _flag(order, f"Stripe session {sid} metadata order_id={meta['order_id']} contradicts our attempt record")
            return "rejected", order, "metadata order mismatch"

        if session.get("payment_status") != "paid":
            return "not_paid", order, f"payment_status={session.get('payment_status')}"

        expected = attempt.amount_minor if attempt else expected_amount_minor(order)
        problems = []
        if session.get("mode") != "payment":
            problems.append(f"mode={session.get('mode')}")
        if (session.get("currency") or "").lower() != STRIPE_CURRENCY:
            problems.append(f"currency={session.get('currency')}")
        if session.get("amount_total") != expected:
            problems.append(f"amount_total={session.get('amount_total')} expected={expected}")
        if problems:
            _flag(order, f"Stripe session {sid} paid but does not match the order: {', '.join(problems)}")
            order.log_event("payment_mismatch", sid, problems=problems, source=source)
            return "rejected", order, "; ".join(problems)

        if order.payment_status == PaymentStatus.PAID:
            if order.stripe_payment_intent_id == pi or not pi:
                return "already_applied", order, ""
            _flag(order, f"SECOND payment {pi} received for already paid order (first {order.stripe_payment_intent_id}) - refund one")
            if attempt:
                attempt.status = PaymentAttempt.Status.PAID
                attempt.save(update_fields=["status", "updated_at"])
            return "duplicate_payment", order, pi

        # ---- apply
        was_cod = order.payment_method == PaymentMethod.COD
        order.paid_amount_minor = session.get("amount_total")
        order.paid_currency = STRIPE_CURRENCY
        order.stripe_payment_intent_id = pi
        order.paid_at = timezone.now()
        if order.payment_method not in CARD_METHODS:
            order.payment_method = PaymentMethod.CARD  # verified fact overrides the stale/mutable choice
        order.save(update_fields=[
            "paid_amount_minor", "paid_currency", "stripe_payment_intent_id", "paid_at", "payment_method",
        ])
        order.transition_payment(PaymentStatus.PAID)
        order.log_event("payment_confirmed", sid, source=source, payment_intent=pi, amount_minor=expected,
                        method_was_cod=was_cod)
        if attempt:
            attempt.status = PaymentAttempt.Status.PAID
            attempt.payment_intent_id = pi
            attempt.paid_at = timezone.now()
            attempt.save(update_fields=["status", "payment_intent_id", "paid_at", "updated_at"])
        # any other open attempt for this order must not collect money a second time
        for other in order.payment_attempts.filter(status=PaymentAttempt.Status.OPEN).exclude(stripe_session_id=sid):
            transaction.on_commit(lambda a=other: _expire_attempt(a))

        if order.shipment_status in (ShipmentStatus.CREATED, ShipmentStatus.IN_PROGRESS, ShipmentStatus.UNKNOWN):
            _flag(order, "Card payment arrived AFTER a shipment was created/started (possibly as cash on delivery): "
                         "check the Econt label and remove the COD amount in e-Econt")
        elif order.shipment_status == ShipmentStatus.FAILED:
            order.flag_review("Paid by card; previous shipment attempt had failed - re-queue the shipment")
        else:
            fulfillment.request_shipment(order.pk)
            fulfillment.dispatch_after_commit(order.pk)
        if was_cod and order.cod_confirmed_at:
            order.flag_review("Customer confirmed COD but also paid by card")

        order_pk = order.pk
        transaction.on_commit(lambda: _notify(order_pk))
        return "applied", order, ""


def _notify(order_pk: int):
    try:
        from .utils import notify_order_accepted

        notify_order_accepted(Order.objects.get(pk=order_pk), event="paid")
    except Exception:
        log.exception("order notification failed for %s", order_pk)


# ---------------------------------------------------------------------------------- webhook events
HANDLED_EVENTS = {
    "checkout.session.completed",
    "checkout.session.async_payment_succeeded",
    "checkout.session.async_payment_failed",
    "checkout.session.expired",
}


def handle_event(event) -> tuple[str, Order | None, str]:
    """Process one verified event. Returns (StripeEvent.Status value, order, detail). Raises on infrastructure errors."""
    etype = event["type"]
    if etype not in HANDLED_EVENTS:
        return StripeEvent.Status.IGNORED, None, "event type not handled"

    if bool(event.get("livemode")) != stripe_is_live():
        log.warning("ignoring %s: livemode=%s but configured key is %s", event["id"], event.get("livemode"),
                    "live" if stripe_is_live() else "test")
        return StripeEvent.Status.IGNORED, None, "livemode mismatch"

    session = event["data"]["object"]

    if etype in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        result, order, detail = record_session_payment(session, source=f"webhook:{event['id']}")
        if result in ("applied", "already_applied", "duplicate_payment"):
            return StripeEvent.Status.PROCESSED, order, result
        if result == "not_paid":  # e.g. delayed method still processing: a later event will carry the payment
            return StripeEvent.Status.IGNORED, order, detail
        return StripeEvent.Status.REJECTED, order, detail

    # expired / failed: only ever move a NOT-paid order, never touch a paid one (out-of-order delivery)
    with transaction.atomic():
        attempt = PaymentAttempt.objects.select_for_update().filter(stripe_session_id=session.get("id")).first()
        if not attempt:
            return StripeEvent.Status.IGNORED, None, "unknown session"
        order = Order.objects.select_for_update().get(pk=attempt.order_id)
        if attempt.status == PaymentAttempt.Status.PAID or order.payment_status == PaymentStatus.PAID:
            return StripeEvent.Status.IGNORED, order, "order already paid (out-of-order event)"
        failed = etype.endswith("failed")
        attempt.status = PaymentAttempt.Status.FAILED if failed else PaymentAttempt.Status.EXPIRED
        attempt.save(update_fields=["status", "updated_at"])
        still_open = order.payment_attempts.filter(status=PaymentAttempt.Status.OPEN).exists()
        if not still_open and order.payment_status == PaymentStatus.PENDING:
            order.transition_payment(PaymentStatus.FAILED if failed else PaymentStatus.UNPAID)
        order.log_event("payment_attempt_" + attempt.status, attempt.stripe_session_id)
        return StripeEvent.Status.PROCESSED, order, attempt.status

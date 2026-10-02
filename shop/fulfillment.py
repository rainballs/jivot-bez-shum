# shop/fulfillment.py
"""
Shipment creation (Econt) - the ONLY code path that creates labels.

Design (see report):
  * One payload builder (`build_label_for_order`) driven purely by database state; no request/session/override data.
  * The payment/COD decision comes from the VERIFIED payment state, never from the mutable `payment_method` choice:
      payment_status == PAID            -> prepaid label (no COD, sender pays Econt)
      method COD + customer confirmed   -> COD label (merchandise COD, recipient pays courier charges)
      anything else                     -> NotFulfillable (nothing is created)
  * attempt_shipment() commits a claim (IN_PROGRESS + ShipmentAttempt row) BEFORE calling Econt, calls Econt
    with no locks held, then persists the result. Every failure window ends in a visible state:
      crash before the call   -> stale claim -> UNKNOWN + review (sweeper)
      timeout / 5xx / garbage -> UNKNOWN + review (never auto-retried: Econt may have created it)
      provider rejection      -> FAILED + review
      request never sent      -> PENDING with bounded exponential backoff, then FAILED + review
      Econt OK, local save fails -> UNKNOWN + review, shipment number logged at CRITICAL
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .address import split_street_num
from .econt_client import (
    EcontAuthError,
    EcontClient,
    EcontNotSent,
    EcontOutcomeUnknown,
    EcontRejected,
    build_create_label_json,
    build_packing_list_from_order,
    summarize_payload,
)
from .models import (
    DeliveryMethod,
    Order,
    PaymentMethod,
    PaymentStatus,
    ShipmentAttempt,
    ShipmentStatus,
)

log = logging.getLogger("shop.fulfillment")

BACKOFF_SECONDS = [60, 300, 900, 3600]


class NotFulfillable(Exception):
    """The order is not in a state where a shipment may be created."""


@dataclass(frozen=True)
class ShipmentPlan:
    kind: str  # "prepaid" | "cod"
    cod_amount: Decimal
    cod_currency: str
    receiver_pays_delivery: bool


def plan_for_order(order: Order, *, for_create: bool) -> ShipmentPlan:
    """
    What must the label ask the recipient to pay?

      prepaid : Stripe collected merchandise AND delivery -> no COD, recipient owes nothing.
      cod     : merchandise is collected by the courier (COD = goods only);
                the recipient also pays the courier charges.

    for_create=False is used for quoting/validation BEFORE payment, from the customer's selection.
    """
    cur = "EUR"

    if order.payment_status == PaymentStatus.PAID:
        return ShipmentPlan("prepaid", Decimal("0"), cur, False)

    if order.payment_method == PaymentMethod.COD:
        if for_create and not order.cod_confirmed_at:
            raise NotFulfillable("COD order has not been confirmed by the customer")
        amount = Decimal(str(order.subtotal_eur or 0)).quantize(Decimal("0.01"))
        if amount <= 0:
            raise NotFulfillable("COD order with zero merchandise amount")
        return ShipmentPlan("cod", amount, cur, True)

    if for_create:
        raise NotFulfillable("card order without verified payment")
    return ShipmentPlan("prepaid", Decimal("0"), cur, False)


def build_label_for_order(order: Order, plan: ShipmentPlan) -> dict:
    """The single payload builder used for quote, validation and creation."""
    d = settings.ECONT["DEFAULTS"]
    to_office = order.delivery_method == DeliveryMethod.TO_OFFICE  # the delivery METHOD decides, not stale fields

    street, num = (order.receiver_street or "").strip(), (order.receiver_num or "").strip()
    if not to_office and not (street and num):  # legacy orders created before structured fields existed
        street, num = split_street_num(order.address_line or order.billing_street or "")

    postcode = (order.postal_code or order.billing_postcode or "").strip()
    city = (order.city or order.billing_city or "").strip()
    qty = int(order.quantity or 1)
    weight = max(0.8, round(0.4 * qty, 3))
    subtotal_eur = Decimal(str(order.subtotal_eur or 0))

    cod = plan.cod_amount if plan.kind == "cod" else Decimal("0")
    return build_create_label_json(
        sender_name=d["sender_name"],
        sender_phone=d["sender_phone"],
        sender_city=d["sender_city"],
        sender_address=d["sender_address"],
        sender_office_code=(d.get("sender_office") or None),
        receiver_name=order.full_name,
        receiver_phone=order.phone,
        receiver_city=city,
        receiver_office_code=(order.econt_office_code or None) if to_office else None,
        receiver_street=street,
        receiver_num=num,
        receiver_postcode=postcode,
        receiver_entrance=order.receiver_entrance or None,
        receiver_floor=order.receiver_floor or None,
        receiver_apartment=order.receiver_apartment or None,
        receiver_quarter=order.receiver_quarter or None,
        receiver_other=order.receiver_other or None,
        weight_kg=weight,
        parcels=1,
        cod_amount=cod,
        cod_currency=plan.cod_currency,
        declared_value=subtotal_eur,
        declared_currency="EUR",
        receiver_pays_delivery=plan.receiver_pays_delivery,
        label_format=d.get("label_format", "10x9"),
        cod_agreement_number=d.get("cod_agreement_number") if cod > 0 else None,
        invoice_num=f"{order.pk} {date.today().strftime('%d.%m.%y')}" if cod > 0 else None,
        sms_notification=True,
        # Off by default: Econt never echoes it back, we do not need it, and it is the one extra field that could
        # make e-Econt file the label outside "Пратки от мен" (see ECONT_SEND_ORDER_NUMBER).
        order_number=str(order.pk) if d.get("send_order_number") else None,
        holiday_delivery_day=settings.ECONT["DEFAULTS"].get("holiday_delivery_day") or None,
        packing_list=build_packing_list_from_order(order),
        packing_list_type="digital",
    )


def verify_label_intent(plan: ShipmentPlan, label: dict) -> list[str]:
    """Compare what Econt says it created with what we intended. Returns a list of problems."""
    problems: list[str] = []
    services = label.get("services") or []
    has_cd = any(isinstance(s, dict) and s.get("type") == "CD" for s in services)
    try:
        receiver_due = Decimal(str(label.get("receiverDueAmount") or 0))
    except Exception:
        receiver_due = Decimal("0")

    if plan.kind == "prepaid":
        if has_cd:
            problems.append("PREPAID order but Econt label contains a COD (CD) service")
        if receiver_due > 0:
            problems.append(f"PREPAID order but recipient is asked to pay {receiver_due} to the courier")
    else:
        if not has_cd:
            problems.append("COD order but Econt label has no COD (CD) service")
        if not receiver_due > 0:
            problems.append("COD order but recipient is not asked to pay the courier charges")
    return problems


def fulfillment_blocker(order: Order) -> str | None:
    """Reason why this order must NOT be shipped right now (None = OK)."""
    if order.econt_shipment_num:
        return "order already has a shipment number"
    if not (order.full_name or "").strip() or not (order.phone or "").strip():
        return "missing receiver name/phone"
    if not (order.city or order.billing_city or "").strip():
        return "missing receiver city"
    return None


# ------------------------------------------------------------------ state changes
def request_shipment(order_id: int) -> bool:
    """NONE -> PENDING (idempotent). Returns True if the order is now waiting for dispatch."""
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order_id)
        if order.shipment_status == ShipmentStatus.NONE:
            order.shipment_next_attempt_at = timezone.now()
            order.transition_shipment(ShipmentStatus.PENDING, extra_fields=["shipment_next_attempt_at"])
            return True
        return order.shipment_status == ShipmentStatus.PENDING


def dispatch_after_commit(order_id: int):
    """Best-effort immediate attempt once the surrounding transaction has committed.
    The durable fallback is `manage.py process_shipments` (swept every minute)."""
    if not getattr(settings, "SHIPMENT_INLINE_DISPATCH", True):
        return

    def _run():
        try:
            attempt_shipment(order_id)
        except Exception:
            log.exception("inline shipment dispatch failed for order %s (sweeper will retry)", order_id)

    transaction.on_commit(_run)


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=BACKOFF_SECONDS[min(max(attempts - 1, 0), len(BACKOFF_SECONDS) - 1)])


def attempt_shipment(order_id: int, *, interactive: bool = False) -> str:
    """
    Try to create the Econt shipment for one order. Returns a short outcome string:
    created | rejected | retry | unknown | skipped:<why> | blocked
    """
    now = timezone.now()

    # ---- phase 1: claim (short transaction, committed before any network call)
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order_id)
        if order.shipment_status != ShipmentStatus.PENDING:
            return f"skipped:{order.shipment_status}"
        if order.shipment_next_attempt_at and order.shipment_next_attempt_at > now:
            return "skipped:not_due"

        blocker = fulfillment_blocker(order)
        try:
            plan = plan_for_order(order, for_create=True)
            payload = None if blocker else build_label_for_order(order, plan)
        except (NotFulfillable, ValueError) as e:
            blocker = blocker or str(e)
            payload = None
        if blocker:
            order.econt_errors = f"Not submitted: {blocker}"
            order.shipment_claimed_at = None
            order.transition_shipment(ShipmentStatus.IN_PROGRESS, save=False)
            order.transition_shipment(ShipmentStatus.FAILED, extra_fields=["econt_errors", "shipment_claimed_at"])
            order.flag_review(f"Shipment not created: {blocker}")
            log.error("shipment blocked order=%s reason=%s", order.pk, blocker)
            return "blocked"

        order.shipment_attempts += 1
        order.shipment_claimed_at = now
        order.econt_cod_amount = plan.cod_amount if plan.kind == "cod" else Decimal("0")
        order.econt_cod_currency = plan.cod_currency
        order.econt_receiver_pays_delivery = plan.receiver_pays_delivery
        order.transition_shipment(
            ShipmentStatus.IN_PROGRESS,
            extra_fields=[
                "shipment_attempts", "shipment_claimed_at", "econt_cod_amount",
                "econt_cod_currency", "econt_receiver_pays_delivery",
            ],
        )
        attempt = ShipmentAttempt.objects.create(
            order=order, number=order.shipment_attempts,
            request_summary={**summarize_payload(payload), "plan": plan.kind},
        )
        attempts_so_far = order.shipment_attempts

    log.info("econt create start order=%s attempt=%s kind=%s", order_id, attempt.uid, plan.kind)

    # ---- phase 2: provider call, no DB locks held
    try:
        result = EcontClient().create_label(payload)
    except EcontNotSent as e:
        return _after_not_sent(order_id, attempt, attempts_so_far, str(e), now)
    except EcontAuthError as e:
        # configuration problem: retry with backoff (bounded) once credentials are fixed
        return _after_not_sent(order_id, attempt, attempts_so_far, f"auth: {e}", now, http_status=e.http_status)
    except EcontRejected as e:
        return _after_rejected(order_id, attempt, str(e), e.http_status, interactive)
    except EcontOutcomeUnknown as e:
        return _after_unknown(order_id, attempt, str(e), e.http_status)
    except Exception as e:  # programming error while talking to Econt: we cannot know what was sent
        log.exception("unexpected error during Econt create order=%s", order_id)
        return _after_unknown(order_id, attempt, f"unexpected {type(e).__name__}", None)

    # ---- phase 3: persist
    return _after_created(order_id, attempt, plan, result)


def _finish_attempt(attempt: ShipmentAttempt, outcome, *, http_status=None, error="", num="", response=None):
    ShipmentAttempt.objects.filter(pk=attempt.pk).update(
        outcome=outcome, finished_at=timezone.now(), http_status=http_status,
        error=(error or "")[:2000], shipment_num=num or "", response_summary=response or {},
    )


def _after_created(order_id, attempt, plan, result) -> str:
    num = result["shipment_num"]
    label = result["label"]
    problems = verify_label_intent(plan, label)
    response = {
        "receiverDueAmount": label.get("receiverDueAmount"),
        "senderDueAmount": label.get("senderDueAmount"),
        "totalPrice": label.get("totalPrice"),
        "currency": label.get("currency"),
        "services": [s.get("type") for s in (label.get("services") or []) if isinstance(s, dict)],
    }
    try:
        with transaction.atomic():
            order = Order.objects.select_for_update().get(pk=order_id)
            order.econt_shipment_num = num
            order.econt_label_url = (result.get("pdf_url") or "")[:500]
            order.econt_errors = None
            order.shipment_claimed_at = None
            order.shipment_next_attempt_at = None
            order.transition_shipment(
                ShipmentStatus.CREATED,
                extra_fields=[
                    "econt_shipment_num", "econt_label_url", "econt_errors",
                    "shipment_claimed_at", "shipment_next_attempt_at",
                ],
            )
            _finish_attempt(attempt, ShipmentAttempt.Outcome.CREATED, http_status=200, num=num, response=response)
            order.log_event("shipment_created", f"Econt shipment {num}", attempt=str(attempt.uid), plan=plan.kind)
            for p in problems:
                order.flag_review(f"Econt label {num}: {p}")
    except Exception as e:
        # Econt HAS the shipment but we failed to record it. Make that impossible to miss and impossible to
        # silently re-create: UNKNOWN is never retried automatically.
        log.critical(
            "ECONT SHIPMENT CREATED BUT NOT SAVED order=%s shipment_num=%s attempt=%s error=%s",
            order_id, num, attempt.uid, type(e).__name__,
        )
        reason = f"Econt created shipment {num} but saving it failed ({type(e).__name__}); do NOT create another label"
        try:
            _finish_attempt(attempt, ShipmentAttempt.Outcome.UNKNOWN, http_status=200, num=num,
                            error=reason, response=response)
            Order.objects.filter(pk=order_id, shipment_status=ShipmentStatus.IN_PROGRESS).update(
                shipment_status=ShipmentStatus.UNKNOWN, needs_review=True, review_reason=reason,
                econt_errors=reason,
            )
        except Exception:
            log.critical("could not even record unknown outcome for order=%s num=%s", order_id, num)
        return "unknown"

    if problems:
        log.error("econt label intent mismatch order=%s num=%s problems=%s", order_id, num, problems)
        _alert(order_id, "Econt label does not match the intended payment configuration", "; ".join(problems))
    log.info("econt created order=%s shipment=%s", order_id, num)
    return "created"


def _after_rejected(order_id, attempt, message, http_status, interactive) -> str:
    _finish_attempt(attempt, ShipmentAttempt.Outcome.REJECTED, http_status=http_status, error=message)
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order_id)
        order.econt_errors = f"Econt: {message}"[:2000]
        order.shipment_claimed_at = None
        order.transition_shipment(ShipmentStatus.FAILED, extra_fields=["econt_errors", "shipment_claimed_at"])
        if order.payment_status == PaymentStatus.PAID or not interactive:
            order.flag_review(f"Econt rejected the shipment: {message[:300]}")
    log.warning("econt rejected order=%s http=%s", order_id, http_status)
    if not interactive:
        _alert(order_id, "Econt rejected the shipment", message)
    return "rejected"


def _after_not_sent(order_id, attempt, attempts_so_far, message, now, http_status=None) -> str:
    _finish_attempt(attempt, ShipmentAttempt.Outcome.NOT_SENT, http_status=http_status, error=message)
    max_attempts = getattr(settings, "SHIPMENT_MAX_ATTEMPTS", 5)
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order_id)
        order.econt_errors = f"Econt unreachable/not sent: {message}"[:2000]
        order.shipment_claimed_at = None
        if attempts_so_far >= max_attempts:
            order.transition_shipment(ShipmentStatus.FAILED, extra_fields=["econt_errors", "shipment_claimed_at"])
            order.flag_review(f"Shipment retries exhausted after {attempts_so_far} attempts: {message[:200]}")
            exhausted = True
        else:
            order.shipment_next_attempt_at = now + _backoff(attempts_so_far)
            order.transition_shipment(
                ShipmentStatus.PENDING,
                extra_fields=["econt_errors", "shipment_claimed_at", "shipment_next_attempt_at"],
            )
            exhausted = False
    if exhausted:
        _alert(order_id, "Econt shipment retries exhausted", message)
    return "retry"


def _after_unknown(order_id, attempt, message, http_status) -> str:
    _finish_attempt(attempt, ShipmentAttempt.Outcome.UNKNOWN, http_status=http_status, error=message)
    reason = f"Econt outcome unknown ({message[:200]}): the shipment may exist in e-Econt; check before retrying"
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order_id)
        order.econt_errors = reason
        order.shipment_claimed_at = None
        order.transition_shipment(ShipmentStatus.UNKNOWN, extra_fields=["econt_errors", "shipment_claimed_at"])
        order.flag_review(reason)
    log.error("econt outcome UNKNOWN order=%s attempt=%s", order_id, attempt.uid)
    _alert(order_id, "Econt outcome unknown - manual check required", message)
    return "unknown"


def _alert(order_id: int, subject: str, detail: str):
    try:
        from .utils import send_admin_alert

        send_admin_alert(Order.objects.get(pk=order_id), subject, detail)
    except Exception:
        log.exception("could not send admin alert for order %s", order_id)


# ------------------------------------------------------------------ sweeper / manual resolution
def recover_stale_claims(now=None) -> int:
    """IN_PROGRESS claims older than the lease mean the process died mid-call: outcome is UNKNOWN."""
    now = now or timezone.now()
    cutoff = now - timedelta(seconds=getattr(settings, "SHIPMENT_STALE_AFTER_SECONDS", 600))
    ids = list(
        Order.objects.filter(shipment_status=ShipmentStatus.IN_PROGRESS, shipment_claimed_at__lt=cutoff)
        .values_list("pk", flat=True)
    )
    n = 0
    for pk in ids:
        with transaction.atomic():
            order = Order.objects.select_for_update().get(pk=pk)
            if order.shipment_status != ShipmentStatus.IN_PROGRESS:
                continue
            reason = "Process stopped during the Econt call; shipment may or may not exist in e-Econt"
            order.econt_errors = reason
            order.shipment_claimed_at = None
            order.transition_shipment(ShipmentStatus.UNKNOWN, extra_fields=["econt_errors", "shipment_claimed_at"])
            order.flag_review(reason)
            ShipmentAttempt.objects.filter(
                order=order, outcome=ShipmentAttempt.Outcome.STARTED
            ).update(outcome=ShipmentAttempt.Outcome.UNKNOWN, finished_at=now, error=reason)
        _alert(pk, "Stale Econt claim -> unknown outcome", "worker/process died during provider call")
        n += 1
    return n


def due_order_ids(limit: int = 50, now=None) -> list[int]:
    now = now or timezone.now()
    return list(
        Order.objects.filter(shipment_status=ShipmentStatus.PENDING, shipment_next_attempt_at__lte=now)
        .order_by("shipment_next_attempt_at")
        .values_list("pk", flat=True)[:limit]
    )


def adopt_existing_shipment(order_id: int, shipment_num: str, who: str = "operator") -> None:
    """Operator confirmed (in e-Econt) that shipment_num belongs to this order: record it, no new label."""
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order_id)
        if order.shipment_status != ShipmentStatus.UNKNOWN:
            raise ValueError("only UNKNOWN shipments can be adopted")
        order.econt_shipment_num = shipment_num.strip()
        order.econt_errors = None
        order.transition_shipment(ShipmentStatus.CREATED, extra_fields=["econt_shipment_num", "econt_errors"])
        order.log_event("shipment_adopted", f"{who} linked shipment {shipment_num}")


def allow_retry_after_manual_check(order_id: int, who: str = "operator") -> None:
    """Operator confirmed in e-Econt that NO shipment exists for this order: allow exactly one new attempt."""
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order_id)
        if order.shipment_status not in (ShipmentStatus.UNKNOWN, ShipmentStatus.FAILED):
            raise ValueError("only UNKNOWN/FAILED shipments can be re-queued")
        order.shipment_next_attempt_at = timezone.now()
        order.shipment_attempts = 0 if order.shipment_status == ShipmentStatus.FAILED else order.shipment_attempts
        order.transition_shipment(ShipmentStatus.PENDING, extra_fields=["shipment_next_attempt_at", "shipment_attempts"])
        order.log_event("shipment_requeued", f"{who} confirmed no shipment exists / fixed data")

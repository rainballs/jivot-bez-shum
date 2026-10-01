"""
READ-ONLY reconciliation report. It never writes to the database and never changes anything at Stripe or Econt
(it only calls Econt `getMyAWB` and Stripe `checkout.sessions.list`, both read operations, and only with --econt /
--stripe). Output contains order ids and statuses only - no names, e-mails, phones or addresses.

    python manage.py reconcile_orders                      # local consistency only (no network)
    python manage.py reconcile_orders --econt --stripe     # additionally compare with the providers
    python manage.py reconcile_orders --since 400 --csv report.csv

Evidence levels:
    LOCAL     inconsistency visible in our own database
    VERIFIED  confirmed by comparing with Econt / Stripe records
"""
import csv
from collections import Counter, defaultdict
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Count
from django.utils import timezone

from shop.models import (
    Order,
    PaymentMethod,
    PaymentStatus,
    ShipmentAttempt,
    ShipmentStatus,
)

LOCAL, VERIFIED = "LOCAL", "VERIFIED"


class Command(BaseCommand):
    help = "Read-only report of orders whose payment / Econt shipment state is inconsistent or needs attention."

    def add_arguments(self, parser):
        parser.add_argument("--since", type=int, default=365, help="look back N days (default 365)")
        parser.add_argument("--econt", action="store_true", help="compare with Econt getMyAWB (read-only)")
        parser.add_argument("--stripe", action="store_true", help="compare with Stripe Checkout Sessions (read-only)")
        parser.add_argument("--csv", help="write the findings to this CSV file")
        parser.add_argument("--include-ok", action="store_true", help="also list orders without findings")
        parser.add_argument("--exclude", default="", help="comma-separated order ids already handled (hidden from the report)")

    # ------------------------------------------------------------------ local checks
    def local_findings(self, o: Order, dup_nums: set, multi_created: set, now):
        f = []
        legacy_paid = o.paid or o.payment_status == PaymentStatus.PAID
        has_label = bool(o.econt_shipment_num)

        if legacy_paid and not has_label:
            if o.shipment_status == ShipmentStatus.UNKNOWN:
                pass  # reported below as UNKNOWN
            else:
                f.append(("PAID_NO_SHIPMENT", LOCAL, "paid order without an Econt shipment number"))
        if o.shipment_status == ShipmentStatus.FAILED:
            f.append(("SHIPMENT_FAILED", LOCAL, "shipment failed / retries exhausted"))
        if o.shipment_status == ShipmentStatus.UNKNOWN:
            f.append(("SHIPMENT_UNKNOWN", LOCAL, "Econt outcome unknown - check e-Econt before retrying"))
        if o.shipment_status == ShipmentStatus.IN_PROGRESS and o.shipment_claimed_at and \
                o.shipment_claimed_at < now - timedelta(seconds=settings.SHIPMENT_STALE_AFTER_SECONDS):
            f.append(("STALE_IN_PROGRESS", LOCAL, "claimed but never finished"))
        if o.shipment_status == ShipmentStatus.PENDING and o.shipment_next_attempt_at and \
                o.shipment_next_attempt_at < now - timedelta(minutes=15):
            f.append(("PENDING_OVERDUE", LOCAL, "waiting for dispatch for >15 min (is process_shipments running?)"))
        if legacy_paid and o.payment_method == PaymentMethod.COD:
            f.append(("PAID_BUT_METHOD_COD", LOCAL,
                      "marked paid (only Stripe sets paid) while payment_method is COD: label may request COD"))
        if legacy_paid and o.econt_cod_amount and o.econt_cod_amount > 0:
            f.append(("COD_REQUESTED_ON_PAID", LOCAL, f"recorded COD request {o.econt_cod_amount} {o.econt_cod_currency}"))
        if has_label and o.payment_method != PaymentMethod.COD and not legacy_paid:
            f.append(("LABEL_WITHOUT_PAYMENT", LOCAL, "card order has a shipment but no verified payment"))
        if has_label and o.econt_shipment_num in dup_nums:
            f.append(("DUPLICATE_SHIPMENT_NUM", LOCAL, "same shipment number on several orders"))
        if o.pk in multi_created:
            f.append(("MULTIPLE_LABELS_CREATED", LOCAL, "more than one successful createLabel attempt"))
        if o.needs_review:
            f.append(("FLAGGED", LOCAL, (o.review_reason or "").splitlines()[0][:120] if o.review_reason else "flagged"))
        return f

    # ------------------------------------------------------------------ provider checks
    def econt_map(self, orders):
        from shop.econt_client import EcontClient

        dates = [o.created_at.date() for o in orders if o.econt_shipment_num]
        if not dates:
            return {}
        start, end = min(dates) - timedelta(days=2), max(dates) + timedelta(days=30)
        client = EcontClient(timeout=(5, 30))
        out, page = {}, 1
        while True:
            body = client.my_awb(start, min(end, timezone.now().date() + timedelta(days=1)), page=page)
            for r in body.get("results") or []:
                out[str(r.get("shipmentNumber"))] = r
            if page >= int(body.get("totalPages") or 1):
                break
            page += 1
        return out

    def econt_findings(self, o, awb):
        f = []
        row = awb.get(str(o.econt_shipment_num))
        if row is None:
            return [("ECONT_NUMBER_NOT_FOUND", VERIFIED, "shipment number not in Econt's list for the period")]
        cd = Decimal(str(row.get("cdAmount") or 0))
        payer = str(row.get("courierServiceMasterPayer") or "").lower()
        paid_by_card = (o.paid or o.payment_status == PaymentStatus.PAID)
        if paid_by_card and cd > 0:
            f.append(("ECONT_COD_ON_PAID_ORDER", VERIFIED,
                      f"Econt requests COD {cd} {row.get('cdCurrency')} although the order was paid online"))
        if paid_by_card and payer == "receiver":
            f.append(("ECONT_RECEIVER_PAYS_ON_PAID_ORDER", VERIFIED,
                      "recipient is the courier-charge payer although delivery was paid online"))
        if o.payment_method == PaymentMethod.COD and not paid_by_card and cd <= 0:
            f.append(("ECONT_NO_COD_ON_COD_ORDER", VERIFIED, "COD order but Econt shows no COD amount"))
        elif o.payment_method == PaymentMethod.COD and not paid_by_card and o.subtotal_eur                 and abs(cd - Decimal(str(o.subtotal_eur))) > Decimal("0.05"):
            # getMyAWB reports amounts in EUR; Econt converts the BGN amount we send (verified on demo)
            f.append(("ECONT_COD_AMOUNT_MISMATCH", VERIFIED,
                      f"Econt COD {cd} EUR differs from goods value {o.subtotal_eur} EUR"))
        return f

    def stripe_findings(self, orders):
        import stripe

        paid_sessions = defaultdict(list)
        since = int((timezone.now() - timedelta(days=self.since)).timestamp())
        for s in stripe.checkout.Session.list(limit=100, created={"gte": since}, api_key=settings.STRIPE_SECRET_LIVE_KEY
                                              ).auto_paging_iter():
            oid = (s.get("metadata") or {}).get("order_id")
            if oid and s.get("payment_status") == "paid":
                paid_sessions[int(oid)].append(s)
        found = defaultdict(list)
        by_pk = {o.pk: o for o in orders}
        for oid, sessions in paid_sessions.items():
            o = by_pk.get(oid)
            if o is None:
                continue
            if not (o.paid or o.payment_status == PaymentStatus.PAID):
                found[oid].append(("STRIPE_PAID_LOCAL_UNPAID", VERIFIED, "Stripe has a paid session; order is not paid locally"))
            if len(sessions) > 1:
                found[oid].append(("STRIPE_MULTIPLE_PAYMENTS", VERIFIED, f"{len(sessions)} paid sessions for one order: refund extras"))
        for o in orders:
            if (o.paid or o.payment_status == PaymentStatus.PAID) and o.payment_method != PaymentMethod.COD \
                    and o.pk not in paid_sessions:
                found[o.pk].append(("LOCAL_PAID_NO_STRIPE_SESSION", VERIFIED, "paid locally but no paid Stripe session in period"))
            elif (o.paid or o.payment_status == PaymentStatus.PAID) and o.payment_method == PaymentMethod.COD \
                    and o.pk not in paid_sessions:
                found[o.pk].append(("PAID_FLAG_WITHOUT_STRIPE_PAYMENT", VERIFIED, "marked paid but no paid Stripe session"))
        return found

    # ------------------------------------------------------------------ main
    def handle(self, *args, **opts):
        self.since = opts["since"]
        now = timezone.now()
        excluded = {int(x) for x in opts["exclude"].split(",") if x.strip().isdigit()}
        orders = list(Order.objects.filter(created_at__gte=now - timedelta(days=self.since))
                      .exclude(pk__in=excluded).order_by("id"))

        dup_nums = set(
            Order.objects.exclude(econt_shipment_num__isnull=True).exclude(econt_shipment_num="")
            .values("econt_shipment_num").annotate(n=Count("id")).filter(n__gt=1)
            .values_list("econt_shipment_num", flat=True)
        )
        multi_created = set(
            ShipmentAttempt.objects.filter(outcome=ShipmentAttempt.Outcome.CREATED)
            .values("order_id").annotate(n=Count("id")).filter(n__gt=1).values_list("order_id", flat=True)
        )

        findings = defaultdict(list)
        for o in orders:
            findings[o.pk] += self.local_findings(o, dup_nums, multi_created, now)

        provider_notes = []
        if opts["econt"]:
            try:
                awb = self.econt_map(orders)
                for o in orders:
                    if o.econt_shipment_num:
                        findings[o.pk] += self.econt_findings(o, awb)
                provider_notes.append(f"Econt: compared {len(awb)} provider records")
            except Exception as e:
                provider_notes.append(f"Econt comparison FAILED ({type(e).__name__}): provider checks incomplete")
        if opts["stripe"]:
            try:
                for pk, items in self.stripe_findings(orders).items():
                    findings[pk] += items
                provider_notes.append("Stripe: compared paid Checkout Sessions")
            except Exception as e:
                provider_notes.append(f"Stripe comparison FAILED ({type(e).__name__}): provider checks incomplete")

        rows, counts = [], Counter()
        for o in orders:
            items = findings[o.pk]
            if not items and not opts["include_ok"]:
                continue
            for code, level, detail in (items or [("OK", LOCAL, "")]):
                counts[code] += 1
                rows.append({
                    "order_id": o.pk, "ref": str(o.public_id)[:8], "created": o.created_at.date().isoformat(),
                    "payment_method": o.payment_method, "payment_status": o.payment_status, "paid_flag": o.paid,
                    "shipment_status": o.shipment_status, "econt_shipment_num": o.econt_shipment_num or "",
                    "code": code, "evidence": level, "detail": detail,
                })

        self.stdout.write(f"Orders examined: {len(orders)} (last {self.since} days). READ-ONLY: nothing was modified.")
        for n in provider_notes:
            self.stdout.write(n)
        if not opts["econt"] and not opts["stripe"]:
            self.stdout.write("Provider comparison not requested: findings are LOCAL only (add --econt --stripe).")
        self.stdout.write("")
        for r in rows:
            self.stdout.write(
                f"#{r['order_id']:<6} {r['created']} {r['payment_method']:<6} pay={r['payment_status']:<7} "
                f"ship={r['shipment_status']:<11} {r['code']:<34} [{r['evidence']}] {r['detail']}"
            )
        self.stdout.write("")
        self.stdout.write("Summary: " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no findings"))

        if opts["csv"] and rows:
            with open(opts["csv"], "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)
            self.stdout.write(f"CSV written: {opts['csv']}")

"""
READ-ONLY: show what Econt itself has recorded for specific orders, next to what our database expects.
Only reads (Econt getMyAWB + getShipmentStatuses). Prints order ids, shipment numbers, amounts and statuses -
no names, phones or addresses.

    python manage.py econt_inspect 345 444 2018
"""
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from shop.econt_client import EcontClient, EcontError
from shop.models import Order


def _num(v):
    try:
        return Decimal(str(v if v not in (None, "") else 0))
    except Exception:
        return Decimal("0")


class Command(BaseCommand):
    help = "Read-only comparison of selected orders with the records in Econt."

    def add_arguments(self, parser):
        parser.add_argument("order_ids", nargs="+", type=int)

    def handle(self, *args, **opts):
        client = EcontClient(timeout=(5, 30))
        for oid in opts["order_ids"]:
            try:
                o = Order.objects.get(pk=oid)
            except Order.DoesNotExist:
                raise CommandError(f"no order {oid}")
            self.stdout.write(self.style.MIGRATE_HEADING(
                f"\n#{o.pk}  created {o.created_at:%Y-%m-%d}  method={o.payment_method}  "
                f"payment={o.payment_status}  shipment={o.shipment_status}"))
            self.stdout.write(f"  we expect : goods {o.subtotal_eur} EUR / {o.subtotal_bgn} BGN, "
                              f"shipping {o.shipping_eur} EUR, total {o.total_eur} EUR; "
                              f"COD requested (new orders only): {o.econt_cod_amount} {o.econt_cod_currency}")
            num = (o.econt_shipment_num or "").strip()
            if not num:
                self.stdout.write("  Econt     : no shipment number stored for this order")
                continue
            self.stdout.write(f"  shipment  : {num}")
            try:
                row = self._find(client, num, o.created_at)
                st = client.shipment_statuses([num])
            except EcontError as e:
                self.stdout.write(self.style.ERROR(f"  Econt query failed: {e}"))
                continue

            if row is None:
                self.stdout.write(self.style.WARNING(
                    "  Econt list : NOT FOUND for this period (cancelled/deleted label, created on another account "
                    "or on the demo server, or older than the search window)"))
            else:
                cd = _num(row.get("cdAmount"))
                self.stdout.write(
                    f"  Econt list : status='{row.get('status')}'  COD={cd} {row.get('cdCurrency') or ''}  "
                    f"delivery charges paid by: {row.get('courierServiceMasterPayer')}")
                if o.payment_method == "cod" and cd <= 0:
                    self.stdout.write(self.style.ERROR("               -> COD order but Econt does NOT ask for cash on delivery"))
                elif o.payment_method == "cod" and o.subtotal_eur and abs(cd - o.subtotal_eur) > Decimal("0.05"):
                    self.stdout.write(self.style.WARNING(
                        f"               -> COD amount differs from the goods value ({o.subtotal_eur} EUR)"))
                elif o.paid and cd > 0:
                    self.stdout.write(self.style.ERROR("               -> paid online but Econt asks for COD"))
                else:
                    self.stdout.write(self.style.SUCCESS("               -> consistent with our records"))

            for item in st.get("shipmentStatuses") or []:
                s = item.get("status") or {}
                if not s:
                    self.stdout.write(f"  Econt detail: {item.get('error') or 'no status returned'}")
                    continue
                self.stdout.write(
                    f"  Econt detail: delivered={s.get('deliveryTime') or '-'}  "
                    f"COD collected={s.get('cdCollectedAmount')} {s.get('cdCollectedCurrency') or ''} "
                    f"(at {s.get('cdCollectedTime') or '-'})  COD paid out={s.get('cdPaidAmount')} "
                    f"{s.get('cdPaidCurrency') or ''}  receiver still owes={s.get('receiverDueAmount')}")

    @staticmethod
    def _find(client, num, created_at):
        start = (created_at - timedelta(days=2)).date()
        end = min((created_at + timedelta(days=90)).date(), timezone.now().date() + timedelta(days=1))
        page = 1
        while True:
            body = client.my_awb(start, end, page=page)
            for r in body.get("results") or []:
                if str(r.get("shipmentNumber")) == num:
                    return r
            if page >= int(body.get("totalPages") or 1):
                return None
            page += 1

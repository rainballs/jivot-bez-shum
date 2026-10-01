"""
Operator tool for orders whose shipment state needs a HUMAN decision. DRY-RUN by default: nothing is changed
unless --yes is given, and it never contacts Econt or Stripe itself (the operator looked in e-Econt).

    # The shipment exists in e-Econt (e.g. after an unknown/timeout outcome): link it, create nothing.
    python manage.py resolve_shipment 123 --adopt 1051234567890 --yes

    # The operator checked e-Econt and there is NO label for this order: allow exactly one new attempt.
    # (works for UNKNOWN / FAILED orders, and for historical paid orders that never got a label)
    python manage.py resolve_shipment 123 --retry --confirm-no-label-in-econt --yes

A retry of a COD-paid-by-card mismatch is intentionally NOT offered: those labels already exist; fix them in
e-Econt (see the report) instead of creating another one.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from shop import fulfillment
from shop.models import Order, PaymentStatus, ShipmentStatus


class Command(BaseCommand):
    help = "Link an existing Econt shipment to an order, or re-queue a shipment after manual verification."

    def add_arguments(self, parser):
        parser.add_argument("order_id", type=int)
        parser.add_argument("--adopt", metavar="SHIPMENT_NUMBER")
        parser.add_argument("--retry", action="store_true")
        parser.add_argument("--confirm-no-label-in-econt", action="store_true")
        parser.add_argument("--yes", action="store_true", help="actually apply (default is a dry run)")

    def handle(self, *args, **o):
        try:
            order = Order.objects.get(pk=o["order_id"])
        except Order.DoesNotExist:
            raise CommandError("no such order")
        if bool(o["adopt"]) == bool(o["retry"]):
            raise CommandError("choose exactly one of --adopt NUMBER or --retry")

        self.stdout.write(
            f"order #{order.pk}: payment={order.payment_status}/{order.payment_method} "
            f"shipment={order.shipment_status} number={order.econt_shipment_num or '-'}"
        )
        if order.econt_shipment_num:
            raise CommandError("order already has a shipment number; refusing to create or replace it")

        if o["adopt"]:
            if order.shipment_status not in (ShipmentStatus.UNKNOWN, ShipmentStatus.FAILED, ShipmentStatus.NONE):
                raise CommandError(f"cannot adopt in shipment state {order.shipment_status}")
            plan = f"link shipment {o['adopt']} to order #{order.pk} (no new label)"
        else:
            if not o["confirm_no_label_in_econt"]:
                raise CommandError("--retry needs --confirm-no-label-in-econt (look the order up in e-Econt first)")
            paid = order.payment_status == PaymentStatus.PAID or order.paid
            if not paid and not order.cod_confirmed_at:
                raise CommandError("order is neither paid nor confirmed as COD: nothing may be shipped")
            plan = f"queue ONE new Econt attempt for order #{order.pk}"

        if not o["yes"]:
            self.stdout.write(f"[dry-run] would: {plan}. Re-run with --yes to apply.")
            return

        if o["adopt"]:
            # Operator override of the state machine (verified in e-Econt by a human). The DB constraint
            # `uniq_order_econt_shipment_num` still refuses a number that belongs to another order.
            from django.db import IntegrityError

            try:
                with transaction.atomic():
                    Order.objects.select_for_update().get(pk=order.pk)
                    Order.objects.filter(pk=order.pk).update(
                        econt_shipment_num=o["adopt"].strip(), econt_errors=None,
                        shipment_status=ShipmentStatus.CREATED, shipment_claimed_at=None,
                        shipment_next_attempt_at=None,
                    )
                    order.log_event("shipment_adopted", "operator linked an existing e-Econt shipment (command)")
            except IntegrityError:
                raise CommandError("that shipment number is already linked to another order")
        else:
            with transaction.atomic():
                order = Order.objects.select_for_update().get(pk=order.pk)
                if order.shipment_status == ShipmentStatus.NONE:
                    order.shipment_next_attempt_at = timezone.now()
                    order.transition_shipment(ShipmentStatus.PENDING, extra_fields=["shipment_next_attempt_at"])
                else:
                    fulfillment.allow_retry_after_manual_check(order.pk, who="command")
        self.stdout.write(self.style.SUCCESS(f"done: {plan}"))

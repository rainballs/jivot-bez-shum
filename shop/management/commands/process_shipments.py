"""
Durable dispatcher for Econt shipments. Schedule it every minute (cron / systemd timer / `--loop` under a
process manager). It is safe to run several copies at once: every attempt claims the order under a row lock.

    python manage.py process_shipments            # one sweep
    python manage.py process_shipments --loop     # keep running (sleeps --interval seconds between sweeps)
    python manage.py process_shipments --dry-run  # only report what it would do
"""
import logging
import time

from django.core.management.base import BaseCommand
from django.utils import timezone

from shop import fulfillment
from shop.models import Order, ShipmentStatus
from shop.utils import notify_pending_orders

log = logging.getLogger("shop.fulfillment")


class Command(BaseCommand):
    help = "Create Econt shipments for paid / confirmed orders that are waiting, and surface stuck ones."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=50)
        parser.add_argument("--loop", action="store_true")
        parser.add_argument("--interval", type=int, default=30)
        parser.add_argument("--dry-run", action="store_true")

    def sweep(self, limit, dry_run):
        now = timezone.now()
        if dry_run:
            stale = Order.objects.filter(shipment_status=ShipmentStatus.IN_PROGRESS).count()
            due = fulfillment.due_order_ids(limit, now)
            self.stdout.write(f"[dry-run] in_progress claims: {stale}; due for dispatch: {due}")
            return
        stale = fulfillment.recover_stale_claims(now)
        if stale:
            self.stdout.write(self.style.WARNING(f"{stale} stale claim(s) marked UNKNOWN (manual check needed)"))
        for pk in fulfillment.due_order_ids(limit, now):
            try:  # one broken order must never stop the others
                outcome = fulfillment.attempt_shipment(pk)
            except Exception:
                log.exception("attempt_shipment crashed for order %s", pk)
                self.stderr.write(f"order {pk}: ERROR (see log)")
                continue
            self.stdout.write(f"order {pk}: {outcome}")
        resent = notify_pending_orders()
        if resent:
            self.stdout.write(f"re-sent {resent} missing order e-mail(s)")

    def handle(self, *args, **opts):
        while True:
            try:
                self.sweep(opts["limit"], opts["dry_run"])
            except Exception:
                if not opts["loop"]:
                    raise
                log.exception("sweep failed; continuing")  # e.g. a database blip must not kill the worker
            if not opts["loop"]:
                break
            time.sleep(opts["interval"])

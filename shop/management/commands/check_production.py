"""
Production health / configuration check. Read-only (no writes, no calls to Stripe or Econt).

    python manage.py check_production            # exit code 1 if any FAIL, warnings do not fail
    python manage.py check_production --strict   # warnings fail too
    python manage.py check_production --config-only   # configuration only, before migrating

Use it at the end of every deployment and from a monitor/cron (e.g. every 10 minutes): it detects the failures that
used to be silent - unconfigured providers, unapplied migrations, a stopped shipment scheduler, stuck orders.
"""
from datetime import timedelta
from urllib.parse import urlparse

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from shop.models import Order, PaymentStatus, ShipmentStatus

OK, WARN, FAIL = "OK", "WARN", "FAIL"


def collect_checks(now=None, config_only=False):
    now = now or timezone.now()
    out = []

    def add(level, name, detail=""):
        out.append((level, name, detail))

    # ---- configuration
    add(FAIL if settings.DEBUG else OK, "DEBUG is off")
    bad_key = settings.SECRET_KEY in ("", "dev-secret-key") or len(settings.SECRET_KEY) < 30
    add(FAIL if bad_key else OK, "SECRET_KEY is set and strong")

    base = settings.ECONT.get("BASE_URL", "")
    add(FAIL if "demo.econt.com" in base or not base else OK, "Econt points to the LIVE server", urlparse(base).netloc)
    add(OK if settings.ECONT.get("USER") and settings.ECONT.get("PASS") else FAIL, "Econt credentials present")
    d = settings.ECONT.get("DEFAULTS", {})
    sender_ok = all(d.get(k) for k in ("sender_name", "sender_phone", "sender_city", "sender_address"))
    add(OK if sender_ok else FAIL, "Econt sender data present")

    key = getattr(settings, "STRIPE_SECRET_LIVE_KEY", "")
    add(OK if key.startswith(("sk_live", "rk_live")) else FAIL, "Stripe secret key is a LIVE key")
    add(OK if str(getattr(settings, "STRIPE_WEBHOOK_SECRET", "")).startswith("whsec_") else FAIL,
        "Stripe webhook signing secret configured")

    site = urlparse(settings.SITE_URL)
    add(OK if site.scheme == "https" and site.netloc else FAIL, "SITE_URL is https", settings.SITE_URL)
    hosts = settings.ALLOWED_HOSTS
    add(FAIL if "*" in hosts else OK, "ALLOWED_HOSTS has no wildcard")
    add(OK if site.netloc in hosts else FAIL, "SITE_URL host is in ALLOWED_HOSTS")
    add(OK if settings.SESSION_COOKIE_SECURE and settings.CSRF_COOKIE_SECURE else FAIL, "Secure session/CSRF cookies")
    add(OK if getattr(settings, "SECURE_PROXY_SSL_HEADER", None) else WARN,
        "Proxy HTTPS header trusted (USE_X_FORWARDED_PROTO)",
        "needed if nginx terminates TLS and Django must know the request was https")
    mail_ok = "smtp" in settings.EMAIL_BACKEND.lower() and settings.EMAIL_HOST_USER and settings.EMAIL_HOST_PASSWORD
    add(OK if mail_ok else FAIL, "E-mail (SMTP) configured")
    add(OK if getattr(settings, "ORDER_NOTIFY_EMAIL", None) else FAIL, "Admin notification address set")
    add(OK if getattr(settings, "SECURE_HSTS_SECONDS", 0) else WARN, "HSTS enabled")

    if config_only:
        return out

    # ---- database / migrations
    try:
        executor = MigrationExecutor(connection)
        pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
        add(OK if not pending else FAIL, "All migrations applied", ", ".join(m.name for m, _ in pending))
    except Exception as e:  # pragma: no cover
        add(FAIL, "Database reachable", type(e).__name__)
        return out

    # ---- order pipeline (the things that used to fail silently)
    stuck_after = now - timedelta(minutes=10)
    pending_old = Order.objects.filter(
        shipment_status=ShipmentStatus.PENDING, shipment_next_attempt_at__lt=stuck_after).count()
    add(OK if not pending_old else FAIL, "Shipment scheduler is running (no overdue pending shipments)",
        f"{pending_old} overdue: is `process_shipments` scheduled every minute?" if pending_old else "")

    stale = Order.objects.filter(shipment_status=ShipmentStatus.IN_PROGRESS, shipment_claimed_at__lt=stuck_after).count()
    add(OK if not stale else WARN, "No stale in-progress shipment claims", str(stale) if stale else "")

    unknown = Order.objects.filter(shipment_status=ShipmentStatus.UNKNOWN).count()
    add(OK if not unknown else WARN, "No shipments with unknown Econt outcome", f"{unknown} need a manual check" if unknown else "")

    failed_paid = Order.objects.filter(shipment_status=ShipmentStatus.FAILED).count()
    add(OK if not failed_paid else WARN, "No failed shipments", f"{failed_paid} need attention" if failed_paid else "")

    paid_no_label = Order.objects.filter(
        payment_status=PaymentStatus.PAID, shipment_status=ShipmentStatus.NONE,
        paid_at__isnull=False, paid_at__lt=stuck_after).count()
    add(OK if not paid_no_label else FAIL, "No card-paid orders without a shipment request",
        str(paid_no_label) if paid_no_label else "")

    review = Order.objects.filter(needs_review=True).count()
    add(OK if not review else WARN, "No orders flagged for review", f"{review} flagged (admin: filter 'needs review')" if review else "")

    unsent = Order.objects.filter(notified_at__isnull=True).filter(
        payment_status=PaymentStatus.PAID, created_at__gt=now - timedelta(days=3)).count()
    add(OK if not unsent else WARN, "Order e-mails sent", f"{unsent} unsent" if unsent else "")
    return out


class Command(BaseCommand):
    help = "Read-only production configuration and order-pipeline health check."

    def add_arguments(self, parser):
        parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
        parser.add_argument("--config-only", action="store_true",
                            help="only the configuration checks (no database): safe BEFORE running migrations")

    def handle(self, *args, **opts):
        results = collect_checks(config_only=opts["config_only"])
        for level, name, detail in results:
            line = f"[{level:4}] {name}" + (f"  - {detail}" if detail else "")
            style = {OK: self.style.SUCCESS, WARN: self.style.WARNING, FAIL: self.style.ERROR}[level]
            self.stdout.write(style(line))
        fails = sum(1 for r in results if r[0] == FAIL)
        warns = sum(1 for r in results if r[0] == WARN)
        self.stdout.write(f"\n{len(results)} checks: {fails} FAIL, {warns} WARN")
        if fails or (opts["strict"] and warns):
            raise SystemExit(1)

from datetime import timedelta
from io import StringIO

from django.conf import settings
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from shop.management.commands.check_production import FAIL, OK, WARN, collect_checks
from shop.models import Order, PaymentStatus, ShipmentStatus

from .base import ShopTestCase, make_order

GOOD = dict(
    DEBUG=False, SECRET_KEY="x" * 50, SITE_URL="https://filyaka.com", ALLOWED_HOSTS=["filyaka.com", "www.filyaka.com"],
    STRIPE_SECRET_LIVE_KEY="sk_live_abc", STRIPE_WEBHOOK_SECRET="whsec_abc", SESSION_COOKIE_SECURE=True,
    CSRF_COOKIE_SECURE=True, SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"), SECURE_HSTS_SECONDS=3600,
    EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend", EMAIL_HOST_USER="u", EMAIL_HOST_PASSWORD="p",
)


def good_econt():
    return {**settings.ECONT, "BASE_URL": "https://ee.econt.com/services"}


def levels(results):
    return {name: level for level, name, _ in results}


class CheckProductionTests(ShopTestCase):
    def test_a_correct_production_configuration_passes_everything(self):
        with override_settings(ECONT=good_econt(), **GOOD):
            res = collect_checks()
        self.assertEqual([r for r in res if r[0] != OK], [])

    def test_every_misconfiguration_that_used_to_fail_silently_is_a_failure(self):
        bad = {**GOOD, "DEBUG": True, "STRIPE_SECRET_LIVE_KEY": "sk_test_x", "STRIPE_WEBHOOK_SECRET": "",
               "SITE_URL": "http://localhost", "SECRET_KEY": "dev-secret-key"}
        econt = {**settings.ECONT, "BASE_URL": "https://demo.econt.com/ee/services"}
        with override_settings(ECONT=econt, **bad):
            lv = levels(collect_checks())
        for name in ("DEBUG is off", "SECRET_KEY is set and strong", "Econt points to the LIVE server",
                     "Stripe secret key is a LIVE key", "Stripe webhook signing secret configured", "SITE_URL is https"):
            self.assertEqual(lv[name], FAIL, name)

    def test_stopped_scheduler_is_detected(self):
        o = make_order(payment_status=PaymentStatus.PAID, paid=True)
        Order.objects.filter(pk=o.pk).update(shipment_status=ShipmentStatus.PENDING,
                                             shipment_next_attempt_at=timezone.now() - timedelta(minutes=30))
        with override_settings(ECONT=good_econt(), **GOOD):
            lv = levels(collect_checks())
        self.assertEqual(lv["Shipment scheduler is running (no overdue pending shipments)"], FAIL)

    def test_paid_order_nobody_queued_is_detected_and_attention_states_warn(self):
        o = make_order(payment_status=PaymentStatus.PAID, paid=True)
        Order.objects.filter(pk=o.pk).update(paid_at=timezone.now() - timedelta(hours=1))
        u = make_order(payment_status=PaymentStatus.PAID, paid=True)
        Order.objects.filter(pk=u.pk).update(shipment_status=ShipmentStatus.UNKNOWN, needs_review=True)
        with override_settings(ECONT=good_econt(), **GOOD):
            lv = levels(collect_checks())
        self.assertEqual(lv["No card-paid orders without a shipment request"], FAIL)
        self.assertEqual(lv["No shipments with unknown Econt outcome"], WARN)
        self.assertEqual(lv["No orders flagged for review"], WARN)

    def test_command_exit_codes(self):
        out = StringIO()
        with override_settings(ECONT=good_econt(), **GOOD):
            call_command("check_production", stdout=out)  # no exception = exit 0
        self.assertIn("0 FAIL", out.getvalue())
        with override_settings(ECONT=good_econt(), **{**GOOD, "DEBUG": True}):
            with self.assertRaises(SystemExit) as cm:
                call_command("check_production", stdout=StringIO())
        self.assertEqual(cm.exception.code, 1)

    def test_it_is_read_only(self):
        make_order(payment_status=PaymentStatus.PAID, paid=True)
        before = list(Order.objects.values_list("pk", "payment_status", "shipment_status", "needs_review"))
        with override_settings(ECONT=good_econt(), **GOOD):
            collect_checks()
        self.assertEqual(before, list(Order.objects.values_list("pk", "payment_status", "shipment_status", "needs_review")))
        self.assertEqual(self.econt.calls, [])

    def test_config_only_mode_skips_database_checks(self):
        with override_settings(ECONT=good_econt(), **GOOD):
            names = [n for _, n, _ in collect_checks(config_only=True)]
        self.assertIn("Stripe secret key is a LIVE key", names)
        self.assertNotIn("All migrations applied", names)
        self.assertNotIn("Shipment scheduler is running (no overdue pending shipments)", names)

"""Data migration of historical orders, and the read-only reconciliation report."""
import csv
import os
import tempfile
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

from shop.models import Order, PaymentMethod, PaymentStatus, ShipmentAttempt, ShipmentStatus

from .base import FakeResponse, ShopTestCase, make_order

FROM = [("shop", "0005_alter_order_payment_method")]
TO = [("shop", "0006_order_state_payments_fulfillment")]


class LegacyBackfillMigrationTests(TransactionTestCase):
    def migrate(self, targets):
        ex = MigrationExecutor(connection)
        ex.loader.build_graph()
        ex.migrate(targets)
        return ex.loader.project_state(targets).apps

    def tearDown(self):
        ex = MigrationExecutor(connection)
        ex.loader.build_graph()
        ex.migrate(ex.loader.graph.leaf_nodes())
        super().tearDown()

    def legacy(self, apps, **kw):
        Order = apps.get_model("shop", "Order")
        base = dict(full_name="X", email="x@example.com", phone="1", quantity=1)
        base.update(kw)
        return Order.objects.create(**base)

    def test_existing_orders_get_unique_public_ids_and_neutral_state(self):
        old = self.migrate(FROM)
        a = self.legacy(old, payment_method="card", paid=True, econt_shipment_num="111")
        b = self.legacy(old, payment_method="cod", paid=False, econt_shipment_num="222")
        c = self.legacy(old, payment_method="card", paid=False)
        d = self.legacy(old, payment_method="cod", paid=True)  # paid flag + COD: suspicious legacy state, kept as is
        new = self.migrate(TO)
        O = new.get_model("shop", "Order")
        rows = {o.pk: o for o in O.objects.all()}
        self.assertEqual(len({o.public_id for o in rows.values()}), 4)  # one uuid PER row
        self.assertEqual((rows[a.pk].payment_status, rows[a.pk].shipment_status), ("paid", "created"))
        self.assertEqual((rows[b.pk].payment_status, rows[b.pk].shipment_status), ("unpaid", "created"))
        self.assertIsNotNone(rows[b.pk].cod_confirmed_at)
        self.assertEqual((rows[c.pk].payment_status, rows[c.pk].shipment_status), ("unpaid", "none"))
        self.assertEqual(rows[d.pk].payment_status, "paid")
        # nothing was changed or flagged: business data and the legacy flag are untouched
        self.assertTrue(rows[a.pk].paid and rows[d.pk].paid and not rows[c.pk].paid)
        self.assertEqual(rows[d.pk].payment_method, "cod")
        self.assertFalse(any(o.needs_review for o in rows.values()))

    def test_historical_duplicate_shipment_numbers_stop_the_migration_with_a_clear_message(self):
        old = self.migrate(FROM)
        a = self.legacy(old, econt_shipment_num="DUP")
        b = self.legacy(old, econt_shipment_num="DUP")
        with self.assertRaises(RuntimeError) as cm:
            self.migrate(TO)
        self.assertIn("DUP", str(cm.exception))
        self.assertIn(str(a.pk), str(cm.exception))
        self.assertIn(str(b.pk), str(cm.exception))
        # operator resolves the historical duplicates, then the migration goes through
        old = self.migrate(FROM)
        old.get_model("shop", "Order").objects.filter(pk=b.pk).update(econt_shipment_num="DUP-2")
        self.migrate(TO)


class ReconcileCommandTests(ShopTestCase):
    def run_cmd(self, *args):
        out = StringIO()
        call_command("reconcile_orders", *args, stdout=out)
        return out.getvalue()

    def snapshot(self):
        return list(Order.objects.order_by("pk").values_list("pk", "payment_status", "shipment_status", "needs_review",
                                                             "econt_shipment_num", "paid"))

    def seed(self):
        legacy_paid_cod = make_order(method=PaymentMethod.COD, paid=True, econt_shipment_num="S-COD")
        paid_no_label = make_order(payment_status=PaymentStatus.PAID, paid=True)
        failed = make_order(payment_status=PaymentStatus.PAID, paid=True, shipment_status=ShipmentStatus.FAILED)
        unknown = make_order(payment_status=PaymentStatus.PAID, paid=True, shipment_status=ShipmentStatus.UNKNOWN)
        label_no_pay = make_order(method=PaymentMethod.CARD, econt_shipment_num="S-FREE")
        fine = make_order(payment_status=PaymentStatus.PAID, paid=True, econt_shipment_num="S-OK",
                          shipment_status=ShipmentStatus.CREATED)
        return legacy_paid_cod, paid_no_label, failed, unknown, label_no_pay, fine

    def test_local_report_flags_the_right_orders_and_writes_nothing(self):
        cod_paid, no_label, failed, unknown, free, fine = self.seed()
        before = self.snapshot()
        out = self.run_cmd()
        self.assertEqual(self.snapshot(), before)  # READ-ONLY
        self.assertEqual(self.econt.calls, [])  # no network without --econt
        self.assertIn("findings are LOCAL only", out)
        self.assertRegex(out, rf"#{cod_paid.pk}\s.*PAID_BUT_METHOD_COD")
        self.assertRegex(out, rf"#{no_label.pk}\s.*PAID_NO_SHIPMENT")
        self.assertRegex(out, rf"#{failed.pk}\s.*SHIPMENT_FAILED")
        self.assertRegex(out, rf"#{unknown.pk}\s.*SHIPMENT_UNKNOWN")
        self.assertRegex(out, rf"#{free.pk}\s.*LABEL_WITHOUT_PAYMENT")
        self.assertNotRegex(out, rf"#{fine.pk}\s")

    def test_report_contains_no_personal_data(self):
        make_order(full_name="Секретно Име", email="secret@example.com", phone="+359 777 000 111",
                   method=PaymentMethod.COD, paid=True, econt_shipment_num="S1")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "r.csv")
            out = self.run_cmd("--csv", path)
            text = out + open(path, encoding="utf-8").read()
        for secret in ("Секретно", "secret@example.com", "777 000", "ул. Витоша"):
            self.assertNotIn(secret, text)

    def test_duplicate_shipment_numbers_detected(self):
        a, b = make_order(), make_order()
        Order.objects.filter(pk=a.pk).update(econt_shipment_num="DUP")
        # the DB constraint prevents this now; historical rows are detected by the migration pre-check instead.
        from django.db import IntegrityError, transaction

        with self.assertRaises(IntegrityError), transaction.atomic():
            Order.objects.filter(pk=b.pk).update(econt_shipment_num="DUP")

    def test_econt_verification_finds_cod_on_a_card_paid_order_and_distinguishes_evidence(self):
        paid_card = make_order(payment_status=PaymentStatus.PAID, paid=True, econt_shipment_num="S-BAD",
                               shipment_status=ShipmentStatus.CREATED)
        ok = make_order(payment_status=PaymentStatus.PAID, paid=True, econt_shipment_num="S-GOOD",
                        shipment_status=ShipmentStatus.CREATED)
        missing = make_order(payment_status=PaymentStatus.PAID, paid=True, econt_shipment_num="S-GONE",
                             shipment_status=ShipmentStatus.CREATED)
        self.econt.mode_queue["getMyAWB"] = [FakeResponse(200, {"page": 1, "totalPages": 1, "results": [
            {"shipmentNumber": "S-BAD", "cdAmount": 25, "cdCurrency": "€", "courierServiceMasterPayer": "receiver"},
            {"shipmentNumber": "S-GOOD", "cdAmount": 0, "courierServiceMasterPayer": "sender"},
        ]})]
        before = self.snapshot()
        out = self.run_cmd("--econt")
        self.assertEqual(self.snapshot(), before)
        self.assertRegex(out, rf"#{paid_card.pk}\s.*ECONT_COD_ON_PAID_ORDER\s+\[VERIFIED\]")
        self.assertRegex(out, rf"#{paid_card.pk}\s.*ECONT_RECEIVER_PAYS_ON_PAID_ORDER")
        self.assertRegex(out, rf"#{missing.pk}\s.*ECONT_NUMBER_NOT_FOUND")
        self.assertNotRegex(out, rf"#{ok.pk}\s")
        # only read endpoints were used
        self.assertEqual({m for m, _ in self.econt.calls}, {"getMyAWB"})

    def test_econt_failure_is_reported_not_hidden(self):
        make_order(payment_status=PaymentStatus.PAID, paid=True, econt_shipment_num="S1")
        import requests

        self.econt.mode_queue["getMyAWB"] = [requests.exceptions.ReadTimeout("x")]
        out = self.run_cmd("--econt")
        self.assertIn("provider checks incomplete", out)

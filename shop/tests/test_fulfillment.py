"""Econt outcomes: every failure window must end in a visible, non-duplicating state."""
import logging
from datetime import timedelta
from io import StringIO
from unittest import mock

import requests
import urllib3
from django.core import mail
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.utils import timezone

from shop import fulfillment
from shop.econt_client import EcontClient, EcontOutcomeUnknown, EcontRejected, flatten_errors
from shop.models import Order, PaymentMethod, PaymentStatus, ShipmentAttempt, ShipmentStatus

from .base import FakeResponse, ShopTestCase, econt_error_body, make_order


def paid_order(**kw):
    o = make_order(**kw)
    o.payment_status = PaymentStatus.PAID
    o.paid = True
    o.save(update_fields=["payment_status", "paid"])
    return o


def queue_and_attempt(case, order):
    fulfillment.request_shipment(order.pk)
    return fulfillment.attempt_shipment(order.pk)


class ClientParsingTests(ShopTestCase):
    def test_flatten_errors_finds_nested_reason_when_top_level_message_is_blank(self):
        self.assertIn("Улицата", flatten_errors(econt_error_body()))

    def test_http_517_json_error_is_a_definitive_rejection_with_the_real_reason(self):
        self.econt.queue.append(FakeResponse(517, econt_error_body()))
        with self.assertRaises(EcontRejected) as cm:
            EcontClient().create_label({"x": 1})
        self.assertIn("Улицата", str(cm.exception))  # the old client reported only "HTTP 517 Unknown Status"

    def test_http_200_with_error_body_is_not_success(self):
        self.econt.queue.append(FakeResponse(200, econt_error_body("Невалиден офис")))
        with self.assertRaises(EcontRejected):
            EcontClient().create_label({"x": 1})

    def test_http_200_without_shipment_number_is_not_success(self):
        self.econt.queue.append(FakeResponse(200, {"label": {"pdfURL": "x"}}))
        with self.assertRaises(EcontOutcomeUnknown):
            EcontClient().create_label({"x": 1})

    def test_non_json_and_5xx_are_unknown_outcomes(self):
        for resp in (FakeResponse(502, text="<html>Bad gateway</html>"), FakeResponse(500, {"oops": 1}),
                     FakeResponse(200, text="")):
            self.econt.queue.append(resp)
            with self.assertRaises(EcontOutcomeUnknown):
                EcontClient().create_label({"x": 1})


class ShipmentOutcomeTests(ShopTestCase):
    def test_success_persists_reference_status_and_attempt(self):
        o = paid_order()
        self.assertEqual(queue_and_attempt(self, o), "created")
        o.refresh_from_db()
        self.assertEqual(o.shipment_status, ShipmentStatus.CREATED)
        self.assertTrue(o.econt_shipment_num.startswith("1051"))
        self.assertEqual(o.econt_label_url, "https://ee.econt.com/pdf/label.pdf")
        self.assertIsNone(o.econt_errors)
        att = o.shipment_attempts_log.get()
        self.assertEqual(att.outcome, ShipmentAttempt.Outcome.CREATED)
        self.assertEqual(att.shipment_num, o.econt_shipment_num)
        self.assertNotIn("name", str(att.request_summary).lower())  # no personal data in the audit summary

    def test_validation_error_is_failed_visible_and_not_retried(self):
        o = paid_order()
        self.econt.queue.append(FakeResponse(517, econt_error_body()))
        self.assertEqual(queue_and_attempt(self, o), "rejected")
        o.refresh_from_db()
        self.assertEqual(o.shipment_status, ShipmentStatus.FAILED)
        self.assertTrue(o.needs_review)
        self.assertIn("Улицата", o.econt_errors)
        self.assertEqual(o.shipment_attempts_log.get().outcome, ShipmentAttempt.Outcome.REJECTED)
        self.assertEqual(fulfillment.due_order_ids(), [])  # not auto-retried
        self.assertTrue(any("rejected" in m.subject.lower() for m in mail.outbox))

    def test_http_200_business_error_is_failed_not_created(self):
        o = paid_order()
        self.econt.queue.append(FakeResponse(200, econt_error_body("Невалиден офис")))
        self.assertEqual(queue_and_attempt(self, o), "rejected")
        o.refresh_from_db()
        self.assertEqual(o.shipment_status, ShipmentStatus.FAILED)
        self.assertFalse(o.econt_shipment_num)

    def test_auth_error_retries_with_backoff_then_exhausts_visibly(self):
        o = paid_order()
        fulfillment.request_shipment(o.pk)
        for i in range(5):
            self.econt.queue.append(FakeResponse(401, {"message": "unauthorized"}))
        outcomes = []
        for i in range(5):
            Order.objects.filter(pk=o.pk).update(shipment_next_attempt_at=timezone.now() - timedelta(seconds=1))
            outcomes.append(fulfillment.attempt_shipment(o.pk))
        self.assertEqual(outcomes, ["retry"] * 5)
        o.refresh_from_db()
        self.assertEqual(o.shipment_status, ShipmentStatus.FAILED)  # bounded: 5 attempts
        self.assertTrue(o.needs_review)
        self.assertIn("retries exhausted", o.review_reason)

    def test_backoff_not_due_is_skipped(self):
        o = paid_order()
        fulfillment.request_shipment(o.pk)
        self.econt.queue.append(FakeResponse(401, {}))
        self.assertEqual(fulfillment.attempt_shipment(o.pk), "retry")
        self.assertEqual(fulfillment.attempt_shipment(o.pk), "skipped:not_due")
        self.assertEqual(len(self.econt.creates()), 1)

    def test_request_never_sent_retries_and_later_succeeds_once(self):
        o = paid_order()
        reason = urllib3.exceptions.NewConnectionError(None, "refused")
        exc = requests.exceptions.ConnectionError(urllib3.exceptions.MaxRetryError(None, "u", reason))
        self.econt.queue.append(exc)
        self.assertEqual(queue_and_attempt(self, o), "retry")
        o.refresh_from_db()
        self.assertEqual(o.shipment_status, ShipmentStatus.PENDING)
        self.assertEqual(o.shipment_attempts_log.get().outcome, ShipmentAttempt.Outcome.NOT_SENT)
        Order.objects.filter(pk=o.pk).update(shipment_next_attempt_at=timezone.now())
        self.assertEqual(fulfillment.attempt_shipment(o.pk), "created")
        self.assertEqual(len(self.econt.creates()), 2)
        o.refresh_from_db()
        self.assertEqual(o.shipment_status, ShipmentStatus.CREATED)

    def test_timeout_is_unknown_flagged_and_never_retried_automatically(self):
        o = paid_order()
        self.econt.queue.append(requests.exceptions.ReadTimeout("slow"))
        self.assertEqual(queue_and_attempt(self, o), "unknown")
        o.refresh_from_db()
        self.assertEqual(o.shipment_status, ShipmentStatus.UNKNOWN)
        self.assertTrue(o.needs_review)
        self.assertEqual(fulfillment.due_order_ids(), [])
        self.assertEqual(fulfillment.attempt_shipment(o.pk), "skipped:unknown")
        call_command("process_shipments", stdout=StringIO())
        self.assertEqual(len(self.econt.creates()), 1)  # exactly one create ever sent
        self.assertTrue(any("unknown" in m.subject.lower() for m in mail.outbox))

    def test_manual_confirmation_is_required_to_retry_an_unknown_outcome(self):
        o = paid_order()
        self.econt.queue.append(requests.exceptions.ReadTimeout("slow"))
        queue_and_attempt(self, o)
        fulfillment.allow_retry_after_manual_check(o.pk, who="test")
        self.assertEqual(fulfillment.attempt_shipment(o.pk), "created")
        self.assertEqual(len(self.econt.creates()), 2)

    def test_unknown_can_be_resolved_by_adopting_the_existing_shipment(self):
        o = paid_order()
        self.econt.queue.append(requests.exceptions.ReadTimeout("slow"))
        queue_and_attempt(self, o)
        fulfillment.adopt_existing_shipment(o.pk, "1051555555555", who="test")
        o.refresh_from_db()
        self.assertEqual((o.shipment_status, o.econt_shipment_num), (ShipmentStatus.CREATED, "1051555555555"))
        self.assertEqual(len(self.econt.creates()), 1)

    def test_econt_accepted_but_local_save_fails_is_unknown_never_duplicated(self):
        o = paid_order()
        fulfillment.request_shipment(o.pk)
        real = Order.transition_shipment

        def boom(self, new, *a, **k):
            if new == ShipmentStatus.CREATED:
                raise RuntimeError("database connection lost")
            return real(self, new, *a, **k)

        with mock.patch.object(Order, "transition_shipment", boom), self.assertLogs("shop.fulfillment", "CRITICAL") as logs:
            self.assertEqual(fulfillment.attempt_shipment(o.pk), "unknown")
        number = self.econt.creates() and Order.objects.get(pk=o.pk).shipment_attempts_log.get().shipment_num
        self.assertTrue(number)  # the provider reference was preserved
        self.assertTrue(any(number in line for line in logs.output))  # and logged at CRITICAL
        o.refresh_from_db()
        self.assertEqual(o.shipment_status, ShipmentStatus.UNKNOWN)
        self.assertTrue(o.needs_review)
        self.assertIn("do NOT create another", o.review_reason)
        self.assertEqual(fulfillment.attempt_shipment(o.pk), "skipped:unknown")
        self.assertEqual(len(self.econt.creates()), 1)

    def test_worker_dies_after_claim_before_provider_answer(self):
        o = paid_order()
        fulfillment.request_shipment(o.pk)
        with mock.patch.object(EcontClient, "create_label", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                fulfillment.attempt_shipment(o.pk)
        o.refresh_from_db()
        self.assertEqual(o.shipment_status, ShipmentStatus.IN_PROGRESS)  # claim is durable
        self.assertEqual(fulfillment.attempt_shipment(o.pk), "skipped:in_progress")  # nobody double-sends meanwhile
        self.assertEqual(fulfillment.recover_stale_claims(), 0)  # lease not expired yet
        Order.objects.filter(pk=o.pk).update(shipment_claimed_at=timezone.now() - timedelta(hours=1))
        self.assertEqual(fulfillment.recover_stale_claims(), 1)
        o.refresh_from_db()
        self.assertEqual(o.shipment_status, ShipmentStatus.UNKNOWN)
        self.assertTrue(o.needs_review)
        self.assertEqual(o.shipment_attempts_log.get().outcome, ShipmentAttempt.Outcome.UNKNOWN)

    def test_crash_before_commit_leaves_nothing_queued(self):
        """Payment persisted + 'pending' queued in one transaction: if it rolls back, neither exists."""
        o = make_order()
        try:
            with transaction.atomic():
                fulfillment.request_shipment(o.pk)
                raise RuntimeError("crash before commit")
        except RuntimeError:
            pass
        o.refresh_from_db()
        self.assertEqual(o.shipment_status, ShipmentStatus.NONE)

    def test_repeated_dispatch_and_retries_never_create_duplicate_labels(self):
        o = paid_order()
        fulfillment.request_shipment(o.pk)
        results = [fulfillment.attempt_shipment(o.pk) for _ in range(4)]
        self.assertEqual(results[0], "created")
        self.assertTrue(all(r.startswith("skipped") for r in results[1:]))
        self.assertEqual(len(self.econt.creates()), 1)
        fulfillment.request_shipment(o.pk)  # even an explicit re-request of a created order is a no-op
        self.assertEqual(fulfillment.attempt_shipment(o.pk), "skipped:created")

    def test_database_refuses_duplicate_shipment_numbers_and_created_without_number(self):
        a, b = paid_order(), paid_order()
        Order.objects.filter(pk=a.pk).update(econt_shipment_num="X1", shipment_status="created")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Order.objects.filter(pk=b.pk).update(econt_shipment_num="X1", shipment_status="created")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Order.objects.filter(pk=b.pk).update(shipment_status="created", econt_shipment_num="")

    def test_invalid_transitions_are_rejected(self):
        from shop.models import InvalidTransition

        o = paid_order()
        with self.assertRaises(InvalidTransition):
            o.transition_shipment(ShipmentStatus.CREATED)  # NONE -> CREATED
        o.transition_payment(PaymentStatus.PAID)
        with self.assertRaises(InvalidTransition):
            o.transition_payment(PaymentStatus.UNPAID)  # PAID is terminal

    def test_incomplete_data_is_blocked_visibly_not_sent(self):
        o = paid_order(phone="")
        fulfillment.request_shipment(o.pk)
        self.assertEqual(fulfillment.attempt_shipment(o.pk), "blocked")
        o.refresh_from_db()
        self.assertEqual(o.shipment_status, ShipmentStatus.FAILED)
        self.assertTrue(o.needs_review)
        self.assertEqual(self.econt.creates(), [])


class ProcessShipmentsCommandTests(ShopTestCase):
    def test_sweeper_creates_queued_orders_and_dry_run_changes_nothing(self):
        o = paid_order()
        fulfillment.request_shipment(o.pk)
        out = StringIO()
        call_command("process_shipments", "--dry-run", stdout=out)
        self.assertEqual(self.econt.creates(), [])
        self.assertEqual(Order.objects.get(pk=o.pk).shipment_status, ShipmentStatus.PENDING)
        call_command("process_shipments", stdout=out)
        self.assertEqual(Order.objects.get(pk=o.pk).shipment_status, ShipmentStatus.CREATED)

    def test_two_overlapping_sweeps_do_not_double_send(self):
        o = paid_order()
        fulfillment.request_shipment(o.pk)
        call_command("process_shipments", stdout=StringIO())
        call_command("process_shipments", stdout=StringIO())
        self.assertEqual(len(self.econt.creates()), 1)


class ResolveShipmentCommandTests(ShopTestCase):
    def call(self, *args):
        from django.core.management.base import CommandError

        out = StringIO()
        call_command("resolve_shipment", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_is_the_default_and_changes_nothing(self):
        o = paid_order()
        Order.objects.filter(pk=o.pk).update(shipment_status=ShipmentStatus.UNKNOWN)
        self.assertIn("dry-run", self.call(str(o.pk), "--adopt", "1051000000777"))
        self.assertIn("dry-run", self.call(str(o.pk), "--retry", "--confirm-no-label-in-econt"))
        o.refresh_from_db()
        self.assertEqual((o.shipment_status, o.econt_shipment_num), (ShipmentStatus.UNKNOWN, None))

    def test_adopt_links_without_creating_and_cannot_reuse_a_number(self):
        from django.core.management.base import CommandError

        a, b = paid_order(), paid_order()
        self.call(str(a.pk), "--adopt", "1051000000777", "--yes")
        a.refresh_from_db()
        self.assertEqual((a.shipment_status, a.econt_shipment_num), (ShipmentStatus.CREATED, "1051000000777"))
        with self.assertRaises(CommandError):
            self.call(str(b.pk), "--adopt", "1051000000777", "--yes")
        self.assertEqual(self.econt.creates(), [])

    def test_retry_requires_explicit_confirmation_and_a_paid_or_confirmed_order(self):
        from django.core.management.base import CommandError

        paid = paid_order()
        with self.assertRaises(CommandError):
            self.call(str(paid.pk), "--retry", "--yes")  # no --confirm-no-label-in-econt
        unpaid = make_order(method=PaymentMethod.CARD)
        with self.assertRaises(CommandError):
            self.call(str(unpaid.pk), "--retry", "--confirm-no-label-in-econt", "--yes")

    def test_retry_for_a_historical_paid_order_without_label_queues_exactly_one_attempt(self):
        o = paid_order()  # legacy: paid, shipment_status none, no label
        self.call(str(o.pk), "--retry", "--confirm-no-label-in-econt", "--yes")
        self.assertEqual(Order.objects.get(pk=o.pk).shipment_status, ShipmentStatus.PENDING)
        call_command("process_shipments", stdout=StringIO())
        call_command("process_shipments", stdout=StringIO())
        self.assertEqual(len(self.econt.creates()), 1)

    def test_refuses_orders_that_already_have_a_shipment(self):
        from django.core.management.base import CommandError

        o = paid_order()
        Order.objects.filter(pk=o.pk).update(econt_shipment_num="X", shipment_status="created")
        with self.assertRaises(CommandError):
            self.call(str(o.pk), "--retry", "--confirm-no-label-in-econt", "--yes")


class SweeperRobustnessTests(ShopTestCase):
    def test_one_crashing_order_does_not_stop_the_others(self):
        a, b = paid_order(), paid_order()
        fulfillment.request_shipment(a.pk)
        fulfillment.request_shipment(b.pk)
        real = fulfillment.attempt_shipment

        def flaky(pk, **kw):
            if pk == a.pk:
                raise KeyError("sender_name")
            return real(pk, **kw)

        out, err = StringIO(), StringIO()
        with mock.patch("shop.fulfillment.attempt_shipment", flaky):
            call_command("process_shipments", stdout=out, stderr=err)
        self.assertIn(f"order {a.pk}: ERROR", err.getvalue())
        self.assertEqual(Order.objects.get(pk=b.pk).shipment_status, ShipmentStatus.CREATED)

    def test_loop_mode_survives_a_failing_sweep(self):
        from shop.management.commands.process_shipments import Command

        calls = {"n": 0}

        def sweep(self_, limit, dry):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("database connection lost")
            if calls["n"] == 3:
                raise SystemExit  # stop the loop in the test

        with mock.patch.object(Command, "sweep", sweep), mock.patch("time.sleep"):
            with self.assertRaises(SystemExit):
                call_command("process_shipments", "--loop", stdout=StringIO())
        self.assertEqual(calls["n"], 3)  # kept going after the first failure

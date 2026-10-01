"""Stripe webhook / payment verification. Signatures are real; Stripe API calls are mocked."""
import json
from unittest import mock

import stripe
from django.core import mail
from django.test import Client
from django.utils import timezone

from shop import payments
from shop.models import (
    Order,
    PaymentAttempt,
    PaymentMethod,
    PaymentStatus,
    ShipmentAttempt,
    ShipmentStatus,
    StripeEvent,
)

from .base import ShopTestCase, make_attempt, make_order, sign, stripe_event, stripe_session


class WebhookSecurityTests(ShopTestCase):
    def setUp(self):
        super().setUp()
        self.order = make_order()
        self.attempt = make_attempt(self.order)
        self.event = stripe_event(stripe_session(self.order, attempt=self.attempt))

    def test_invalid_signature_rejected_and_nothing_changes(self):
        r = self.post_webhook(self.event, secret="whsec_wrong")
        self.assertEqual(r.status_code, 400)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, PaymentStatus.UNPAID)
        self.assertEqual(StripeEvent.objects.count(), 0)

    def test_missing_signature_header_rejected(self):
        r = self.client.post("/pay/stripe/webhook/", data=json.dumps(self.event), content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_body_modified_after_signing_rejected(self):
        payload = json.dumps(self.event).encode()
        sig = sign(payload)
        tampered = payload.replace(b"checkout.session.completed", b"checkout.session.completeX")
        r = self.client.post("/pay/stripe/webhook/", data=tampered, content_type="application/json",
                             HTTP_STRIPE_SIGNATURE=sig)
        self.assertEqual(r.status_code, 400)

    def test_get_not_allowed(self):
        self.assertEqual(self.client.get("/pay/stripe/webhook/").status_code, 405)

    def test_webhook_works_without_csrf_token_but_normal_posts_still_need_one(self):
        c = Client(enforce_csrf_checks=True)
        self.assertEqual(self.post_webhook(self.event, client=c).status_code, 200)
        self.assertEqual(c.post("/checkout/confirm-cod/").status_code, 403)
        self.assertEqual(c.post("/pay/stripe/create-session/").status_code, 403)
        self.assertEqual(c.post("/checkout/save-inline/", {"quantity": "2"}).status_code, 403)


class WebhookPaymentTests(ShopTestCase):
    def setUp(self):
        super().setUp()
        self.order = make_order(method=PaymentMethod.CARD)
        self.order.payment_status = PaymentStatus.PENDING
        self.order.save()
        self.attempt = make_attempt(self.order)

    def deliver(self, event):
        with self.captureOnCommitCallbacks(execute=True):
            return self.post_webhook(event)

    def completed(self, **kw):
        return stripe_event(stripe_session(self.order, attempt=self.attempt, **kw))

    def test_success_without_browser_return_pays_and_ships_prepaid(self):
        """No thank_you request anywhere in this test: payment + label come from the webhook alone."""
        r = self.deliver(self.completed(pi="pi_test_abc"))
        self.assertEqual(r.status_code, 200)
        o = Order.objects.get(pk=self.order.pk)
        self.assertEqual(o.payment_status, PaymentStatus.PAID)
        self.assertTrue(o.paid)
        self.assertEqual(o.stripe_payment_intent_id, "pi_test_abc")
        self.assertEqual(o.paid_amount_minor, self.attempt.amount_minor)
        self.assertEqual(o.shipment_status, ShipmentStatus.CREATED)
        self.assertTrue(o.econt_shipment_num)
        sent = self.econt.creates()
        self.assertEqual(len(sent), 1)
        self.assertNotIn("cdAmount", sent[0].get("services", {}))
        self.assertNotIn("paymentReceiverMethod", sent[0])
        self.assertEqual(sent[0]["receiverAddress"]["num"], "12")  # the old webhook path sent no street number

    def test_card_paid_but_stored_method_is_cod_is_corrected_and_still_prepaid(self):
        """Reported bug #2: customer switched to COD in another tab, the Stripe session was still payable."""
        Order.objects.filter(pk=self.order.pk).update(payment_method=PaymentMethod.COD)
        self.deliver(self.completed())
        o = Order.objects.get(pk=self.order.pk)
        self.assertEqual(o.payment_method, PaymentMethod.CARD)  # verified fact wins over the mutable choice
        self.assertNotIn("cdAmount", self.econt.creates()[0].get("services", {}))
        self.assertNotIn("paymentReceiverMethod", self.econt.creates()[0])

    def test_duplicate_delivery_is_processed_once(self):
        ev = self.completed()
        self.assertEqual(self.deliver(ev).status_code, 200)
        self.assertEqual(self.deliver(ev).status_code, 200)
        self.assertEqual(self.deliver(ev).status_code, 200)
        self.assertEqual(len(self.econt.creates()), 1)
        self.assertEqual(StripeEvent.objects.filter(event_id=ev["id"]).count(), 1)
        self.assertEqual(len(mail.outbox), 2)  # admin + customer, once

    def test_same_session_under_two_event_ids_is_idempotent(self):
        s = stripe_session(self.order, attempt=self.attempt, pi="pi_test_same")
        self.deliver(stripe_event(s))
        self.deliver(stripe_event(s, etype="checkout.session.async_payment_succeeded"))
        self.assertEqual(len(self.econt.creates()), 1)
        self.assertFalse(Order.objects.get(pk=self.order.pk).needs_review)

    def test_out_of_order_expired_after_completed_does_not_unpay(self):
        self.deliver(self.completed())
        self.deliver(stripe_event(stripe_session(self.order, attempt=self.attempt, payment_status="unpaid"),
                                  etype="checkout.session.expired"))
        o = Order.objects.get(pk=self.order.pk)
        self.assertEqual(o.payment_status, PaymentStatus.PAID)
        self.assertEqual(o.shipment_status, ShipmentStatus.CREATED)

    def test_expired_then_late_completed_still_pays(self):
        self.deliver(stripe_event(stripe_session(self.order, attempt=self.attempt, payment_status="unpaid"),
                                  etype="checkout.session.expired"))
        self.assertEqual(Order.objects.get(pk=self.order.pk).payment_status, PaymentStatus.UNPAID)
        self.deliver(self.completed())
        self.assertEqual(Order.objects.get(pk=self.order.pk).payment_status, PaymentStatus.PAID)

    def test_unpaid_session_completed_event_does_not_pay(self):
        self.deliver(self.completed(payment_status="unpaid"))
        o = Order.objects.get(pk=self.order.pk)
        self.assertEqual(o.payment_status, PaymentStatus.PENDING)
        self.assertEqual(self.econt.creates(), [])

    def test_amount_mismatch_rejected_and_flagged(self):
        r = self.deliver(self.completed(amount=100))
        self.assertEqual(r.status_code, 200)  # acknowledged (retrying cannot help) ...
        o = Order.objects.get(pk=self.order.pk)
        self.assertNotEqual(o.payment_status, PaymentStatus.PAID)  # ... but NOT applied
        self.assertTrue(o.needs_review)  # ... and visible
        self.assertEqual(StripeEvent.objects.get().status, StripeEvent.Status.REJECTED)
        self.assertEqual(self.econt.creates(), [])

    def test_currency_mismatch_rejected(self):
        self.deliver(self.completed(currency="bgn"))
        self.assertNotEqual(Order.objects.get(pk=self.order.pk).payment_status, PaymentStatus.PAID)

    def test_metadata_order_mismatch_rejected(self):
        other = make_order()
        self.deliver(self.completed(metadata_order_id=other.pk))
        self.assertNotEqual(Order.objects.get(pk=self.order.pk).payment_status, PaymentStatus.PAID)
        self.assertNotEqual(Order.objects.get(pk=other.pk).payment_status, PaymentStatus.PAID)

    def test_unknown_order_is_acknowledged_without_crashing(self):
        s = stripe_session(self.order, attempt=self.attempt, session_id="cs_test_unknown", metadata_order_id=99999)
        s["amount_total"] = 1
        r = self.deliver(stripe_event(s))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(StripeEvent.objects.get().status, StripeEvent.Status.REJECTED)

    def test_event_from_other_stripe_mode_is_ignored(self):
        self.deliver(stripe_event(stripe_session(self.order, attempt=self.attempt), livemode=True))
        self.assertNotEqual(Order.objects.get(pk=self.order.pk).payment_status, PaymentStatus.PAID)
        self.assertEqual(StripeEvent.objects.get().status, StripeEvent.Status.IGNORED)

    def test_second_payment_for_same_order_is_flagged_not_reapplied(self):
        self.deliver(self.completed(pi="pi_test_first"))
        a2 = make_attempt(self.order)
        self.deliver(stripe_event(stripe_session(self.order, attempt=a2, pi="pi_test_second")))
        o = Order.objects.get(pk=self.order.pk)
        self.assertEqual(o.stripe_payment_intent_id, "pi_test_first")
        self.assertTrue(o.needs_review)
        self.assertIn("SECOND payment", o.review_reason)
        self.assertEqual(len(self.econt.creates()), 1)

    def test_processing_error_returns_500_then_retry_succeeds(self):
        ev = self.completed()
        with mock.patch("shop.payments.record_session_payment", side_effect=RuntimeError("db down")):
            r = self.deliver(ev)
        self.assertEqual(r.status_code, 500)
        self.assertEqual(StripeEvent.objects.get().status, StripeEvent.Status.ERROR)
        self.assertEqual(Order.objects.get(pk=self.order.pk).payment_status, PaymentStatus.PENDING)
        r = self.deliver(ev)  # Stripe retries the same event
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Order.objects.get(pk=self.order.pk).payment_status, PaymentStatus.PAID)
        self.assertEqual(StripeEvent.objects.get().status, StripeEvent.Status.PROCESSED)

    def test_email_failure_does_not_roll_back_payment_or_shipment(self):
        with mock.patch("shop.utils.send_mail", side_effect=OSError("smtp down")):
            r = self.deliver(self.completed())
        self.assertEqual(r.status_code, 200)
        o = Order.objects.get(pk=self.order.pk)
        self.assertEqual(o.payment_status, PaymentStatus.PAID)
        self.assertEqual(o.shipment_status, ShipmentStatus.CREATED)

    def test_econt_down_does_not_fail_the_webhook_and_leaves_pending_for_the_sweeper(self):
        import requests

        self.econt.queue.append(requests.exceptions.ConnectTimeout("no route"))
        r = self.deliver(self.completed())
        self.assertEqual(r.status_code, 200)  # Stripe must not be told "failed" because Econt is down
        o = Order.objects.get(pk=self.order.pk)
        self.assertEqual(o.payment_status, PaymentStatus.PAID)
        self.assertEqual(o.shipment_status, ShipmentStatus.PENDING)
        self.assertGreater(o.shipment_next_attempt_at, timezone.now())


class SwitchingAndLatePaymentTests(ShopTestCase):
    """Payment-method switching and a late Stripe success after COD was confirmed."""

    def test_late_card_success_after_cod_label_exists_is_flagged_and_creates_no_second_label(self):
        order = make_order(method=PaymentMethod.CARD)
        attempt = make_attempt(order)  # open Stripe session the customer never finished
        # customer switches to COD and confirms (label requested with COD)
        Order.objects.filter(pk=order.pk).update(payment_method=PaymentMethod.COD, cod_confirmed_at=timezone.now())
        from shop import fulfillment

        fulfillment.request_shipment(order.pk)
        self.assertEqual(fulfillment.attempt_shipment(order.pk), "created")
        self.assertIn("cdAmount", self.econt.creates()[0]["services"])

        # ... then the old Stripe session is paid
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.post_webhook(stripe_event(stripe_session(order, attempt=attempt))).status_code, 200)

        o = Order.objects.get(pk=order.pk)
        self.assertEqual(o.payment_status, PaymentStatus.PAID)
        self.assertTrue(o.needs_review)
        self.assertIn("AFTER a shipment", o.review_reason)
        self.assertEqual(len(self.econt.creates()), 1)  # no second label

    def test_card_success_while_cod_confirmed_but_not_yet_shipped_gives_prepaid_label(self):
        order = make_order(method=PaymentMethod.CARD)
        attempt = make_attempt(order)
        Order.objects.filter(pk=order.pk).update(payment_method=PaymentMethod.COD, cod_confirmed_at=timezone.now())
        from shop import fulfillment

        fulfillment.request_shipment(order.pk)  # PENDING, not attempted yet
        with self.captureOnCommitCallbacks(execute=True):
            self.post_webhook(stripe_event(stripe_session(order, attempt=attempt)))
        sent = self.econt.creates()
        self.assertEqual(len(sent), 1)
        self.assertNotIn("cdAmount", sent[0].get("services", {}))  # paid wins: no COD
        self.assertNotIn("paymentReceiverMethod", sent[0])
        self.assertTrue(Order.objects.get(pk=order.pk).needs_review)  # operator still told about the oddity

    def test_switching_to_cod_expires_the_open_stripe_session(self):
        order = make_order(method=PaymentMethod.CARD, payment_status=PaymentStatus.PENDING)
        attempt = make_attempt(order)
        client = self.session_for(order)
        with mock.patch("stripe.checkout.Session.expire") as expire:
            r = client.post("/checkout/save-inline/", {"payment_method": "cod"})
        self.assertEqual(r.status_code, 200)
        expire.assert_called_once()
        self.assertEqual(expire.call_args[0][0], attempt.stripe_session_id)
        order.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual(order.payment_method, PaymentMethod.COD)
        self.assertEqual(order.payment_status, PaymentStatus.UNPAID)
        self.assertEqual(attempt.status, PaymentAttempt.Status.SUPERSEDED)

    def test_cannot_switch_when_the_session_turns_out_to_be_paid(self):
        order = make_order(method=PaymentMethod.CARD, payment_status=PaymentStatus.PENDING)
        attempt = make_attempt(order)
        client = self.session_for(order)
        paid_session = stripe_session(order, attempt=attempt)
        with mock.patch("stripe.checkout.Session.expire", side_effect=stripe.InvalidRequestError("complete", "id")), \
                mock.patch("stripe.checkout.Session.retrieve", return_value=paid_session), \
                self.captureOnCommitCallbacks(execute=True):
            r = client.post("/checkout/save-inline/", {"payment_method": "cod"})
        self.assertEqual(r.status_code, 409)  # refused: it is already paid
        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.PAID)
        self.assertEqual(order.payment_method, PaymentMethod.CARD)


class ExpectedAmountTests(ShopTestCase):
    def test_amount_is_goods_snapshot_plus_quoted_shipping_in_euro_cents(self):
        order = make_order(qty=3)
        self.assertEqual(payments.expected_amount_minor(order), 3 * 1278 + 594)

    def test_line_items_match_expected_amount(self):
        order = make_order(qty=2)
        items = payments.build_line_items(order)
        total = sum(i["price_data"]["unit_amount"] * i["quantity"] for i in items)
        self.assertEqual(total, payments.expected_amount_minor(order))
        self.assertTrue(all(i["price_data"]["currency"] == "eur" for i in items))


class PaymentGuardTests(ShopTestCase):
    """The service layer enforces the rules itself (the view checks are not the only line of defence)."""

    def start(self, order):
        with mock.patch("stripe.checkout.Session.create") as create:
            with self.assertRaises(payments.PaymentNotAllowed):
                payments.create_checkout_session(order.pk, "https://shop.test")
        create.assert_not_called()
        self.assertEqual(PaymentAttempt.objects.count(), 0)

    def test_refuses_unquoted_delivery(self):
        self.start(make_order(quoted=False))

    def test_refuses_incomplete_address(self):
        self.start(make_order(receiver_num=""))

    def test_refuses_cod_orders(self):
        self.start(make_order(method=PaymentMethod.COD))

    def test_refuses_already_paid_orders(self):
        self.start(make_order(payment_status=PaymentStatus.PAID, paid=True))

    def test_refuses_orders_already_confirmed_as_cod(self):
        self.start(make_order(cod_confirmed_at=timezone.now()))


class ExpireSafetyTests(ShopTestCase):
    def test_a_session_that_could_not_be_expired_and_is_still_open_keeps_the_order_locked(self):
        order = make_order(method=PaymentMethod.CARD, payment_status=PaymentStatus.PENDING)
        attempt = make_attempt(order)
        still_open = {"id": attempt.stripe_session_id, "status": "open", "payment_status": "unpaid"}
        with mock.patch("stripe.checkout.Session.expire", side_effect=stripe.InvalidRequestError("odd", "id")), \
                mock.patch("stripe.checkout.Session.retrieve", return_value=still_open):
            self.assertFalse(payments.release_pending_payment(order.pk))
        order.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.PENDING)  # still locked: it may yet be paid
        self.assertEqual(attempt.status, PaymentAttempt.Status.OPEN)

    def test_a_session_stripe_reports_expired_is_released(self):
        order = make_order(method=PaymentMethod.CARD, payment_status=PaymentStatus.PENDING)
        attempt = make_attempt(order)
        with mock.patch("stripe.checkout.Session.expire", side_effect=stripe.InvalidRequestError("expired", "id")), \
                mock.patch("stripe.checkout.Session.retrieve",
                           return_value={"id": attempt.stripe_session_id, "status": "expired", "payment_status": "unpaid"}):
            self.assertTrue(payments.release_pending_payment(order.pk))
        self.assertEqual(Order.objects.get(pk=order.pk).payment_status, PaymentStatus.UNPAID)


class NotificationRetryTests(ShopTestCase):
    def test_a_failed_send_is_retried_by_the_sweeper_and_sent_once(self):
        from io import StringIO

        from django.core.management import call_command

        order = make_order(payment_status=PaymentStatus.PAID, paid=True)
        with mock.patch("shop.utils.send_mail", side_effect=OSError("smtp down")):
            from shop.utils import notify_order_accepted

            self.assertFalse(notify_order_accepted(order, event="paid"))
        self.assertIsNone(Order.objects.get(pk=order.pk).notified_at)  # claim released
        self.assertEqual(len(mail.outbox), 0)
        call_command("process_shipments", stdout=StringIO())  # SMTP is back
        self.assertEqual(len(mail.outbox), 2)
        call_command("process_shipments", stdout=StringIO())
        self.assertEqual(len(mail.outbox), 2)  # not again

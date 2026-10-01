"""
Real concurrency tests. They depend on PostgreSQL row locks (select_for_update), which SQLite does not provide,
so they are SKIPPED unless run with:   TEST_DB=postgres python manage.py test shop.tests.test_concurrency \
                                          --settings=Filip.test_settings
(the DB_* variables of .env must point at a throw-away PostgreSQL server: the test database is created and destroyed).
"""
import threading
import unittest
from unittest import mock

import requests
from django.core import mail
from django.db import close_old_connections, connection
from django.test import Client, TransactionTestCase

from shop import fulfillment
from shop.models import Order, OrderEvent, PaymentStatus, ShipmentStatus, StripeEvent

from .base import EcontFake, make_attempt, make_order, make_product, stripe_event, stripe_session, sign, WEBHOOK_SECRET

import json

N = 8


def run_parallel(fn, n=N):
    barrier = threading.Barrier(n)
    results, errors = [], []

    def worker():
        try:
            barrier.wait(timeout=10)
            results.append(fn())
        except Exception as e:  # pragma: no cover - reported by the assertions
            errors.append(e)
        finally:
            close_old_connections()
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(n)]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    return results, errors


@unittest.skipUnless(connection.vendor == "postgresql", "needs PostgreSQL (row-level locks)")
class ConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.econt = EcontFake()
        p = mock.patch.object(requests.Session, "post", autospec=True, side_effect=self.econt)
        p.start()
        self.addCleanup(p.stop)
        make_product()

    def test_parallel_webhook_deliveries_of_the_same_event_pay_and_ship_exactly_once(self):
        order = make_order(payment_status=PaymentStatus.PENDING)
        attempt = make_attempt(order)
        event = stripe_event(stripe_session(order, attempt=attempt))
        payload = json.dumps(event).encode()
        sig = sign(payload)

        def deliver():
            return Client().post("/pay/stripe/webhook/", data=payload, content_type="application/json",
                                 HTTP_STRIPE_SIGNATURE=sig).status_code

        results, errors = run_parallel(deliver)
        self.assertEqual(errors, [])
        self.assertTrue(all(r == 200 for r in results), results)
        o = Order.objects.get(pk=order.pk)
        self.assertEqual(o.payment_status, PaymentStatus.PAID)
        self.assertEqual(o.shipment_status, ShipmentStatus.CREATED)
        self.assertEqual(len(self.econt.creates()), 1)
        self.assertEqual(StripeEvent.objects.filter(event_id=event["id"]).count(), 1)
        # the payment itself was recorded exactly once (this is what the row lock in record_session_payment buys)
        self.assertEqual(OrderEvent.objects.filter(order=order, kind="payment_confirmed").count(), 1)
        self.assertEqual(len(mail.outbox), 2)

    def test_parallel_shipment_attempts_create_one_label(self):
        order = make_order(payment_status=PaymentStatus.PAID, paid=True)
        fulfillment.request_shipment(order.pk)
        results, errors = run_parallel(lambda: fulfillment.attempt_shipment(order.pk))
        self.assertEqual(errors, [])
        self.assertEqual(results.count("created"), 1, results)
        self.assertEqual(len(self.econt.creates()), 1)

    def test_webhook_and_browser_return_racing_pay_and_ship_once(self):
        order = make_order(payment_status=PaymentStatus.PENDING)
        attempt = make_attempt(order)
        session = stripe_session(order, attempt=attempt)
        event = stripe_event(session)
        payload = json.dumps(event).encode()

        def webhook():
            return Client().post("/pay/stripe/webhook/", data=payload, content_type="application/json",
                                 HTTP_STRIPE_SIGNATURE=sign(payload)).status_code

        def browser():
            c = Client()
            s = c.session
            s["current_order_id"] = order.pk
            s.save()
            with mock.patch("stripe.checkout.Session.retrieve", return_value=session):
                return c.get(f"/checkout/thank-you/?session_id={attempt.stripe_session_id}").status_code

        toggles = [webhook, browser] * (N // 2)
        i = iter(range(len(toggles)))
        results, errors = run_parallel(lambda: toggles[next(i)](), n=len(toggles))
        self.assertEqual(errors, [])
        self.assertEqual(len(self.econt.creates()), 1)
        self.assertEqual(OrderEvent.objects.filter(order=order, kind="payment_confirmed").count(), 1)
        self.assertEqual(Order.objects.get(pk=order.pk).payment_status, PaymentStatus.PAID)

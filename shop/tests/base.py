"""
Shared test helpers.

MOCKED: every call to Econt (`requests.Session.post`) goes through `EcontFake`; every Stripe API call is patched
per-test; webhook signatures are REAL (HMAC computed like Stripe does and verified by stripe.Webhook.construct_event).
Nothing here talks to a provider sandbox - see the report for what remains unverified.
"""
import hashlib
import hmac
import itertools
import json
import time
from decimal import Decimal
from unittest import mock

import requests
from django.test import TestCase
from django.utils import timezone

from shop.models import (
    DeliveryMethod,
    Order,
    OrderItem,
    PaymentAttempt,
    PaymentMethod,
    Product,
)

WEBHOOK_SECRET = "whsec_test_secret"
_counter = itertools.count(1)


# ------------------------------------------------------------------------------------- Econt fake
class FakeResponse:
    def __init__(self, status_code=200, body=None, text=None):
        self.status_code = status_code
        self._body = body
        self.text = text if text is not None else json.dumps(body or {})
        self.reason = "x"

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


def econt_error_body(msg="Улицата, която въведохте, е налична в следните квартали"):
    """Real shape captured from the Econt demo server: blank top-level message, reason nested."""
    return {"type": "ExInvalidParam", "message": " ", "fields": [], "innerErrors": [
        {"type": "ExInvalidParam", "message": "получател: ", "fields": [], "innerErrors": [
            {"type": "ExInvalidAddress", "message": msg, "fields": [], "innerErrors": []}]}]}


class EcontFake:
    """Replaces requests.Session.post. Records every call; `queue` lets a test script specific answers."""

    def __init__(self):
        self.calls = []  # (mode_or_method, payload_dict)
        self.queue = []  # list of FakeResponse | Exception, consumed for 'create' calls only
        self.mode_queue = {}  # mode -> list of FakeResponse | Exception for validate / calculate / getMyAWB ...
        self.numbers = itertools.count(1051000000001)

    def __call__(self, session, url, data=None, timeout=None, **kw):
        body = json.loads(data.decode("utf-8")) if data else {}
        mode = body.get("mode") or url.rsplit("/", 1)[-1].split(".")[-2]
        self.calls.append((mode, body))
        if self.mode_queue.get(mode):
            item = self.mode_queue[mode].pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        if mode == "create" and self.queue:
            item = self.queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        if mode == "create":
            return FakeResponse(200, self._created(body["label"]))
        if mode == "validate":
            return FakeResponse(200, {"label": {}})
        if mode == "calculate":
            return FakeResponse(200, {"label": {
                "totalPrice": 5.94, "currency": "EUR",
                "services": [{"type": "C", "price": 5.62, "currency": "EUR"},
                             {"type": "SMS_NOTIFICATION", "price": 0.11, "currency": "EUR"},
                             {"type": "OC", "price": 0.21, "currency": "EUR"}]}})
        raise AssertionError(f"unexpected Econt call {mode}")

    def _created(self, label):
        """Mimics what the Econt demo server returned for create (receiverDueAmount / services / pdfURL)."""
        receiver_pays = label.get("paymentReceiverMethod") is not None
        svc = label.get("services") or {}
        services = [{"type": "C", "paymentSide": "RECEIVER" if receiver_pays else "SENDER", "price": 5.62}]
        if svc.get("cdAmount"):
            services.append({"type": "CD", "price": 0.19})
        return {"label": {
            "shipmentNumber": str(next(self.numbers)),
            "pdfURL": "https://ee.econt.com/pdf/label.pdf",
            "totalPrice": 5.94, "currency": "EUR",
            "senderDueAmount": 0 if receiver_pays else 5.94,
            "receiverDueAmount": 5.94 if receiver_pays else 0,
            "services": services,
        }}

    def creates(self):
        return [b["label"] for m, b in self.calls if m == "create"]


# ------------------------------------------------------------------------------------- stripe helpers
def sign(payload: bytes, secret=WEBHOOK_SECRET, ts=None) -> str:
    ts = ts or int(time.time())
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def stripe_session(order, *, attempt=None, payment_status="paid", amount=None, currency="eur",
                   session_id=None, metadata_order_id=None, mode="payment", pi=None):
    attempt = attempt or order.payment_attempts.first()
    sid = session_id or (attempt.stripe_session_id if attempt else f"cs_test_{next(_counter):012d}")
    return {
        "id": sid, "object": "checkout.session", "mode": mode, "payment_status": payment_status,
        "status": "complete", "currency": currency,
        "amount_total": amount if amount is not None else (attempt.amount_minor if attempt else 0),
        "payment_intent": pi or f"pi_test_{next(_counter):012d}",
        "metadata": {"order_id": str(metadata_order_id if metadata_order_id is not None else order.pk)},
    }


def stripe_event(session, etype="checkout.session.completed", event_id=None, livemode=False):
    return {"id": event_id or f"evt_test_{next(_counter):012d}", "object": "event", "type": etype,
            "livemode": livemode, "api_version": "2024-06-20", "data": {"object": session}}


# ------------------------------------------------------------------------------------- factories
def make_product():
    return Product.objects.create(name="Живот без шум", slug="book", price_bgn=Decimal("25.00"),
                                  price_eur=Decimal("12.78"))


def make_order(product=None, *, qty=1, method=PaymentMethod.CARD, delivery=DeliveryMethod.TO_ADDRESS,
               quoted=True, **fields) -> Order:
    product = product or Product.objects.first() or make_product()
    base = dict(
        full_name="Иван Иванов", email="ivan@example.com", phone="+359 888 123 456", city="София",
        postal_code="1000", delivery_method=delivery, payment_method=method, quantity=qty,
    )
    if delivery == DeliveryMethod.TO_ADDRESS:
        base.update(address_line="ул. Витоша 12", receiver_street="ул. Витоша", receiver_num="12")
    else:
        base.update(econt_office_code="1000", office_text="Офис София")
    base.update(fields)
    order = Order.objects.create(**base)
    OrderItem.objects.create(order=order, product=product, quantity=qty,
                             unit_price_bgn=product.price_bgn, unit_price_eur=product.price_eur)
    if quoted:
        order.shipping_eur = Decimal("5.94")
        order.shipping_bgn = Decimal("11.62")
        order.shipping_quoted_at = timezone.now()
    order.recompute_totals()
    order.save()
    return order


def make_attempt(order, amount=None, sid=None, status=PaymentAttempt.Status.OPEN):
    from shop.payments import expected_amount_minor

    return PaymentAttempt.objects.create(
        order=order, stripe_session_id=sid or f"cs_test_{next(_counter):012d}",
        amount_minor=amount if amount is not None else expected_amount_minor(order),
        currency="eur", status=status, checkout_url="https://checkout.stripe.test/pay/x",
    )


class ShopTestCase(TestCase):
    """Blocks all real network access; installs the Econt fake on requests.Session.post."""

    def setUp(self):
        super().setUp()
        self.econt = EcontFake()
        p = mock.patch.object(requests.Session, "post", autospec=True, side_effect=self.econt)
        p.start()
        self.addCleanup(p.stop)
        # any other outbound request (Stripe uses its own client; those are patched per test) must fail loudly
        p2 = mock.patch.object(requests.Session, "request",
                               side_effect=AssertionError("real network call attempted in test"))
        p2.start()
        self.addCleanup(p2.stop)
        self.product = make_product()

    def post_webhook(self, event: dict, secret=WEBHOOK_SECRET, signature=None, client=None):
        payload = json.dumps(event).encode()
        client = client or self.client
        return client.post(
            "/pay/stripe/webhook/", data=payload, content_type="application/json",
            HTTP_STRIPE_SIGNATURE=signature if signature is not None else sign(payload, secret),
        )

    def session_for(self, order, client=None):
        client = client or self.client
        s = client.session
        s["current_order_id"] = order.pk
        s.save()
        return client

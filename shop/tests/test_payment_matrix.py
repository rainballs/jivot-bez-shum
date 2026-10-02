"""
Payment-method x Econt COD / delivery-payer matrix (the heart of reported problem #2).

  prepaid (card, Stripe collected goods + delivery) : no COD service, no receiver payment, sender pays Econt
  cod (customer confirmed)                          : COD = goods only, recipient pays courier charges
"""
from decimal import Decimal

from django.utils import timezone

from shop import fulfillment
from shop.fulfillment import NotFulfillable, build_label_for_order, plan_for_order
from shop.models import DeliveryMethod, PaymentMethod, PaymentStatus, ShipmentStatus

from .base import ShopTestCase, make_order


def paid(order):
    order.payment_status = PaymentStatus.PAID
    order.paid = True
    order.save(update_fields=["payment_status", "paid"])
    return order


class PayloadMatrixTests(ShopTestCase):
    def label(self, order, for_create=True):
        return build_label_for_order(order, plan_for_order(order, for_create=for_create))

    def assert_prepaid(self, label):
        services = label.get("services", {})
        self.assertNotIn("cdAmount", services)
        self.assertNotIn("cdType", services)
        self.assertNotIn("cdPayOptionsTemplate", services)
        self.assertNotIn("invoiceNum", services)
        self.assertNotIn("paymentReceiverMethod", label)
        self.assertNotIn("paymentReceiverAmount", label)
        self.assertNotIn("payer", label)  # not an Econt field (verified on demo: silently ignored)

    def test_card_paid_requests_no_cod_and_sender_pays_delivery(self):
        order = paid(make_order(method=PaymentMethod.CARD))
        self.assert_prepaid(self.label(order))

    def test_paid_order_with_stale_cod_method_is_still_prepaid(self):
        """The reported bug: Stripe-paid order whose payment_method got flipped/stale to COD."""
        order = paid(make_order(method=PaymentMethod.COD))
        order.cod_confirmed_at = timezone.now()  # even if COD had been confirmed before the payment arrived
        order.save(update_fields=["cod_confirmed_at"])
        self.assert_prepaid(self.label(order))

    def test_genuine_cod_asks_goods_amount_only_and_recipient_pays_delivery(self):
        order = make_order(method=PaymentMethod.COD, qty=2, cod_confirmed_at=timezone.now())
        label = self.label(order)
        self.assertEqual(order.subtotal_eur, Decimal("25.56"))
        self.assertEqual(label["services"]["cdAmount"], 25.56)  # goods only (shipping NOT included)
        self.assertNotEqual(label["services"]["cdAmount"], float(order.total_eur))
        self.assertEqual(label["services"]["cdCurrency"], "EUR")
        self.assertEqual(label["services"]["cdType"], "get")
        self.assertEqual(label["paymentReceiverMethod"], "CASH")
        self.assertNotIn("paymentReceiverAmount", label)  # used to be set to the COD amount (wrong semantics)

    def test_cod_not_confirmed_cannot_be_created(self):
        order = make_order(method=PaymentMethod.COD)
        with self.assertRaises(NotFulfillable):
            plan_for_order(order, for_create=True)

    def test_unpaid_card_orders_never_become_cod(self):
        for status in (PaymentStatus.UNPAID, PaymentStatus.PENDING, PaymentStatus.FAILED):
            order = make_order(method=PaymentMethod.CARD, payment_status=status)
            with self.assertRaises(NotFulfillable, msg=status):
                plan_for_order(order, for_create=True)
            # quoting before payment uses the prepaid shape, never COD
            self.assert_prepaid(self.label(order, for_create=False))

    def test_quote_uses_selected_method(self):
        order = make_order(method=PaymentMethod.COD)
        self.assertEqual(self.label(order, for_create=False)["services"]["cdAmount"], 12.78)

    def test_delivery_method_decides_office_vs_address_not_stale_fields(self):
        order = make_order(method=PaymentMethod.CARD, delivery=DeliveryMethod.TO_ADDRESS, econt_office_code="9999")
        label = paid(order) and self.label(order)
        self.assertEqual(label["service"], "toDoor")
        self.assertNotIn("receiverOfficeCode", label)
        self.assertEqual(label["receiverAddress"]["street"], "ул. Витоша")
        self.assertEqual(label["receiverAddress"]["num"], "12")

    def test_office_delivery(self):
        order = paid(make_order(delivery=DeliveryMethod.TO_OFFICE))
        label = self.label(order)
        self.assertEqual(label["service"], "toOffice")
        self.assertEqual(label["receiverOfficeCode"], "1000")
        self.assertNotIn("receiverAddress", label)

    def test_legacy_order_without_structured_address_is_split_with_street_and_number(self):
        order = paid(make_order(receiver_street="", receiver_num="", address_line="ул. Витоша 12А"))
        addr = self.label(order)["receiverAddress"]
        self.assertEqual((addr["street"], addr["num"]), ("ул. Витоша", "12А"))

    def test_quantity_scales_packing_list_declared_value_and_weight(self):
        order = paid(make_order(qty=3))
        label = self.label(order)
        self.assertEqual(label["packingList"][0]["count"], 3)
        self.assertEqual(label["services"]["declaredValueAmount"], 38.34)
        self.assertEqual(label["services"]["declaredValueCurrency"], "EUR")
        self.assertEqual(label["packingList"][0]["price"], 12.78)  # unit price in EUR
        self.assertGreaterEqual(label["weight"], 1.2)


class EndToEndMatrixTests(ShopTestCase):
    """What Econt actually receives through the whole attempt_shipment() path."""

    def run_shipment(self, order):
        fulfillment.request_shipment(order.pk)
        return fulfillment.attempt_shipment(order.pk)

    def test_prepaid_end_to_end(self):
        order = paid(make_order(method=PaymentMethod.CARD))
        self.assertEqual(self.run_shipment(order), "created")
        sent = self.econt.creates()[0]
        self.assertNotIn("cdAmount", sent.get("services", {}))
        self.assertNotIn("paymentReceiverMethod", sent)
        order.refresh_from_db()
        self.assertEqual(order.shipment_status, ShipmentStatus.CREATED)
        self.assertEqual(order.econt_cod_amount, Decimal("0"))
        self.assertFalse(order.econt_receiver_pays_delivery)
        self.assertFalse(order.needs_review)

    def test_cod_end_to_end_records_what_was_requested(self):
        order = make_order(method=PaymentMethod.COD, cod_confirmed_at=timezone.now())
        self.assertEqual(self.run_shipment(order), "created")
        order.refresh_from_db()
        self.assertEqual(order.econt_cod_amount, Decimal("12.78"))
        self.assertEqual(order.econt_cod_currency, "EUR")
        self.assertTrue(order.econt_receiver_pays_delivery)
        self.assertFalse(order.needs_review)

    def test_pending_failed_cancelled_abandoned_card_payments_are_never_shipped(self):
        for status in (PaymentStatus.UNPAID, PaymentStatus.PENDING, PaymentStatus.FAILED):
            order = make_order(method=PaymentMethod.CARD, payment_status=status)
            fulfillment.request_shipment(order.pk)  # even if something wrongly queued it ...
            self.assertEqual(fulfillment.attempt_shipment(order.pk), "blocked")  # ... it is refused, and visible
            order.refresh_from_db()
            self.assertTrue(order.needs_review)
        self.assertEqual(self.econt.creates(), [])

    def test_intent_mismatch_reported_by_econt_is_flagged(self):
        """If Econt's answer shows COD / recipient-pays on a prepaid order, an operator is alerted."""
        from .base import FakeResponse

        order = paid(make_order(method=PaymentMethod.CARD))
        self.econt.queue.append(FakeResponse(200, {"label": {
            "shipmentNumber": "1051999999999", "receiverDueAmount": 5.94,
            "services": [{"type": "C"}, {"type": "CD"}]}}))
        self.assertEqual(self.run_shipment(order), "created")
        order.refresh_from_db()
        self.assertTrue(order.needs_review)
        self.assertIn("COD", order.review_reason)
        from django.core import mail

        self.assertTrue(any("does not match" in m.subject for m in mail.outbox))


class SendDateTests(ShopTestCase):
    """Econt rejects door deliveries with a Friday send date unless holidayDeliveryDay is sent (demo-verified)."""

    def test_payload_always_carries_the_delivery_day_choice(self):
        for delivery in (DeliveryMethod.TO_ADDRESS, DeliveryMethod.TO_OFFICE):
            label = build_label_for_order(paid(make_order(delivery=delivery)), plan_for_order(
                paid(make_order(delivery=delivery)), for_create=True))
            self.assertEqual(label["holidayDeliveryDay"], "workday")

    def test_a_thursday_order_gets_a_friday_send_date_and_the_choice_that_makes_econt_accept_it(self):
        import datetime
        from unittest import mock

        from shop import econt_client

        class Thursday(datetime.date):
            @classmethod
            def today(cls):
                return cls(2026, 10, 1)

        order = paid(make_order())
        with mock.patch.object(econt_client, "date", Thursday):
            label = build_label_for_order(order, plan_for_order(order, for_create=True))
        self.assertEqual(label["sendDate"], "2026-10-02")  # a Friday
        self.assertEqual(label["holidayDeliveryDay"], "workday")

    def test_setting_can_disable_the_field(self):
        from django.test import override_settings
        from django.conf import settings

        order = paid(make_order())
        cfg = {**settings.ECONT, "DEFAULTS": {**settings.ECONT["DEFAULTS"], "holiday_delivery_day": ""}}
        with override_settings(ECONT=cfg):
            self.assertNotIn("holidayDeliveryDay", build_label_for_order(order, plan_for_order(order, for_create=True)))


class OrderNumberSwitchTests(ShopTestCase):
    """orderNumber is an optional extra: off by default, switchable from the environment."""

    def label_for_paid_order(self):
        order = paid(make_order())
        return build_label_for_order(order, plan_for_order(order, for_create=True))

    def test_not_sent_by_default(self):
        self.assertNotIn("orderNumber", self.label_for_paid_order())

    def test_can_be_enabled(self):
        from django.conf import settings
        from django.test import override_settings

        cfg = {**settings.ECONT, "DEFAULTS": {**settings.ECONT["DEFAULTS"], "send_order_number": True}}
        with override_settings(ECONT=cfg):
            self.assertRegex(self.label_for_paid_order()["orderNumber"], r"^\d+$")

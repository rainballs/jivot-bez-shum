"""Order e-mails carry the Econt shipment data; the admin list mirrors Econt's own status."""
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.test import Client, override_settings
from django.utils import timezone

from shop import fulfillment
from shop.models import DeliveryMethod, Order, PaymentMethod, PaymentStatus, ShipmentStatus

from .base import FakeResponse, ShopTestCase, make_attempt, make_order, stripe_event, stripe_session


def statuses(*pairs):
    return FakeResponse(200, {"shipmentStatuses": [
        {"status": {"shipmentNumber": n, "shortDeliveryStatus": st}} if st else
        {"status": None, "error": {"message": "Не е намерена пратка"}} for n, st in pairs]})


class OrderEmailContentTests(ShopTestCase):
    def test_cod_office_order_emails_name_the_office_and_carry_shipment_and_tracking(self):
        order = make_order(method=PaymentMethod.COD, delivery=DeliveryMethod.TO_OFFICE, office_text="",
                           econt_office_code="8020", city="Бургас")
        self.session_for(order).post("/checkout/confirm-cod/")
        order.refresh_from_db()
        num = order.econt_shipment_num
        admin, customer = sorted(mail.outbox, key=lambda m: m.to[0] != "admin@shop.test")
        for m in (admin, customer):
            self.assertIn("Офис на Еконт 8020", m.body)  # was empty: office_text is blank for office orders
            self.assertIn(num, m.body)
            self.assertIn(f"https://www.econt.com/services/track-shipment/{num}", m.body)
        self.assertIn("Label PDF: https://", admin.body)
        self.assertIn("Cash on delivery requested: € 12,78", admin.body)
        self.assertNotIn("Label PDF", customer.body)  # the printable label is for the shop only

    def test_card_order_payment_email_includes_the_shipment_when_econt_answers_immediately(self):
        order = make_order(method=PaymentMethod.CARD, payment_status=PaymentStatus.PENDING)
        attempt = make_attempt(order)
        with self.captureOnCommitCallbacks(execute=True):
            self.post_webhook(stripe_event(stripe_session(order, attempt=attempt)))
        order.refresh_from_db()
        admin = [m for m in mail.outbox if m.to == ["admin@shop.test"]]
        self.assertEqual(len(admin), 1)
        self.assertIn(order.econt_shipment_num, admin[0].body)
        self.assertIn("none (prepaid)", admin[0].body)

    def test_card_order_gets_a_shipment_notice_when_the_label_is_created_later(self):
        import requests

        order = make_order(method=PaymentMethod.CARD, payment_status=PaymentStatus.PENDING)
        attempt = make_attempt(order)
        self.econt.queue.append(requests.exceptions.ConnectTimeout("econt down"))
        with self.captureOnCommitCallbacks(execute=True):
            self.post_webhook(stripe_event(stripe_session(order, attempt=attempt)))
        self.assertIn("Shipment not created yet", [m for m in mail.outbox if m.to == ["admin@shop.test"]][0].body)
        Order.objects.filter(pk=order.pk).update(shipment_next_attempt_at=timezone.now())
        call_command("process_shipments", stdout=StringIO())  # Econt is back
        order.refresh_from_db()
        notice = [m for m in mail.outbox if "товарителница" in m.subject]
        self.assertEqual(len(notice), 1)
        self.assertEqual(notice[0].to, ["admin@shop.test"])
        self.assertIn(order.econt_shipment_num, notice[0].body)
        self.assertIn("няма (платена онлайн)", notice[0].body)


class EcontStatusMirrorTests(ShopTestCase):
    def created(self, num, **kw):
        o = make_order(payment_status=PaymentStatus.PAID, paid=True, **kw)
        Order.objects.filter(pk=o.pk).update(shipment_status=ShipmentStatus.CREATED, econt_shipment_num=num)
        return o

    def test_status_text_is_copied_and_rate_limited(self):
        a, b = self.created("N-1"), self.created("N-2")
        self.econt.mode_queue["getShipmentStatuses"] = [statuses(("N-1", "Очаква предаване към Еконт"), ("N-2", None))]
        self.assertEqual(fulfillment.refresh_econt_statuses(), 2)
        self.assertEqual(Order.objects.get(pk=a.pk).econt_status, "Очаква предаване към Еконт")
        self.assertEqual(Order.objects.get(pk=b.pk).econt_status, "НЕ Е НАМЕРЕНА в Еконт")
        self.assertEqual(fulfillment.refresh_econt_statuses(), 0)  # checked less than 15 minutes ago
        self.assertEqual(len([m for m, _ in self.econt.calls if m == "getShipmentStatuses"]), 1)

    def test_delivered_shipments_are_not_polled_again(self):
        o = self.created("N-3")
        Order.objects.filter(pk=o.pk).update(econt_status="Доставена",
                                             econt_status_checked_at=timezone.now() - timedelta(hours=2))
        self.assertEqual(fulfillment.refresh_econt_statuses(), 0)
        self.assertEqual(self.econt.calls, [])

    def test_econt_outage_does_not_break_the_sweeper(self):
        import requests

        self.created("N-4")
        self.econt.mode_queue["getShipmentStatuses"] = [requests.exceptions.ReadTimeout("x")]
        call_command("process_shipments", stdout=StringIO())  # no exception

    def test_admin_list_shows_status_and_label_link(self):
        from django.conf import settings

        o = self.created("N-5")
        Order.objects.filter(pk=o.pk).update(econt_status="Очаква предаване към Еконт",
                                             econt_label_url="https://ee.econt.com/pdf/x.pdf")
        get_user_model().objects.create_superuser("root", "r@example.com", "pw")
        c = Client()
        c.login(username="root", password="pw")
        html = c.get(f"/{settings.ADMIN_URL}shop/order/").content.decode()
        self.assertIn("Очаква предаване към Еконт", html)
        self.assertIn('href="https://ee.econt.com/pdf/x.pdf"', html)


class MailboxWarningTests(ShopTestCase):
    def test_self_addressed_admin_mail_is_warned_about(self):
        from shop.management.commands.check_production import WARN, collect_checks

        name = "Admin notifications go to a different mailbox than the sender"
        with override_settings(ORDER_NOTIFY_EMAIL="shop@gmail.com", EMAIL_HOST_USER="shop@gmail.com"):
            self.assertEqual({n: lv for lv, n, _ in collect_checks(config_only=True)}[name], WARN)
        with override_settings(ORDER_NOTIFY_EMAIL="owner@icloud.com", EMAIL_HOST_USER="shop@gmail.com"):
            self.assertNotEqual({n: lv for lv, n, _ in collect_checks(config_only=True)}[name], WARN)


class HandOverFilterTests(ShopTestCase):
    def test_admin_lists_the_parcels_still_to_take_to_econt(self):
        from django.conf import settings

        waiting = make_order(full_name="Чака Предаване")
        sent = make_order(full_name="Вече Изпратена")
        Order.objects.filter(pk=waiting.pk).update(shipment_status=ShipmentStatus.CREATED, econt_shipment_num="W1",
                                                   econt_status="Очаква предаване към Еконт")
        Order.objects.filter(pk=sent.pk).update(shipment_status=ShipmentStatus.CREATED, econt_shipment_num="S1",
                                                econt_status="Доставена")
        get_user_model().objects.create_superuser("root", "r@example.com", "pw")
        c = Client()
        c.login(username="root", password="pw")
        html = c.get(f"/{settings.ADMIN_URL}shop/order/?handover=waiting").content.decode()
        self.assertIn("Чака Предаване", html)
        self.assertNotIn("Вече Изпратена", html)

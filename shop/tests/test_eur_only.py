"""The shop is EUR-only: no BGN amount may be displayed, calculated or sent anywhere."""
import re
from decimal import Decimal
from pathlib import Path

from django.core import mail
from django.test import Client

from shop import fulfillment
from shop.models import Order, OrderItem, PaymentMethod, PaymentStatus, Product

from .base import ShopTestCase, make_order

ROOT = Path(__file__).resolve().parents[2]
BGN_WORDS = re.compile(r"\bBGN\b|\d\s?лв\b|лв\.|\bлева\b", re.IGNORECASE)


class EurOnlyTests(ShopTestCase):
    def assert_no_bgn(self, html, where):
        found = BGN_WORDS.findall(html)
        self.assertEqual(found, [], f"BGN amount shown on {where}")

    def test_pages_show_euro_only(self):
        order = make_order()
        c = self.session_for(order)
        self.assert_no_bgn(Client().get("/").content.decode(), "home")
        self.assert_no_bgn(c.get("/checkout/").content.decode(), "checkout form")
        summary = c.get("/checkout/summary/").content.decode()
        self.assert_no_bgn(summary, "summary")
        self.assertIn("€", summary)
        Order.objects.filter(pk=order.pk).update(payment_status=PaymentStatus.PAID, paid=True)
        self.assert_no_bgn(c.get("/checkout/thank-you/").content.decode(), "thank-you")

    def test_home_shows_one_euro_price_per_block(self):
        html = Client().get("/").content.decode()
        self.assertIn("€ 12,78", html.replace("12.78", "12,78"))
        self.assertNotIn("лв", re.sub(r"<[^>]+>", " ", html).replace("булевард", "").replace("попълв", ""))

    def test_emails_are_euro_only(self):
        from shop.utils import send_order_notification

        order = make_order(payment_status=PaymentStatus.PAID, paid=True)
        send_order_notification(order, event="paid")
        self.assertTrue(mail.outbox)
        for m in mail.outbox:
            self.assert_no_bgn(m.body, f"e-mail {m.subject}")
            self.assertIn("€", m.body)

    def test_terms_state_prices_are_in_euro(self):
        html = (ROOT / "templates/legal/terms.html").read_text(encoding="utf-8")
        self.assertIn("Всички цени в Сайта са в евро (EUR)", html)
        self.assertNotIn("(BGN)", html)

    def test_orders_are_calculated_in_euro_without_touching_the_legacy_bgn_columns(self):
        order = make_order(qty=3)
        order.refresh_from_db()
        self.assertEqual(order.subtotal_eur, Decimal("38.34"))
        self.assertEqual(order.total_eur, Decimal("38.34") + Decimal("5.94"))
        self.assertEqual((order.subtotal_bgn, order.shipping_bgn, order.total_bgn), (0, 0, 0))
        self.assertIsNone(order.items.get().unit_price_bgn)

    def test_a_product_without_any_bgn_price_works_end_to_end(self):
        self.assertIsNone(Product.objects.get().price_bgn)
        c = Client()
        self.assertEqual(c.get("/checkout/").status_code, 200)
        order = Order.objects.get()
        self.assertEqual(order.items.get().unit_price_eur, Decimal("12.78"))
        self.assertEqual(order.subtotal_eur, Decimal("12.78"))

    def test_shipping_estimate_before_the_econt_quote_is_in_euro_and_never_chargeable(self):
        order = make_order(quoted=False)
        order.shipping_eur = Decimal("0")
        order.recompute_totals()
        self.assertEqual(order.shipping_eur, Order.ESTIMATE_SHIPPING_ADDRESS)
        self.assertFalse(order.delivery_ready)  # an estimate cannot be paid

    def test_econt_payload_is_euro_only(self):
        order = make_order(method=PaymentMethod.COD, qty=2)
        order.cod_confirmed_at = __import__("django.utils.timezone", fromlist=["now"]).now()
        order.save()
        plan = fulfillment.plan_for_order(order, for_create=True)
        label = fulfillment.build_label_for_order(order, plan)
        text = str(label)
        self.assertNotIn("BGN", text)
        self.assertEqual(label["services"]["cdCurrency"], "EUR")
        self.assertEqual(label["services"]["declaredValueCurrency"], "EUR")
        self.assertEqual(plan.cod_currency, "EUR")

    def test_a_bgn_answer_from_econt_is_converted_to_euro(self):
        from shop.econt_service import _to_eur

        self.assertEqual(_to_eur(Decimal("11.62"), "EUR"), Decimal("11.62"))
        self.assertEqual(_to_eur(Decimal("11.62"), "BGN"), Decimal("5.94"))

    def test_hero_button_is_centred_on_mobile(self):
        css = (ROOT / "static/css/styles.css").read_text(encoding="utf-8")
        block = css[css.rindex("@media (max-width: 768px)"):]
        self.assertIn(".hero-cta", block)
        self.assertIn("margin-left: auto", block)
        self.assertIn("margin-right: auto", block)

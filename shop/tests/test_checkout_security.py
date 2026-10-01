"""Checkout flow: access control, price tampering, abuse limits, no-label-without-payment, idempotent submits."""
from decimal import Decimal
from unittest import mock

import requests
import stripe
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from shop.models import (
    Order,
    OrderItem,
    PaymentAttempt,
    PaymentMethod,
    PaymentStatus,
    ShipmentStatus,
)

from .base import FakeResponse, ShopTestCase, econt_error_body, make_order, stripe_session


class CheckoutBase(ShopTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()


class AccessControlTests(CheckoutBase):
    def test_order_id_in_query_or_post_is_ignored(self):
        mine = make_order(full_name="Моя Поръчка")
        theirs = make_order(full_name="Чужд Клиент", email="victim@example.com")
        c = self.session_for(mine)
        body = c.get(f"/checkout/summary/?order_id={theirs.pk}").content.decode()
        self.assertIn("Моя Поръчка", body)
        self.assertNotIn("Чужд Клиент", body)
        self.assertNotIn("victim@example.com", body)

    def test_no_session_means_no_access_even_with_a_valid_order_id(self):
        theirs = make_order(full_name="Чужд Клиент")
        c = Client()
        r = c.get(f"/checkout/summary/?order_id={theirs.pk}")
        self.assertEqual(r.status_code, 302)  # redirected to checkout, nothing leaked
        r = c.get(f"/econt/partial/address/?order_id={theirs.pk}")
        self.assertFalse(r.json()["ok"])

    def test_guessable_id_routes_no_longer_exist(self):
        order = make_order()
        c = self.session_for(order)
        for url in (f"/checkout/summary/{order.pk}/", "/checkout/payment/", "/checkout/inline-update/"):
            self.assertEqual(c.get(url).status_code, 404, url)

    def test_posting_someone_elses_order_id_does_not_modify_it(self):
        theirs = make_order(full_name="Чужд Клиент", qty=1)
        c = Client()
        r = c.post("/checkout/save-inline/", {"order_id": theirs.pk, "quantity": "5", "billing_full_name": "Hacker"})
        self.assertEqual(r.status_code, 200)
        theirs.refresh_from_db()
        self.assertEqual((theirs.quantity, theirs.billing_full_name), (1, ""))

    def test_thank_you_does_not_reveal_personal_data_to_other_browsers(self):
        victim = make_order(email="victim@example.com", payment_status=PaymentStatus.PENDING)
        from .base import make_attempt

        attempt = make_attempt(victim)
        with mock.patch("stripe.checkout.Session.retrieve",
                        return_value=stripe_session(victim, attempt=attempt)):
            body = Client().get(f"/checkout/thank-you/?session_id={attempt.stripe_session_id}").content.decode()
        self.assertIn(f"#{victim.pk}", body)
        self.assertNotIn("victim@example.com", body)

    def test_label_pdf_link_is_not_published_on_the_thank_you_page(self):
        order = make_order(payment_status=PaymentStatus.PAID, shipment_status="created", econt_shipment_num="X9")
        order.econt_label_pdf.save("l.pdf", __import__("django.core.files.base", fromlist=["ContentFile"]).ContentFile(b"%PDF"))
        body = self.session_for(order).get("/checkout/thank-you/").content.decode()
        self.assertNotIn("econt_labels", body)

    def test_customer_data_is_escaped_in_templates(self):
        order = make_order(full_name="<script>alert(1)</script>")
        body = self.session_for(order).get("/checkout/summary/").content.decode()
        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertIn("&lt;script&gt;", body)

    def test_econt_values_are_escaped_before_innerhtml(self):
        import pathlib

        base = pathlib.Path(__file__).resolve().parents[2] / "templates"
        for name in ("checkout/info.html", "econt/office.html"):
            html = (base / name).read_text(encoding="utf-8")
            self.assertIn("function escHtml", html, name)
            self.assertNotIn("${o.name}", html, name)
            self.assertNotIn("${c.name}", html, name)


class AdminTests(CheckoutBase):
    def test_order_change_page_renders_and_requires_staff(self):
        """delivery_preview used `obj.DeliveryMethod` (AttributeError, silently swallowed by Django -> blank preview)."""
        from django.conf import settings

        order = make_order()
        base = f"/{settings.ADMIN_URL}shop/order"
        self.assertEqual(Client().get(f"{base}/{order.pk}/change/").status_code, 302)  # anonymous -> login
        User = get_user_model()
        User.objects.create_superuser("root", "r@example.com", "pw")
        c = Client()
        c.login(username="root", password="pw")
        r = c.get(f"{base}/{order.pk}/change/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "София, ул. Витоша 12, 1000")  # the preview really renders now
        self.assertEqual(c.get(f"{base}/").status_code, 200)
        self.assertEqual(c.get(f"{base}/?needs_review__exact=1").status_code, 200)

    def test_payment_and_shipment_fields_are_read_only_in_admin(self):
        from django.contrib import admin as dj_admin

        ma = dj_admin.site._registry[Order]
        for f in ("payment_status", "paid", "shipment_status", "econt_shipment_num", "stripe_payment_intent_id"):
            self.assertIn(f, ma.readonly_fields)

    def test_admin_actions_cannot_retry_unknown_outcomes_through_the_failed_action(self):
        from shop import fulfillment
        from shop.admin import action_requeue_failed

        o = make_order(payment_status=PaymentStatus.PAID)
        Order.objects.filter(pk=o.pk).update(shipment_status=ShipmentStatus.UNKNOWN)
        ma = mock.Mock()
        action_requeue_failed(ma, mock.Mock(user=mock.Mock(pk=1)), Order.objects.filter(pk=o.pk))
        self.assertEqual(Order.objects.get(pk=o.pk).shipment_status, ShipmentStatus.UNKNOWN)


class PriceTamperingTests(CheckoutBase):
    def test_quantity_must_be_within_limits(self):
        order = make_order()
        c = self.session_for(order)
        for bad in ("0", "-3", "abc", "21", "99999999", "1.5", " "):
            if bad.strip():
                r = c.post("/checkout/save-inline/", {"quantity": bad})
                self.assertEqual(r.status_code, 400, bad)
        order.refresh_from_db()
        self.assertEqual(order.quantity, 1)

    def test_valid_quantity_updates_item_and_totals_server_side_and_invalidates_the_quote(self):
        order = make_order()
        c = self.session_for(order)
        self.assertEqual(c.post("/checkout/save-inline/", {"quantity": "3"}).status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.quantity, 3)
        self.assertEqual(order.items.get().quantity, 3)
        self.assertEqual(order.subtotal_eur, Decimal("38.34"))
        self.assertIsNone(order.shipping_quoted_at)  # must re-quote before paying

    def test_client_cannot_set_prices_status_or_paid_flags(self):
        order = make_order()
        c = self.session_for(order)
        c.post("/checkout/save-inline/", {
            "total_eur": "0.01", "subtotal_eur": "0.01", "shipping_eur": "0.00", "paid": "true",
            "payment_status": "paid", "shipment_status": "created", "econt_shipment_num": "1",
            "unit_price_eur": "0.01", "currency": "bgn",
        })
        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.UNPAID)
        self.assertFalse(order.paid)
        self.assertEqual(order.shipping_eur, Decimal("5.94"))
        self.assertEqual(order.items.get().unit_price_eur, Decimal("12.78"))
        self.assertEqual(order.shipment_status, ShipmentStatus.NONE)

    def test_only_card_and_cod_are_valid_payment_methods(self):
        order = make_order()
        c = self.session_for(order)
        for bad in ("apple_pay", "free", "<b>", "paid"):
            self.assertEqual(c.post("/checkout/save-inline/", {"payment_method": bad}).status_code, 400, bad)
        order.refresh_from_db()
        self.assertEqual(order.payment_method, PaymentMethod.CARD)

    def test_overlong_values_rejected_instead_of_crashing(self):
        c = self.session_for(make_order())
        self.assertEqual(c.post("/checkout/save-inline/", {"billing_full_name": "x" * 500}).status_code, 400)

    def test_stripe_amount_comes_only_from_the_database(self):
        order = make_order(qty=2)
        c = self.session_for(order)
        fake = {"id": "cs_test_abcdefghij12", "url": "https://checkout.stripe.test/c/pay/x"}
        with mock.patch("stripe.checkout.Session.create", return_value=fake) as create:
            r = c.post("/pay/stripe/create-session/", {"amount": "1", "total_eur": "0.01", "currency": "bgn"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], fake["url"])
        kw = create.call_args.kwargs
        amounts = sum(i["price_data"]["unit_amount"] * i["quantity"] for i in kw["line_items"])
        self.assertEqual(amounts, 2 * 1278 + 594)
        self.assertEqual(kw["metadata"]["order_id"], str(order.pk))
        self.assertTrue(kw["success_url"].startswith("https://shop.test/"))  # SITE_URL, not the Host header
        attempt = PaymentAttempt.objects.get()
        self.assertEqual((attempt.amount_minor, attempt.currency), (amounts, "eur"))
        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.PENDING)

    def test_double_click_reuses_the_same_stripe_session(self):
        c = self.session_for(make_order())
        fake = {"id": "cs_test_abcdefghij12", "url": "https://checkout.stripe.test/c/pay/x"}
        with mock.patch("stripe.checkout.Session.create", return_value=fake) as create:
            c.post("/pay/stripe/create-session/")
            c.post("/pay/stripe/create-session/")
        self.assertEqual(create.call_count, 1)
        self.assertEqual(PaymentAttempt.objects.count(), 1)

    def test_payment_refused_without_a_live_econt_quote(self):
        """The placeholder 9/7 lv shipping used to let customers pay without any delivery data: 'paid, no label'."""
        order = make_order(quoted=False, receiver_street="", receiver_num="", address_line="")
        c = self.session_for(order)
        with mock.patch("stripe.checkout.Session.create") as create:
            r = c.post("/pay/stripe/create-session/")
        create.assert_not_called()
        self.assertEqual(r.status_code, 302)
        self.assertEqual(PaymentAttempt.objects.count(), 0)

    def test_create_session_is_post_only(self):
        c = self.session_for(make_order())
        with mock.patch("stripe.checkout.Session.create") as create:
            self.assertEqual(c.get("/pay/stripe/create-session/").status_code, 302)
        create.assert_not_called()

    def test_stripe_failure_shows_generic_message_without_internals(self):
        c = self.session_for(make_order())
        with mock.patch("stripe.checkout.Session.create",
                        side_effect=stripe.APIConnectionError("sk_test_secret leaked host=10.0.0.5")):
            r = c.post("/pay/stripe/create-session/", follow=True)
        self.assertNotContains(r, "sk_test_secret")
        self.assertNotContains(r, "10.0.0.5")


class NoLabelWithoutPaymentTests(CheckoutBase):
    def test_thank_you_with_a_made_up_session_id_creates_no_label_and_marks_nothing_paid(self):
        """Old code created an Econt label for the session's order on ANY ?session_id=... (free books)."""
        order = make_order(method=PaymentMethod.CARD)
        c = self.session_for(order)
        with mock.patch("stripe.checkout.Session.retrieve", side_effect=stripe.InvalidRequestError("nope", "id")):
            c.get("/checkout/thank-you/?session_id=cs_test_madeupmadeup")
        c.get("/checkout/thank-you/?session_id=garbage")
        self.assertEqual(self.econt.creates(), [])
        order.refresh_from_db()
        self.assertEqual((order.payment_status, order.shipment_status), (PaymentStatus.UNPAID, ShipmentStatus.NONE))

    def test_thank_you_with_an_unpaid_session_does_nothing(self):
        order = make_order(payment_status=PaymentStatus.PENDING)
        from .base import make_attempt

        attempt = make_attempt(order)
        c = self.session_for(order)
        with mock.patch("stripe.checkout.Session.retrieve",
                        return_value=stripe_session(order, attempt=attempt, payment_status="unpaid")):
            c.get(f"/checkout/thank-you/?session_id={attempt.stripe_session_id}")
        self.assertEqual(self.econt.creates(), [])
        self.assertEqual(Order.objects.get(pk=order.pk).payment_status, PaymentStatus.PENDING)

    def test_thank_you_with_a_verified_paid_session_is_a_safe_fallback_for_a_missing_webhook(self):
        order = make_order(payment_status=PaymentStatus.PENDING)
        from .base import make_attempt

        attempt = make_attempt(order)
        c = self.session_for(order)
        with mock.patch("stripe.checkout.Session.retrieve", return_value=stripe_session(order, attempt=attempt)), \
                self.captureOnCommitCallbacks(execute=True):
            c.get(f"/checkout/thank-you/?session_id={attempt.stripe_session_id}")
            c.get(f"/checkout/thank-you/?session_id={attempt.stripe_session_id}")  # refresh
        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.PAID)
        self.assertEqual(len(self.econt.creates()), 1)  # same code path as the webhook: exactly one label

    def test_legacy_econt_submit_endpoint_never_creates_a_label(self):
        order = make_order(method=PaymentMethod.CARD)
        c = self.session_for(order)
        c.post("/econt/submit/", {"to_office": "0", "receiver_street": "ул. Витоша", "receiver_num": "12",
                                  "receiver_postcode": "1000", "city": "София", "full_name": "Иван Иванов",
                                  "phone": "0888123456"})
        self.assertEqual(self.econt.creates(), [])
        self.assertEqual(Order.objects.get(pk=order.pk).shipment_status, ShipmentStatus.NONE)

    def test_legacy_stripe_return_url_redirects_to_thank_you(self):
        r = Client().get("/econt/collect/?session_id=cs_test_abcdefghij12")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r["Location"].startswith("/checkout/thank-you/"))


class InlineSubmitTests(CheckoutBase):
    ADDRESS = {"to_office": "0", "receiver_street": "ул. Витоша", "receiver_num": "12", "receiver_postcode": "1000",
               "full_name": "Иван Иванов", "phone": "0888123456", "city": "София",
               "billing_email": "ivan@example.com", "payment_method": "card", "delivery_method": "address"}

    def blank_order(self):
        return make_order(quoted=False, receiver_street="", receiver_num="", address_line="", email="")

    def test_address_is_validated_priced_and_stored_structured(self):
        order = self.blank_order()
        r = self.session_for(order).post("/econt/submit-inline/", self.ADDRESS)
        self.assertTrue(r.json()["ok"], r.content)
        modes = [m for m, _ in self.econt.calls]
        self.assertEqual(modes, ["validate", "calculate"])  # calculate alone does not check street/number
        order.refresh_from_db()
        self.assertEqual((order.receiver_street, order.receiver_num, order.postal_code), ("ул. Витоша", "12", "1000"))
        self.assertEqual(order.shipping_eur, Decimal("5.94"))
        self.assertIsNotNone(order.shipping_quoted_at)
        self.assertTrue(order.delivery_ready)

    def test_econt_validation_error_is_shown_and_blocks_the_quote(self):
        order = self.blank_order()
        self.econt.mode_queue["validate"] = [FakeResponse(517, econt_error_body("Невалиден пощенски код"))] * 2  # both spellings
        r = self.session_for(order).post("/econt/submit-inline/", self.ADDRESS)
        self.assertFalse(r.json()["ok"])
        self.assertIn("Невалиден пощенски код", r.json()["error"])
        order.refresh_from_db()
        self.assertIsNone(order.shipping_quoted_at)
        self.assertFalse(order.delivery_ready)

    def test_econt_outage_gives_retryable_message(self):
        order = self.blank_order()
        self.econt.mode_queue["validate"] = [requests.exceptions.ReadTimeout("slow")]
        r = self.session_for(order).post("/econt/submit-inline/", self.ADDRESS)
        self.assertFalse(r.json()["ok"])
        self.assertIn("временно", r.json()["error"])

    def test_final_selection_travels_with_the_submit_so_autosave_races_cannot_flip_the_method(self):
        order = self.blank_order()
        Order.objects.filter(pk=order.pk).update(payment_method=PaymentMethod.COD)  # stale / lost autosave
        self.session_for(order).post("/econt/submit-inline/", self.ADDRESS)  # carries payment_method=card
        order.refresh_from_db()
        self.assertEqual(order.payment_method, PaymentMethod.CARD)

    def test_bad_input_is_rejected(self):
        order = self.blank_order()
        c = self.session_for(order)
        for patch in ({"receiver_postcode": "12"}, {"receiver_num": ""}, {"phone": "abc"}, {"billing_email": "nope"},
                      {"city": "x" * 500}, {"quantity": "0"}, {"payment_method": "free"}):
            data = {**self.ADDRESS, **patch}
            r = c.post("/econt/submit-inline/", data)
            self.assertFalse(r.json()["ok"], patch)
        self.assertEqual([m for m, _ in self.econt.calls if m == "create"], [])

    def test_office_flow(self):
        order = self.blank_order()
        data = {"to_office": "1", "receiver_office_code": "1000", "full_name": "Иван Иванов", "phone": "0888123456",
                "city": "София", "billing_email": "ivan@example.com", "delivery_method": "office"}
        r = self.session_for(order).post("/econt/submit-inline/", data)
        self.assertTrue(r.json()["ok"], r.content)
        order.refresh_from_db()
        self.assertEqual((order.delivery_method, order.econt_office_code, order.receiver_street), ("office", "1000", ""))

    def test_locked_orders_cannot_be_edited(self):
        paid = make_order(payment_status=PaymentStatus.PAID)
        c = self.session_for(paid)
        self.assertEqual(c.post("/checkout/save-inline/", {"quantity": "5"}).status_code, 409)
        self.assertEqual(c.post("/econt/submit-inline/", self.ADDRESS).status_code, 409)
        self.assertEqual(Order.objects.get(pk=paid.pk).quantity, 1)


class CodConfirmationTests(CheckoutBase):
    def test_confirm_creates_one_cod_label_and_double_submit_does_not_duplicate(self):
        order = make_order(method=PaymentMethod.COD)
        c = self.session_for(order)
        self.assertEqual(c.post("/checkout/confirm-cod/").status_code, 302)
        self.assertEqual(c.post("/checkout/confirm-cod/").status_code, 302)
        self.assertEqual(len(self.econt.creates()), 1)
        sent = self.econt.creates()[0]
        self.assertEqual(sent["services"]["cdAmount"], 12.78)
        self.assertEqual(sent["services"]["cdCurrency"], "EUR")
        self.assertEqual(sent["paymentReceiverMethod"], "CASH")
        order.refresh_from_db()
        self.assertEqual(order.shipment_status, ShipmentStatus.CREATED)
        self.assertEqual(len(mail.outbox), 2)  # admin + customer, once

    def test_cod_requires_complete_quoted_delivery_data(self):
        order = make_order(method=PaymentMethod.COD, quoted=False)
        self.session_for(order).post("/checkout/confirm-cod/")
        self.assertEqual(self.econt.creates(), [])
        self.assertIsNone(Order.objects.get(pk=order.pk).cod_confirmed_at)

    def test_card_order_cannot_be_confirmed_as_cod(self):
        order = make_order(method=PaymentMethod.CARD)
        self.session_for(order).post("/checkout/confirm-cod/")
        self.assertEqual(self.econt.creates(), [])

    def test_paid_order_can_never_be_turned_into_cod(self):
        order = make_order(method=PaymentMethod.COD, payment_status=PaymentStatus.PAID, paid=True)
        self.session_for(order).post("/checkout/confirm-cod/")
        self.assertEqual(self.econt.creates(), [])

    def test_econt_rejection_is_shown_to_the_customer_who_can_fix_it_and_retry(self):
        order = make_order(method=PaymentMethod.COD)
        c = self.session_for(order)
        self.econt.queue.append(FakeResponse(517, econt_error_body("Невалиден адрес")))
        r = c.post("/checkout/confirm-cod/", follow=True)
        self.assertContains(r, "Невалиден адрес")
        order.refresh_from_db()
        self.assertIsNone(order.cod_confirmed_at)
        self.assertEqual(order.shipment_status, ShipmentStatus.NONE)
        self.assertFalse(order.needs_review)
        c.post("/checkout/confirm-cod/")  # corrected / transient: second try works
        self.assertEqual(Order.objects.get(pk=order.pk).shipment_status, ShipmentStatus.CREATED)

    def test_econt_outage_still_accepts_the_order_and_queues_it(self):
        order = make_order(method=PaymentMethod.COD)
        self.econt.queue.append(requests.exceptions.ReadTimeout("slow"))
        self.session_for(order).post("/checkout/confirm-cod/")
        order.refresh_from_db()
        self.assertEqual(order.shipment_status, ShipmentStatus.UNKNOWN)
        self.assertTrue(order.needs_review)  # nothing lost, operator alerted

    def test_cod_confirmation_expires_an_open_card_session_first(self):
        order = make_order(method=PaymentMethod.COD, payment_status=PaymentStatus.PENDING)
        from .base import make_attempt

        attempt = make_attempt(order)
        with mock.patch("stripe.checkout.Session.expire") as expire:
            self.session_for(order).post("/checkout/confirm-cod/")
        expire.assert_called_once()
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PaymentAttempt.Status.SUPERSEDED)
        self.assertEqual(len(self.econt.creates()), 1)


class AbuseTests(CheckoutBase):
    def test_order_creation_is_rate_limited_per_client(self):
        codes = [Client(REMOTE_ADDR="8.8.8.7").get("/checkout/").status_code for _ in range(32)]
        self.assertEqual(codes[:30], [200] * 30)
        self.assertEqual(codes[30:], [429, 429])
        self.assertEqual(Order.objects.count(), 30 + 0)  # the 2 extra requests created no rows

    def test_repeated_checkout_form_submission_updates_one_order(self):
        data = {
            "full_name": "Иван Иванов", "email": "ivan@example.com", "phone": "0888123456", "quantity": "2",
            "billing_full_name": "Иван Иванов", "billing_email": "ivan@example.com", "billing_phone": "0888123456",
            "billing_city": "София", "billing_street": "ул. Витоша 12", "billing_postcode": "1000",
            "ship_same_as_billing": "on", "delivery_method": "address", "payment_method": "card",
        }
        c = Client()
        c.get("/checkout/")
        for _ in range(3):
            r = c.post("/checkout/", data)
            self.assertEqual(r.status_code, 200)
        self.assertEqual(Order.objects.count(), 1)
        order = Order.objects.get()
        self.assertEqual(order.items.count(), 1)
        self.assertEqual((order.quantity, order.items.get().quantity), (2, 2))
        self.assertEqual(order.subtotal_eur, Decimal("25.56"))

    def test_checkout_form_rejects_absurd_quantity(self):
        c = Client()
        c.get("/checkout/")
        r = c.post("/checkout/", {"quantity": "1000", "full_name": "A", "email": "a@b.co", "phone": "0888123456",
                                  "payment_method": "card"})
        self.assertEqual(Order.objects.get().quantity, 1)

    def test_nomenclature_proxies_validate_input_cache_and_limit(self):
        c = Client()
        self.assertEqual(c.get("/api/econt/offices/?cityID=abc").status_code, 400)
        self.assertEqual(c.get("/api/econt/offices/").status_code, 400)
        with mock.patch("shop.econt_views.get_cities", return_value=[{"id": 1, "name": "София"}]) as g:
            for _ in range(5):
                self.assertTrue(c.get("/api/econt/cities/?q=sof").json()["ok"])
            self.assertEqual(g.call_count, 1)  # cached
        with mock.patch("shop.econt_views.get_cities", side_effect=RuntimeError("https://user:pw@host/")):
            r = c.get("/api/econt/cities/?q=other")
        self.assertEqual(r.status_code, 502)
        self.assertNotIn("pw@host", r.content.decode())  # internal error text is no longer echoed back

    def test_nomenclature_rate_limit(self):
        c = Client(REMOTE_ADDR="8.8.8.8")
        with mock.patch("shop.econt_views.get_cities", return_value=[]):
            codes = [c.get(f"/api/econt/cities/?q=a{i}").status_code for i in range(125)]
        self.assertEqual(codes[:120], [200] * 120)
        self.assertEqual(codes[-1], 429)


class ThrottleBehindProxyTests(CheckoutBase):
    """Review finding: behind nginx every visitor has REMOTE_ADDR=proxy; limits must not become site-wide."""

    def test_proxy_address_does_not_lock_out_the_whole_shop(self):
        codes = [Client(REMOTE_ADDR="127.0.0.1").get("/checkout/").status_code for _ in range(60)]
        self.assertEqual(set(codes), {200})  # 60 different visitors through one proxy IP: all served
        codes = [Client(REMOTE_ADDR="10.0.0.5").get("/api/econt/offices/?cityID=1").status_code for _ in range(150)]
        self.assertNotIn(429, codes)

    def test_proxy_flood_is_still_stopped(self):
        with mock.patch("shop.checkout.PROXY_LIMIT_MULTIPLIER", 2):  # same logic, small numbers
            codes = [Client(REMOTE_ADDR="127.0.0.1").get("/checkout/").status_code for _ in range(70)]
        self.assertEqual(codes[:60], [200] * 60)
        self.assertIn(429, codes[60:])

    def test_trusted_forwarded_for_gives_each_visitor_their_own_bucket(self):
        from django.test import override_settings

        with override_settings(TRUST_X_FORWARDED_FOR=True):
            def visit(ip, n):
                return [Client(REMOTE_ADDR="127.0.0.1", HTTP_X_FORWARDED_FOR=f"1.2.3.4, {ip}").get("/checkout/").status_code
                        for _ in range(n)]

            self.assertIn(429, visit("198.51.100.1", 32))  # one noisy client is limited ...
            self.assertEqual(set(visit("198.51.100.2", 3)), {200})  # ... others are not
            # a client-forged leading entry cannot dodge the limit: only the proxy-appended last entry counts
            forged = [Client(REMOTE_ADDR="127.0.0.1", HTTP_X_FORWARDED_FOR=f"9.9.9.{i}, 198.51.100.1").get("/checkout/")
                      .status_code for i in range(5)]
            self.assertEqual(set(forged), {429})

    def test_checkout_form_post_without_session_is_throttled_too(self):
        data = {"full_name": "A B", "email": "a@b.co", "phone": "0888123456", "quantity": "1",
                "billing_full_name": "A B", "billing_email": "a@b.co", "billing_phone": "0888123456",
                "billing_city": "София", "billing_street": "ул. Витоша 12", "billing_postcode": "1000",
                "ship_same_as_billing": "on", "delivery_method": "address", "payment_method": "card"}
        codes = [Client(REMOTE_ADDR="8.8.8.9").post("/checkout/", data).status_code for _ in range(32)]
        self.assertEqual(codes[-1], 429)
        self.assertEqual(Order.objects.count(), 30)

    def test_thank_you_does_not_call_stripe_more_than_the_limit(self):
        c = Client(REMOTE_ADDR="8.8.8.10")
        with mock.patch("stripe.checkout.Session.retrieve", side_effect=stripe.InvalidRequestError("x", "id")) as r:
            for i in range(30):
                c.get(f"/checkout/thank-you/?session_id=cs_test_abcdefghij{i:02d}")
        self.assertEqual(r.call_count, 20)

    def test_thank_you_keeps_an_unrelated_in_progress_order_in_the_session(self):
        from .base import make_attempt

        current = make_order()  # customer is working on this one
        other = make_order(payment_status=PaymentStatus.PENDING)
        attempt = make_attempt(other)
        c = self.session_for(current)
        with mock.patch("stripe.checkout.Session.retrieve", return_value=stripe_session(other, attempt=attempt)), \
                self.captureOnCommitCallbacks(execute=True):
            c.get(f"/checkout/thank-you/?session_id={attempt.stripe_session_id}")
        self.assertEqual(c.session.get("current_order_id"), current.pk)


class StreetSpellingTests(CheckoutBase):
    """[demo] Econt rejects some street spellings ('ул. Витоша' ambiguous, 'ул. Княз Александър' unknown)."""
    DATA = InlineSubmitTests.ADDRESS

    def blank(self):
        return make_order(quoted=False, receiver_street="", receiver_num="", address_line="", email="")

    def test_variants_helper(self):
        from shop.address import street_variants

        self.assertEqual(street_variants("ул. Витоша"), ["ул. Витоша", "Витоша"])
        self.assertEqual(street_variants("  улица   Витоша "), ["улица Витоша", "Витоша"])
        self.assertEqual(street_variants("бул. Витоша"), ["бул. Витоша"])  # boulevards are accepted as typed
        self.assertEqual(street_variants("Витоша"), ["Витоша"])
        self.assertEqual(street_variants(""), [])
        self.assertEqual(street_variants("ул.Витоша"), ["ул.Витоша", "Витоша"])
        self.assertEqual(street_variants("Улов"), ["Улов"])  # a street that merely starts with the letters is untouched

    def test_bare_street_name_is_used_when_econt_rejects_the_prefixed_spelling(self):
        order = self.blank()
        self.econt.mode_queue["validate"] = [FakeResponse(517, econt_error_body("Улицата е налична в квартали: Х, У")),
                                             FakeResponse(200, {"label": {}})]
        r = self.session_for(order).post("/econt/submit-inline/", {**self.DATA, "receiver_street": "ул. Витоша"})
        self.assertTrue(r.json()["ok"], r.content)
        order.refresh_from_db()
        self.assertEqual(order.receiver_street, "Витоша")  # the spelling Econt validated is what labels will use
        sent = [b["label"]["receiverAddress"]["street"] for m, b in self.econt.calls if m == "validate"]
        self.assertEqual(sent, ["ул. Витоша", "Витоша"])

    def test_if_every_spelling_fails_the_customer_sees_econts_reason_and_keeps_what_they_typed(self):
        order = self.blank()
        self.econt.mode_queue["validate"] = [FakeResponse(517, econt_error_body("Моля, посочете квартал")),
                                             FakeResponse(517, econt_error_body("Друга грешка"))]
        r = self.session_for(order).post("/econt/submit-inline/", {**self.DATA, "receiver_street": "ул. Витоша"})
        self.assertFalse(r.json()["ok"])
        self.assertIn("квартал", r.json()["error"])
        order.refresh_from_db()
        self.assertEqual(order.receiver_street, "ул. Витоша")
        self.assertIsNone(order.shipping_quoted_at)

    def test_outage_does_not_try_other_spellings(self):
        order = self.blank()
        self.econt.mode_queue["validate"] = [requests.exceptions.ReadTimeout("slow")]
        r = self.session_for(order).post("/econt/submit-inline/", {**self.DATA, "receiver_street": "ул. Витоша"})
        self.assertFalse(r.json()["ok"])
        self.assertEqual(len([1 for m, _ in self.econt.calls if m == "validate"]), 1)

    def test_quarter_and_block_reach_the_econt_payload(self):
        from shop import fulfillment

        order = self.blank()
        r = self.session_for(order).post("/econt/submit-inline/", {
            **self.DATA, "receiver_quarter": "Люлин", "receiver_other": "бл. 5"})
        self.assertTrue(r.json()["ok"], r.content)
        order.refresh_from_db()
        Order.objects.filter(pk=order.pk).update(payment_status=PaymentStatus.PAID, paid=True)
        fulfillment.request_shipment(order.pk)
        fulfillment.attempt_shipment(order.pk)
        addr = self.econt.creates()[0]["receiverAddress"]
        self.assertEqual((addr["quarter"], addr["other"]), ("Люлин", "бл. 5"))

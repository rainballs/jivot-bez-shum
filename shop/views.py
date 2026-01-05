# shop/views.py
import json
import logging
from decimal import Decimal, ROUND_HALF_UP

import re
from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseRedirect, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from .utils import send_order_notification
from .econt_service import create_econt_label
from django.views.decorators.http import require_POST, require_GET

import stripe

logger = logging.getLogger('gunicorn.error')

from .forms import CheckoutInfoForm, PaymentMethodForm
from .models import Order, OrderItem, PaymentMethod, Product, DeliveryMethod

# Configure Stripe once (safe even if keys are empty; we check before use)
stripe.api_key = settings.STRIPE_SECRET_LIVE_KEY


# ---------- Helpers ----------
def get_single_product():
    qs = Product.objects.filter(is_active=True).order_by("id")
    return qs.first() or Product.objects.first()


def _site_url(request):
    scheme = "https" if request.is_secure() else "http"
    return f"{scheme}://{request.get_host()}"


def stripe_success_url(request):
    return _site_url(request) + reverse("thank_you")


def stripe_cancel_url(request):
    return _site_url(request) + reverse("checkout_info")


def _to_minor_units(amount: Decimal) -> int:
    """BGN minor units (stotinki)."""
    return int((amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100))


def _ship_bgn_for(order) -> Decimal:
    """
    Ако имаме изчислена цена от Еконт (shipping_bgn > 0),
    ползваме нея. Иначе падаме обратно на 9 / 7 лв.
    """
    try:
        if getattr(order, "shipping_bgn", None):
            # shipping_bgn е Decimal – просто я връщаме
            if order.shipping_bgn > 0:
                return order.shipping_bgn
    except Exception:
        pass

    # fallback старото поведение
    try:
        return Decimal("9.00") if order.delivery_method == DeliveryMethod.TO_ADDRESS else Decimal("7.00")
    except Exception:
        return Decimal("7.00")


def stripe_checkout_line_items(order: Order, product: Product):
    unit_cents = _to_minor_units(product.price_bgn)
    ship_cents = _to_minor_units(_ship_bgn_for(order))
    return [
        {
            "price_data": {
                "currency": "eur",
                "product_data": {"name": product.name},
                "unit_amount": unit_cents,
            },
            "quantity": order.quantity,
        },
        {
            "price_data": {
                "currency": "eur",
                "product_data": {"name": "Доставка"},
                "unit_amount": ship_cents,
            },
            "quantity": 1,
        },
    ]


def _get_current_order(request):
    oid = request.session.get("current_order_id")
    if not oid:
        oid = request.GET.get("order_id") or request.POST.get("order_id")
    return Order.objects.filter(pk=oid).first() if oid else None


# ---------- Pages ----------
def home(request):
    product = get_single_product()
    return render(request, "pages/home.html", {"product": product})


@transaction.atomic
def checkout_info(request):
    product = get_single_product()
    if not product:
        messages.error(request, "Няма наличен продукт.")
        return redirect("home")

    if request.method == "POST":
        # your old POST stays the same
        info_form = CheckoutInfoForm(request.POST)
        pay_form = PaymentMethodForm(request.POST)

        if info_form.is_valid() and pay_form.is_valid():
            order = info_form.save(commit=False)

            dm = request.POST.get("delivery_method", "address")
            order.delivery_method = (
                DeliveryMethod.TO_ADDRESS if dm == "address" else DeliveryMethod.TO_OFFICE
            )
            order.payment_method = pay_form.cleaned_data["payment_method"]

            if order.ship_same_as_billing:
                order.full_name = order.billing_full_name or order.full_name
                order.email = order.billing_email or order.email
                order.phone = order.billing_phone or order.phone
                order.city = order.billing_city or order.city
                order.postal_code = order.billing_postcode or order.postal_code
                order.address_line = order.billing_street or order.address_line

            order.quantity = info_form.cleaned_data["quantity"]
            order.paid = False

            # 🔴 NEW: hard validation for Econt before saving the order
            missing_parts = []

            if order.delivery_method == DeliveryMethod.TO_ADDRESS:
                if not (order.full_name or "").strip():
                    missing_parts.append("име и фамилия")
                if not (order.phone or "").strip():
                    missing_parts.append("телефон")
                if not (order.city or "").strip():
                    missing_parts.append("град")
                if not (order.postal_code or "").strip():
                    missing_parts.append("пощенски код")
                if not (order.address_line or "").strip():
                    missing_parts.append("улица и номер")

            else:  # TO_OFFICE
                if not (order.city or "").strip():
                    missing_parts.append("град")
                if not (getattr(order, "econt_office_code", "") or "").strip():
                    missing_parts.append("офис на Еконт")

            if missing_parts:
                messages.error(
                    request,
                    "За да продължите, попълнете: " + ", ".join(missing_parts) + "."
                )
                return render(
                    request,
                    "checkout/info.html",
                    {
                        "product": product,
                        "form": info_form,
                        "pay_form": pay_form,
                    },
                )

            # ✅ all required fields for Econt are present – safe to save
            order.save()

            # line item
            OrderItem.objects.create(
                order=order,
                product=product,
                quantity=order.quantity,
                unit_price_bgn=product.price_bgn,
                unit_price_eur=product.price_eur,
            )

            # totals
            order.recompute_totals()
            order.save(update_fields=[
                "subtotal_bgn", "subtotal_eur",
                "shipping_bgn", "shipping_eur",
                "total_bgn", "total_eur",
                "paid", "payment_method"
            ])

            # remember
            request.session["current_order_id"] = order.id

            # re-render same page
            return render(
                request,
                "checkout/info.html",
                {
                    "product": product,
                    "form": info_form,
                    "pay_form": pay_form,
                    "order": order,
                },
            )

        messages.error(request, "Моля, коригирайте грешките във формата.")
        return render(request, "checkout/info.html", {
            "product": product,
            "form": info_form,
            "pay_form": pay_form,
        })

    # ←–––––––––––––––––––––––––––––––––––––––––––––
    # GET branch: if we DON'T have an order yet, create a normal one,
    # using the same product and the same shipping logic.
    # This keeps your totals correct.
    # ––––––––––––––––––––––––––––––––––––––––––––→
    order = _get_current_order(request)
    if not order:
        # create a real order, not a dummy
        order = Order.objects.create(
            quantity=1,
            delivery_method=DeliveryMethod.TO_ADDRESS,
            payment_method=PaymentMethod.COD,
            paid=False,
        )
        # create line item so recompute_totals works the same
        OrderItem.objects.create(
            order=order,
            product=product,
            quantity=1,
            unit_price_bgn=product.price_bgn,
            unit_price_eur=product.price_eur,
        )
        order.recompute_totals()
        order.save()
        request.session["current_order_id"] = order.id

    # build forms from the order
    info_form = CheckoutInfoForm(instance=order)
    pay_form = PaymentMethodForm(initial={"payment_method": order.payment_method})

    return render(
        request,
        "checkout/info.html",
        {
            "product": product,
            "form": info_form,
            "pay_form": pay_form,
            "order": order,
        },
    )


@transaction.atomic
def checkout_payment(request):
    order = _get_current_order(request)
    if not order:
        return redirect("checkout_info")
    product = get_single_product()

    if request.method == "POST":
        form = PaymentMethodForm(request.POST, instance=order)
        if form.is_valid():
            order = form.save()

            # Anything that's not COD goes to Stripe
            if order.payment_method in {PaymentMethod.CARD, PaymentMethod.APPLE_PAY, PaymentMethod.GOOGLE_PAY}:
                if not settings.STRIPE_PUBLIC_LIVE_KEY or not settings.STRIPE_SECRET_LIVE_KEY:
                    messages.error(request, "Stripe не е конфигуриран (липсват STRIPE_PUBLIC_KEY / STRIPE_SECRET_KEY).")
                    return redirect("checkout_payment")
                return redirect("stripe_create_session")

            # COD → go to the proper Econt page (address/office)
            order.paid = False
            order.save(update_fields=["paid"])
            request.session.pop("stripe_session_id", None)
            if order.delivery_method == DeliveryMethod.TO_ADDRESS:
                return redirect("econt_collect_address")
            else:
                return redirect("econt_collect_office")

        messages.error(request, "Моля, изберете метод на плащане.")
    else:
        initial = {"payment_method": order.payment_method or PaymentMethod.CARD}
        form = PaymentMethodForm(instance=order, initial=initial)

    return render(request, "checkout/payment.html", {
        "product": product,
        "order": order,
        "form": form
    })


# ---------- Stripe integration ----------
from django.urls import reverse


def stripe_create_checkout_session(request):
    order = _get_current_order(request)
    if not order:
        return redirect("checkout_info")
    product = get_single_product()
    if not product:
        messages.error(request, "Няма наличен продукт.")
        return redirect("home")

    # NEW: always go to thank_you after Stripe succeeds
    success_url = _site_url(request) + reverse("thank_you") + "?session_id={CHECKOUT_SESSION_ID}"

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            line_items=stripe_checkout_line_items(order, product),
            metadata={
                "order_id": str(order.id),
                "delivery_method": str(order.delivery_method),
            },
            success_url=success_url,  # ⬅️ now to thank_you
            cancel_url=stripe_cancel_url(request),
            currency="eur",
            customer_email=order.email or None,
        )
    except Exception as e:
        messages.error(request, f"Грешка при свързване със Stripe: {e}")
        # fallback: still show thank_you, or you can send to checkout_info
        return redirect("thank_you")

    request.session["stripe_session_id"] = session.id
    return HttpResponseRedirect(session.url)


@csrf_exempt
def stripe_webhook(request):
    payload = request.body
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE")
    secret = settings.STRIPE_WEBHOOK_SECRET

    if not secret:
        return HttpResponseBadRequest("Missing STRIPE_WEBHOOK_SECRET")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, secret)
    except (ValueError, stripe.error.SignatureVerificationError):
        return HttpResponse(status=400)

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        order_id = (session.get("metadata") or {}).get("order_id")
        if order_id:
            try:
                order = Order.objects.get(pk=order_id)
            except Order.DoesNotExist:
                return HttpResponse(status=200)

            if not order.paid:
                order.paid = True
                order.save(update_fields=["paid"])
                try:
                    send_order_notification(order, event="paid")
                except Exception:
                    pass

            # optional: create econt label ONLY if delivery data is already there
            # (i.e. user used the inline form)
            if order.city and (order.econt_office_code or order.address_line):
                try:
                    create_econt_label(order)
                except Exception:
                    pass

    return HttpResponse(status=200)


def _split_street_num(line: str) -> tuple[str, str]:
    """
    Same logic as in econt_views: "ul Oborishte 70" -> ("ul Oborishte", "70")
    """
    if not line:
        return "", ""
    s = line.strip()

    m = re.search(r'(?:№\s*)(\d+[A-Za-zА-Яа-я\-\/]*)\s*$', s)
    if not m:
        m = re.search(r'\s(\d+[A-Za-zА-Яа-я\-\/]*)\s*$', s)

    if m:
        num = m.group(1)
        street = s[:m.start(1)].rstrip(' ,№')
        return street.strip(), num.strip()

    return s, ""


def thank_you(request):
    """
    Final page after checkout.
    If Stripe sent us back with ?session_id=..., verify it and mark the order .paid = True
    so the admin doesn’t stay with a red X.
    """
    session_id = request.GET.get("session_id")
    order = _get_current_order(request)

    if session_id and settings.STRIPE_SECRET_LIVE_KEY:
        try:
            sess = stripe.checkout.Session.retrieve(
                session_id,
                api_key=settings.STRIPE_SECRET_LIVE_KEY,
                expand=["payment_intent"],
            )
            # metadata should contain order_id because we put it in stripe_create_checkout_session
            meta = sess.get("metadata") or {}
            stripe_order_id = meta.get("order_id")

            if sess.get("payment_status") == "paid" and stripe_order_id:
                try:
                    paid_order = Order.objects.get(pk=stripe_order_id)
                except Order.DoesNotExist:
                    paid_order = None
                else:
                    if not paid_order.paid:
                        paid_order.paid = True
                        paid_order.save(update_fields=["paid"])
                        # try:
                        #     send_order_notification(paid_order, event="paid")
                        # except Exception:
                        #     pass
                    order = paid_order
                    request.session["current_order_id"] = paid_order.pk
        except Exception as e:
            logger.error("Stripe verify on thank_you failed: %s", e)

        # 2) try to create Econt label NOW (only for card flow)
        if order:
            overrides = {}

            if order.delivery_method == DeliveryMethod.TO_OFFICE:
                # we expect this to be filled in econt_submit_inline earlier
                if order.econt_office_code:
                    overrides["receiver_office_code"] = order.econt_office_code

            else:  # TO_ADDRESS
                # take whatever we have on the order and split it
                street_line = (
                        (order.address_line or "")
                        or (getattr(order, "billing_street", "") or "")
                )
                street, num = _split_street_num(street_line)

                overrides["receiver_street"] = street
                if num:
                    overrides["receiver_num"] = num

                # postcode – try both names
                postcode = (
                        getattr(order, "postal_code", "")
                        or getattr(order, "billing_postcode", "")
                        or getattr(order, "billing_postal_code", "")
                )
                if postcode:
                    overrides["receiver_postcode"] = postcode

            # only call Econt if we actually have something meaningful
            try:
                create_econt_label(order, overrides=overrides)
            except Exception as e:
                # don't break the thank-you page
                logger.error("Econt label after Stripe failed for order %s: %s", order.pk, e)

    # clear session so refresh doesn’t reuse the same order
    request.session.pop("current_order_id", None)

    return render(request, "checkout/thank_you.html", {"order": order})


@require_POST
def checkout_inline_update(request):
    """
    Lightweight AJAX updater for current order:
    - payment_method
    - delivery_method
    - quantity
    """
    order = _get_current_order(request)
    if not order:
        return JsonResponse({"ok": False, "error": "No current order"}, status=404)

    pm = request.POST.get("payment_method")
    dm = request.POST.get("delivery_method")
    qty = request.POST.get("quantity")

    changed = False

    if pm:
        order.payment_method = pm
        changed = True

    if dm:
        from .models import DeliveryMethod
        order.delivery_method = (
            DeliveryMethod.TO_ADDRESS if dm == "address" else DeliveryMethod.TO_OFFICE
        )
        changed = True

    if qty:
        try:
            q = int(qty)
            if q > 0:
                order.quantity = q
                # if you want totals to be recomputed here:
                order.recompute_totals()
                changed = True
        except ValueError:
            pass

    if changed:
        order.save()

    return JsonResponse({"ok": True})


def _get_current_order(request):
    oid = request.session.get("current_order_id")
    return Order.objects.filter(pk=oid).first() if oid else None


@require_POST
def checkout_save_inline(request):
    """
    Called from JS on the checkout page whenever the user changes
    top fields (billing, payment, delivery).
    """
    order = _get_current_order(request)
    if not order:
        order = Order.objects.create()
        request.session["current_order_id"] = order.pk

    # billing
    for field in [
        "billing_full_name",
        "billing_email",
        "billing_phone",
        "billing_city",
        "billing_street",
        "billing_postcode",
    ]:
        val = request.POST.get(field)
        if val is not None:
            setattr(order, field, val)

    # “use same address”
    same = request.POST.get("ship_same_as_billing")
    if same is not None:
        order.ship_same_as_billing = (same == "true")

    # delivery
    dm = request.POST.get("delivery_method")
    if dm == "address":
        order.delivery_method = DeliveryMethod.TO_ADDRESS
    elif dm == "office":
        order.delivery_method = DeliveryMethod.TO_OFFICE

    # payment
    pm = request.POST.get("payment_method")
    if pm:
        # adapt names if yours are different
        if pm == "card":
            order.payment_method = PaymentMethod.CARD
        else:
            order.payment_method = PaymentMethod.COD

    order.save()
    return JsonResponse({"ok": True})


@require_GET
def checkout_summary(request, order_id=None):
    """
    Read-only view of the current order:
    - uses current_order_id from session, OR
    - explicit order_id in the URL (for links from emails/admin, etc.)
    """
    order = None

    if order_id is not None:
        order = get_object_or_404(Order, pk=order_id)
        # remember it in the session so other flows can reuse it
        request.session["current_order_id"] = order.pk
    else:
        order = _get_current_order(request)
        if not order:
            messages.error(request, "Няма активна поръчка.")
            return redirect("checkout_info")

    # Get product from the items if present, else fall back to your helper
    item = order.items.first()
    if item:
        product = item.product
    else:
        product = get_single_product()

    return render(
        request,
        "checkout/summary_readonly.html",
        {
            "order": order,
            "product": product,
        },
    )


@require_POST
def checkout_confirm_cod(request):
    order = _get_current_order(request)
    if not order:
        messages.error(request, "Няма активна поръчка.")
        return redirect("checkout_info")

    if order.payment_method != PaymentMethod.COD:
        messages.error(request, "Тази поръчка не е с наложен платеж.")
        return redirect("checkout_summary")

    res = create_econt_label(order, overrides={})
    if not res.get("ok"):
        messages.error(request, f"Грешка при Еконт: {res.get('error') or 'Неуспешно създаване на товарителница.'}")
        return redirect("checkout_summary")

    try:
        send_order_notification(order, event="created")
    except Exception:
        pass

    return redirect("thank_you")
# shop/views.py
import json
import logging
from decimal import Decimal, ROUND_HALF_UP

import re
from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseRedirect, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from .utils import send_order_notification
from .econt_service import create_econt_label
from django.views.decorators.http import require_POST, require_GET

import stripe

logger = logging.getLogger('gunicorn.error')

from .forms import CheckoutInfoForm, PaymentMethodForm
from .models import Order, OrderItem, PaymentMethod, Product, DeliveryMethod

# Configure Stripe once (safe even if keys are empty; we check before use)
stripe.api_key = settings.STRIPE_SECRET_LIVE_KEY


# ---------- Helpers ----------
def get_single_product():
    qs = Product.objects.filter(is_active=True).order_by("id")
    return qs.first() or Product.objects.first()


def _site_url(request):
    scheme = "https" if request.is_secure() else "http"
    return f"{scheme}://{request.get_host()}"


def stripe_success_url(request):
    return _site_url(request) + reverse("thank_you")


def stripe_cancel_url(request):
    return _site_url(request) + reverse("checkout_info")


def _to_minor_units(amount: Decimal) -> int:
    """BGN minor units (stotinki)."""
    return int((amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100))


def _ship_bgn_for(order) -> Decimal:
    """
    Ако имаме изчислена цена от Еконт (shipping_bgn > 0),
    ползваме нея. Иначе падаме обратно на 9 / 7 лв.
    """
    try:
        if getattr(order, "shipping_bgn", None):
            # shipping_bgn е Decimal – просто я връщаме
            if order.shipping_bgn > 0:
                return order.shipping_bgn
    except Exception:
        pass

    # fallback старото поведение
    try:
        return Decimal("9.00") if order.delivery_method == DeliveryMethod.TO_ADDRESS else Decimal("7.00")
    except Exception:
        return Decimal("7.00")


def stripe_checkout_line_items(order: Order, product: Product):
    unit_cents = _to_minor_units(product.price_bgn)
    ship_cents = _to_minor_units(_ship_bgn_for(order))
    return [
        {
            "price_data": {
                "currency": "bgn",
                "product_data": {"name": product.name},
                "unit_amount": unit_cents,
            },
            "quantity": order.quantity,
        },
        {
            "price_data": {
                "currency": "bgn",
                "product_data": {"name": "Доставка"},
                "unit_amount": ship_cents,
            },
            "quantity": 1,
        },
    ]


def _get_current_order(request):
    oid = request.session.get("current_order_id")
    if not oid:
        oid = request.GET.get("order_id") or request.POST.get("order_id")
    return Order.objects.filter(pk=oid).first() if oid else None


# ---------- Pages ----------
def home(request):
    product = get_single_product()
    return render(request, "pages/home.html", {"product": product})


@transaction.atomic
def checkout_info(request):
    product = get_single_product()
    if not product:
        messages.error(request, "Няма наличен продукт.")
        return redirect("home")

    if request.method == "POST":
        # your old POST stays the same
        info_form = CheckoutInfoForm(request.POST)
        pay_form = PaymentMethodForm(request.POST)

        if info_form.is_valid() and pay_form.is_valid():
            order = info_form.save(commit=False)

            dm = request.POST.get("delivery_method", "address")
            order.delivery_method = (
                DeliveryMethod.TO_ADDRESS if dm == "address" else DeliveryMethod.TO_OFFICE
            )
            order.payment_method = pay_form.cleaned_data["payment_method"]

            if order.ship_same_as_billing:
                order.full_name = order.billing_full_name or order.full_name
                order.email = order.billing_email or order.email
                order.phone = order.billing_phone or order.phone
                order.city = order.billing_city or order.city
                order.postal_code = order.billing_postcode or order.postal_code
                order.address_line = order.billing_street or order.address_line

            order.quantity = info_form.cleaned_data["quantity"]
            order.paid = False

            # 🔴 NEW: hard validation for Econt before saving the order
            missing_parts = []

            if order.delivery_method == DeliveryMethod.TO_ADDRESS:
                if not (order.full_name or "").strip():
                    missing_parts.append("име и фамилия")
                if not (order.phone or "").strip():
                    missing_parts.append("телефон")
                if not (order.city or "").strip():
                    missing_parts.append("град")
                if not (order.postal_code or "").strip():
                    missing_parts.append("пощенски код")
                if not (order.address_line or "").strip():
                    missing_parts.append("улица и номер")

            else:  # TO_OFFICE
                if not (order.city or "").strip():
                    missing_parts.append("град")
                if not (getattr(order, "econt_office_code", "") or "").strip():
                    missing_parts.append("офис на Еконт")

            if missing_parts:
                messages.error(
                    request,
                    "За да продължите, попълнете: " + ", ".join(missing_parts) + "."
                )
                return render(
                    request,
                    "checkout/info.html",
                    {
                        "product": product,
                        "form": info_form,
                        "pay_form": pay_form,
                    },
                )

            # ✅ all required fields for Econt are present – safe to save
            order.save()

            # line item
            OrderItem.objects.create(
                order=order,
                product=product,
                quantity=order.quantity,
                unit_price_bgn=product.price_bgn,
                unit_price_eur=product.price_eur,
            )

            # totals
            order.recompute_totals()
            order.save(update_fields=[
                "subtotal_bgn", "subtotal_eur",
                "shipping_bgn", "shipping_eur",
                "total_bgn", "total_eur",
                "paid", "payment_method"
            ])

            # remember
            request.session["current_order_id"] = order.id

            # re-render same page
            return render(
                request,
                "checkout/info.html",
                {
                    "product": product,
                    "form": info_form,
                    "pay_form": pay_form,
                    "order": order,
                },
            )

        messages.error(request, "Моля, коригирайте грешките във формата.")
        return render(request, "checkout/info.html", {
            "product": product,
            "form": info_form,
            "pay_form": pay_form,
        })

    # ←–––––––––––––––––––––––––––––––––––––––––––––
    # GET branch: if we DON'T have an order yet, create a normal one,
    # using the same product and the same shipping logic.
    # This keeps your totals correct.
    # ––––––––––––––––––––––––––––––––––––––––––––→
    order = _get_current_order(request)
    if not order:
        # create a real order, not a dummy
        order = Order.objects.create(
            quantity=1,
            delivery_method=DeliveryMethod.TO_ADDRESS,
            payment_method=PaymentMethod.COD,
            paid=False,
        )
        # create line item so recompute_totals works the same
        OrderItem.objects.create(
            order=order,
            product=product,
            quantity=1,
            unit_price_bgn=product.price_bgn,
            unit_price_eur=product.price_eur,
        )
        order.recompute_totals()
        order.save()
        request.session["current_order_id"] = order.id

    # build forms from the order
    info_form = CheckoutInfoForm(instance=order)
    pay_form = PaymentMethodForm(initial={"payment_method": order.payment_method})

    return render(
        request,
        "checkout/info.html",
        {
            "product": product,
            "form": info_form,
            "pay_form": pay_form,
            "order": order,
        },
    )


@transaction.atomic
def checkout_payment(request):
    order = _get_current_order(request)
    if not order:
        return redirect("checkout_info")
    product = get_single_product()

    if request.method == "POST":
        form = PaymentMethodForm(request.POST, instance=order)
        if form.is_valid():
            order = form.save()

            # Anything that's not COD goes to Stripe
            if order.payment_method in {PaymentMethod.CARD, PaymentMethod.APPLE_PAY, PaymentMethod.GOOGLE_PAY}:
                if not settings.STRIPE_PUBLIC_LIVE_KEY or not settings.STRIPE_SECRET_LIVE_KEY:
                    messages.error(request, "Stripe не е конфигуриран (липсват STRIPE_PUBLIC_KEY / STRIPE_SECRET_KEY).")
                    return redirect("checkout_payment")
                return redirect("stripe_create_session")

            # COD → go to the proper Econt page (address/office)
            order.paid = False
            order.save(update_fields=["paid"])
            request.session.pop("stripe_session_id", None)
            if order.delivery_method == DeliveryMethod.TO_ADDRESS:
                return redirect("econt_collect_address")
            else:
                return redirect("econt_collect_office")

        messages.error(request, "Моля, изберете метод на плащане.")
    else:
        initial = {"payment_method": order.payment_method or PaymentMethod.CARD}
        form = PaymentMethodForm(instance=order, initial=initial)

    return render(request, "checkout/payment.html", {
        "product": product,
        "order": order,
        "form": form
    })


# ---------- Stripe integration ----------
from django.urls import reverse


def stripe_create_checkout_session(request):
    order = _get_current_order(request)
    if not order:
        return redirect("checkout_info")
    product = get_single_product()
    if not product:
        messages.error(request, "Няма наличен продукт.")
        return redirect("home")

    # NEW: always go to thank_you after Stripe succeeds
    success_url = _site_url(request) + reverse("thank_you") + "?session_id={CHECKOUT_SESSION_ID}"

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            line_items=stripe_checkout_line_items(order, product),
            metadata={
                "order_id": str(order.id),
                "delivery_method": str(order.delivery_method),
            },
            success_url=success_url,  # ⬅️ now to thank_you
            cancel_url=stripe_cancel_url(request),
            currency="eur",
            customer_email=order.email or None,
        )
    except Exception as e:
        messages.error(request, f"Грешка при свързване със Stripe: {e}")
        # fallback: still show thank_you, or you can send to checkout_info
        return redirect("thank_you")

    request.session["stripe_session_id"] = session.id
    return HttpResponseRedirect(session.url)


@csrf_exempt
def stripe_webhook(request):
    payload = request.body
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE")
    secret = settings.STRIPE_WEBHOOK_SECRET

    if not secret:
        return HttpResponseBadRequest("Missing STRIPE_WEBHOOK_SECRET")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, secret)
    except (ValueError, stripe.error.SignatureVerificationError):
        return HttpResponse(status=400)

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        order_id = (session.get("metadata") or {}).get("order_id")
        if order_id:
            try:
                order = Order.objects.get(pk=order_id)
            except Order.DoesNotExist:
                return HttpResponse(status=200)

            if not order.paid:
                order.paid = True
                order.save(update_fields=["paid"])
                try:
                    send_order_notification(order, event="paid")
                except Exception:
                    pass

            # optional: create econt label ONLY if delivery data is already there
            # (i.e. user used the inline form)
            if order.city and (order.econt_office_code or order.address_line):
                try:
                    create_econt_label(order)
                except Exception:
                    pass

    return HttpResponse(status=200)


def _split_street_num(line: str) -> tuple[str, str]:
    """
    Same logic as in econt_views: "ul Oborishte 70" -> ("ul Oborishte", "70")
    """
    if not line:
        return "", ""
    s = line.strip()

    m = re.search(r'(?:№\s*)(\d+[A-Za-zА-Яа-я\-\/]*)\s*$', s)
    if not m:
        m = re.search(r'\s(\d+[A-Za-zА-Яа-я\-\/]*)\s*$', s)

    if m:
        num = m.group(1)
        street = s[:m.start(1)].rstrip(' ,№')
        return street.strip(), num.strip()

    return s, ""


def thank_you(request):
    """
    Final page after checkout.
    If Stripe sent us back with ?session_id=..., verify it and mark the order .paid = True
    so the admin doesn’t stay with a red X.
    """
    session_id = request.GET.get("session_id")
    order = _get_current_order(request)

    if session_id and settings.STRIPE_SECRET_LIVE_KEY:
        try:
            sess = stripe.checkout.Session.retrieve(
                session_id,
                api_key=settings.STRIPE_SECRET_LIVE_KEY,
                expand=["payment_intent"],
            )
            # metadata should contain order_id because we put it in stripe_create_checkout_session
            meta = sess.get("metadata") or {}
            stripe_order_id = meta.get("order_id")

            if sess.get("payment_status") == "paid" and stripe_order_id:
                try:
                    paid_order = Order.objects.get(pk=stripe_order_id)
                except Order.DoesNotExist:
                    paid_order = None
                else:
                    if not paid_order.paid:
                        paid_order.paid = True
                        paid_order.save(update_fields=["paid"])
                        try:
                            send_order_notification(paid_order, event="paid")
                        except Exception:
                            pass
                    order = paid_order
                    request.session["current_order_id"] = paid_order.pk
        except Exception as e:
            logger.error("Stripe verify on thank_you failed: %s", e)

        # 2) try to create Econt label NOW (only for card flow)
        if order:
            overrides = {}

            if order.delivery_method == DeliveryMethod.TO_OFFICE:
                # we expect this to be filled in econt_submit_inline earlier
                if order.econt_office_code:
                    overrides["receiver_office_code"] = order.econt_office_code

            else:  # TO_ADDRESS
                # take whatever we have on the order and split it
                street_line = (
                        (order.address_line or "")
                        or (getattr(order, "billing_street", "") or "")
                )
                street, num = _split_street_num(street_line)

                overrides["receiver_street"] = street
                if num:
                    overrides["receiver_num"] = num

                # postcode – try both names
                postcode = (
                        getattr(order, "postal_code", "")
                        or getattr(order, "billing_postcode", "")
                        or getattr(order, "billing_postal_code", "")
                )
                if postcode:
                    overrides["receiver_postcode"] = postcode

            # only call Econt if we actually have something meaningful
            try:
                create_econt_label(order, overrides=overrides)
            except Exception as e:
                # don't break the thank-you page
                logger.error("Econt label after Stripe failed for order %s: %s", order.pk, e)

    # clear session so refresh doesn’t reuse the same order
    request.session.pop("current_order_id", None)

    return render(request, "checkout/thank_you.html", {"order": order})


@require_POST
def checkout_inline_update(request):
    """
    Lightweight AJAX updater for current order:
    - payment_method
    - delivery_method
    - quantity
    """
    order = _get_current_order(request)
    if not order:
        return JsonResponse({"ok": False, "error": "No current order"}, status=404)

    pm = request.POST.get("payment_method")
    dm = request.POST.get("delivery_method")
    qty = request.POST.get("quantity")

    changed = False

    if pm:
        order.payment_method = pm
        changed = True

    if dm:
        from .models import DeliveryMethod
        order.delivery_method = (
            DeliveryMethod.TO_ADDRESS if dm == "address" else DeliveryMethod.TO_OFFICE
        )
        changed = True

    if qty:
        try:
            q = int(qty)
            if q > 0:
                order.quantity = q
                # if you want totals to be recomputed here:
                order.recompute_totals()
                changed = True
        except ValueError:
            pass

    if changed:
        order.save()

    return JsonResponse({"ok": True})


def _get_current_order(request):
    oid = request.session.get("current_order_id")
    return Order.objects.filter(pk=oid).first() if oid else None


@require_POST
def checkout_save_inline(request):
    """
    Called from JS on the checkout page whenever the user changes
    top fields (billing, payment, delivery).
    """
    order = _get_current_order(request)
    if not order:
        order = Order.objects.create()
        request.session["current_order_id"] = order.pk

    # billing
    for field in [
        "billing_full_name",
        "billing_email",
        "billing_phone",
        "billing_city",
        "billing_street",
        "billing_postcode",
    ]:
        val = request.POST.get(field)
        if val is not None:
            setattr(order, field, val)

    # “use same address”
    same = request.POST.get("ship_same_as_billing")
    if same is not None:
        order.ship_same_as_billing = (same == "true")

    # delivery
    dm = request.POST.get("delivery_method")
    if dm == "address":
        order.delivery_method = DeliveryMethod.TO_ADDRESS
    elif dm == "office":
        order.delivery_method = DeliveryMethod.TO_OFFICE

    # payment
    pm = request.POST.get("payment_method")
    if pm:
        # adapt names if yours are different
        if pm == "card":
            order.payment_method = PaymentMethod.CARD
        else:
            order.payment_method = PaymentMethod.COD

    order.save()
    return JsonResponse({"ok": True})


@require_GET
def checkout_summary(request, order_id=None):
    """
    Read-only view of the current order:
    - uses current_order_id from session, OR
    - explicit order_id in the URL (for links from emails/admin, etc.)
    """
    order = None

    if order_id is not None:
        order = get_object_or_404(Order, pk=order_id)
        # remember it in the session so other flows can reuse it
        request.session["current_order_id"] = order.pk
    else:
        order = _get_current_order(request)
        if not order:
            messages.error(request, "Няма активна поръчка.")
            return redirect("checkout_info")

    # Get product from the items if present, else fall back to your helper
    item = order.items.first()
    if item:
        product = item.product
    else:
        product = get_single_product()

    return render(
        request,
        "checkout/summary_readonly.html",
        {
            "order": order,
            "product": product,
        },
    )


@require_POST
def checkout_confirm_cod(request):
    order = _get_current_order(request)
    if not order:
        messages.error(request, "Няма активна поръчка.")
        return redirect("checkout_info")

    if order.payment_method != PaymentMethod.COD:
        messages.error(request, "Тази поръчка не е с наложен платеж.")
        return redirect("checkout_summary")

    res = create_econt_label(order, overrides={})
    if not res.get("ok"):
        messages.error(request, f"Грешка при Еконт: {res.get('error') or 'Неуспешно създаване на товарителница.'}")
        return redirect("checkout_summary")

    try:
        send_order_notification(order, event="created")
    except Exception:
        pass

    return redirect("thank_you")

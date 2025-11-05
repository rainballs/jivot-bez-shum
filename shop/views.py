# shop/views.py
import json
import logging
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseRedirect
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from .utils import send_order_notification, maybe_send_order_email
from .econt_service import create_econt_label

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
    # 9.00 лв for "address", else 7.00 лв
    try:
        return Decimal("9.00") if order.delivery_method == DeliveryMethod.TO_ADDRESS else Decimal("7.00")
    except Exception:
        # fall back safely
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
        info_form = CheckoutInfoForm(request.POST)
        pay_form = PaymentMethodForm(request.POST)

        if info_form.is_valid() and pay_form.is_valid():
            # 1) create ONE order object
            order = info_form.save(commit=False)

            # delivery method from radio
            dm = request.POST.get("delivery_method", "address")
            from .models import DeliveryMethod, PaymentMethod, OrderItem  # or put these at top of file
            order.delivery_method = (
                DeliveryMethod.TO_ADDRESS if dm == "address" else DeliveryMethod.TO_OFFICE
            )

            # payment method
            order.payment_method = pay_form.cleaned_data["payment_method"]

            # mirror billing → shipping if requested
            if order.ship_same_as_billing:
                order.full_name = order.billing_full_name or order.full_name
                order.email = order.billing_email or order.email
                order.phone = order.billing_phone or order.phone
                order.city = order.billing_city or order.city
                order.postal_code = order.billing_postcode or order.postal_code
                order.address_line = order.billing_street or order.address_line

            qty = info_form.cleaned_data["quantity"]
            order.quantity = qty
            order.paid = False  # always false here
            order.save()

            # 2) create line item
            OrderItem.objects.create(
                order=order,
                product=product,
                quantity=qty,
                unit_price_bgn=product.price_bgn,
                unit_price_eur=product.price_eur,
            )

            # 3) totals
            order.recompute_totals()
            order.save(update_fields=[
                "subtotal_bgn", "subtotal_eur", "shipping_bgn", "shipping_eur",
                "total_bgn", "total_eur", "paid", "payment_method"
            ])

            # 4) remember order in session
            request.session["current_order_id"] = order.id

            # 5) branch by payment
            if order.payment_method in {PaymentMethod.CARD, PaymentMethod.APPLE_PAY, PaymentMethod.GOOGLE_PAY}:
                return redirect("stripe_create_session")

            # COD → go straight to econt
            if order.delivery_method == DeliveryMethod.TO_ADDRESS:
                return redirect("econt_collect_address")
            else:
                return redirect("econt_collect_office")

        # forms invalid
        messages.error(request, "Моля, коригирайте грешките във формата.")
    else:
        info_form = CheckoutInfoForm(initial={
            "quantity": 1,
            "ship_same_as_billing": True,
        })
        pay_form = PaymentMethodForm(initial={"payment_method": PaymentMethod.COD})

    return render(
        request,
        "checkout/info.html",
        {"product": product, "form": info_form, "pay_form": pay_form},
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
def stripe_create_checkout_session(request):
    order = _get_current_order(request)
    if not order:
        return redirect("checkout_info")
    product = get_single_product()
    if not product:
        messages.error(request, "Няма наличен продукт.")
        return redirect("home")

    # Decide the success page up front based on the chosen delivery method
    success_name = "econt_collect_address" if order.delivery_method == DeliveryMethod.TO_ADDRESS else "econt_collect_office"
    success_url = _site_url(request) + reverse(success_name) + "?session_id={CHECKOUT_SESSION_ID}"

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            line_items=stripe_checkout_line_items(order, product),
            metadata={
                "order_id": str(order.id),
                "delivery_method": str(order.delivery_method),
            },
            success_url=success_url,  # ⬅️ go to address/office page
            cancel_url=stripe_cancel_url(request),
            currency="bgn",
            customer_email=order.email or None,
        )
    except Exception as e:
        messages.error(request, f"Грешка при свързване със Stripe: {e}")
        # On error, send user to the *intended* delivery page too
        return redirect(success_name)

    request.session["stripe_session_id"] = session.id
    return HttpResponseRedirect(session.url)


@csrf_exempt
def stripe_webhook(request):
    payload = request.body
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE")
    secret = settings.STRIPE_WEBHOOK_SECRET

    logger.error("logging...")

    if not secret:
        return HttpResponseBadRequest("Missing STRIPE_WEBHOOK_SECRET")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
        )
        logger.error("✅ Stripe event received: %s", event["type"])
        logger.error("Full payload: %s", json.dumps(event, indent=4))
    except ValueError as e:
        logger.error("Invalid payload: %s", e)
        return HttpResponse(status=400)
    except stripe.error.SignatureVerificationError as e:
        logger.error("Signature verification failed: %s", e)
        return HttpResponse(status=400)

        # Process successful payment
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        logger.info("💰 Payment completed for session %s", session.get("id"))

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        order_id = (session.get("metadata") or {}).get("order_id")
        if order_id:
            try:
                order = Order.objects.get(pk=order_id)
                order.paid = True
                order.save(update_fields=["paid"])
                maybe_send_order_email(order)
                # Card paid → no COD
                _ = create_econt_label(order)
            except Order.DoesNotExist:
                pass

    return HttpResponse(status=200)


def thank_you(request):
    order = _get_current_order(request)
    if order:
        # Optionally clear the session so refresh doesn't reuse it
        request.session.pop("current_order_id", None)
    return render(request, "checkout/thank_you.html", {"order": order})

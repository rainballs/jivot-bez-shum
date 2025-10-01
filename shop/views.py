# shop/views.py
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseRedirect
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from .utils import send_order_notification

import stripe

from .forms import CheckoutInfoForm, PaymentMethodForm
from .models import Order, OrderItem, PaymentMethod, Product

# Configure Stripe once (safe even if keys are empty; we check before use)
stripe.api_key = settings.STRIPE_SECRET_KEY


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
    return _site_url(request) + reverse("checkout_payment")


def _to_minor_units(amount: Decimal) -> int:
    """BGN minor units (stotinki)."""
    return int((amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100))


def stripe_checkout_line_items(order: Order, product: Product):
    unit_cents = _to_minor_units(product.price_bgn)
    ship_cents = _to_minor_units(Decimal("5.00"))
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
        form = CheckoutInfoForm(request.POST)
        if form.is_valid():
            qty = form.cleaned_data["quantity"]
            order = form.save(commit=False)
            order.quantity = qty
            order.save()

            OrderItem.objects.create(
                order=order,
                product=product,
                quantity=qty,
                unit_price_bgn=product.price_bgn,
                unit_price_eur=product.price_eur,
            )
            order.recompute_totals()
            # Make it COD-only
            order.payment_method = PaymentMethod.COD
            order.paid = False
            order.save(update_fields=[
                "subtotal_bgn", "subtotal_eur", "shipping_bgn", "shipping_eur",
                "total_bgn", "total_eur", "payment_method", "paid"
            ])

            send_order_notification(order, event="created")

            request.session["current_order_id"] = order.id
            return redirect("thank_you")
        else:
            messages.error(request, "Моля, коригирайте грешките във формата.")
    else:
        form = CheckoutInfoForm(initial={"quantity": 1})

    return render(request, "checkout/info.html", {"product": product, "form": form})


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

            # Card / Apple Pay / Google Pay -> Stripe Checkout
            if order.payment_method in {PaymentMethod.CARD, PaymentMethod.APPLE_PAY, PaymentMethod.GOOGLE_PAY}:
                if not settings.STRIPE_PUBLIC_KEY or not settings.STRIPE_SECRET_KEY:
                    messages.error(request, "Stripe не е конфигуриран (липсват STRIPE_PUBLIC_KEY / STRIPE_SECRET_KEY).")
                    return redirect("checkout_payment")
                return redirect("stripe_create_session")

            # COD -> finish locally
            order.paid = False
            order.save(update_fields=["paid"])
            return redirect("thank_you")
        messages.error(request, "Моля, изберете метод на плащане.")
    else:
        # Preselect Card so there is always a value
        initial = {"payment_method": order.payment_method or PaymentMethod.CARD}
        form = PaymentMethodForm(instance=order, initial=initial)

    return render(request, "checkout/payment.html", {"product": product, "order": order, "form": form})


# ---------- Stripe integration ----------
def stripe_create_checkout_session(request):
    order = _get_current_order(request)
    if not order:
        return redirect("checkout_info")
    product = get_single_product()
    if not product:
        messages.error(request, "Няма наличен продукт.")
        return redirect("home")

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],  # Payment Request API enables Apple/Google Pay automatically
            line_items=stripe_checkout_line_items(order, product),
            metadata={"order_id": str(order.id)},
            success_url=stripe_success_url(request),
            cancel_url=stripe_cancel_url(request),
            currency="bgn",
            customer_email=order.email or None,
        )
    except Exception as e:
        messages.error(request, f"Грешка при свързване със Stripe: {e}")
        return redirect("checkout_payment")

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
        event = stripe.Webhook.construct_event(payload=payload, sig_header=sig_header, secret=secret)
    except Exception as e:
        return HttpResponseBadRequest(str(e))

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        order_id = (session.get("metadata") or {}).get("order_id")
        if order_id:
            try:
                order = Order.objects.get(pk=order_id)
                order.paid = True
                order.save(update_fields=["paid"])

                from .utils import send_order_notification
                send_order_notification(order, event="paid")
            except Order.DoesNotExist:
                pass

    return HttpResponse(status=200)


def thank_you(request):
    order = _get_current_order(request)
    if order:
        # Optionally clear the session so refresh doesn't reuse it
        request.session.pop("current_order_id", None)
    return render(request, "checkout/thank_you.html", {"order": order})

# shop/views.py
import logging
import re
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.http import (
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseRedirect,
    JsonResponse,
)
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_GET

import stripe

from .forms import CheckoutInfoForm, PaymentMethodForm
from .models import Order, OrderItem, PaymentMethod, Product, DeliveryMethod
from .utils import send_order_notification
from .econt_service import create_econt_label

logger = logging.getLogger("gunicorn.error")

# Stripe config
stripe.api_key = settings.STRIPE_SECRET_LIVE_KEY

# Currency constants (Stripe no longer supports BGN for Bulgaria)
STRIPE_CURRENCY = "eur"
BGN_PER_EUR = Decimal("1.95583")


# ---------- Helpers ----------
def get_single_product():
    qs = Product.objects.filter(is_active=True).order_by("id")
    return qs.first() or Product.objects.first()


def _site_url(request):
    scheme = "https" if request.is_secure() else "http"
    return f"{scheme}://{request.get_host()}"


def stripe_cancel_url(request):
    return _site_url(request) + reverse("checkout_info")


def _to_minor_units(amount: Decimal) -> int:
    """Convert Decimal to cents (minor units) for EUR."""
    return int((amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100))


def _bgn_to_eur(bgn: Decimal) -> Decimal:
    return (bgn / BGN_PER_EUR).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _safe_product_price_eur(product: Product) -> Decimal:
    """
    Prefer product.price_eur.
    If missing, fallback convert from product.price_bgn.
    """
    p = getattr(product, "price_eur", None)
    if p is not None and Decimal(p) > 0:
        return Decimal(p)

    bgn = getattr(product, "price_bgn", None)
    if bgn is None:
        raise ValueError("Product has no price_eur and no price_bgn.")
    return _bgn_to_eur(Decimal(bgn))


def _ship_eur_for(order: Order) -> Decimal:
    """
    Prefer order.shipping_eur (computed by recompute_totals()).
    Fallback: convert old 9/7 BGN to EUR.
    """
    ship_eur = getattr(order, "shipping_eur", None)
    if ship_eur is not None:
        ship_eur = Decimal(ship_eur)
        if ship_eur > 0:
            return ship_eur

    # fallback if shipping_eur not computed yet
    bgn = Decimal("9.00") if order.delivery_method == DeliveryMethod.TO_ADDRESS else Decimal("7.00")
    return _bgn_to_eur(bgn)


def stripe_checkout_line_items(order: Order, product: Product):
    """
    ALWAYS returns EUR line items.
    Includes product + shipping as separate line items.
    """
    unit_eur = _safe_product_price_eur(product)
    ship_eur = _ship_eur_for(order)

    unit_cents = _to_minor_units(unit_eur)
    ship_cents = _to_minor_units(ship_eur)

    # Stripe expects positive integers for unit_amount
    if unit_cents < 1:
        raise ValueError(f"Product unit_amount is too small: {unit_eur} EUR")
    if ship_cents < 0:
        raise ValueError(f"Shipping is negative: {ship_eur} EUR")

    items = [
        {
            "price_data": {
                "currency": STRIPE_CURRENCY,
                "product_data": {"name": product.name},
                "unit_amount": unit_cents,
            },
            "quantity": int(order.quantity or 1),
        },
        {
            "price_data": {
                "currency": STRIPE_CURRENCY,
                "product_data": {"name": "Доставка"},
                "unit_amount": ship_cents,
            },
            "quantity": 1,
        },
    ]

    # Bulletproof guard: never allow BGN to reach Stripe
    for it in items:
        cur = (it.get("price_data") or {}).get("currency")
        if cur != STRIPE_CURRENCY:
            raise ValueError(f"Non-EUR currency detected in line_items: {cur}")

    return items


def _get_current_order(request) -> Order | None:
    """
    Single source of truth for current order:
    - session current_order_id, else
    - order_id from GET/POST (optional)
    """
    oid = request.session.get("current_order_id")
    if not oid:
        oid = request.GET.get("order_id") or request.POST.get("order_id")
    return Order.objects.filter(pk=oid).first() if oid else None


def _split_street_num(line: str) -> tuple[str, str]:
    if not line:
        return "", ""
    s = line.strip()

    m = re.search(r"(?:№\s*)(\d+[A-Za-zА-Яа-я\-\/]*)\s*$", s)
    if not m:
        m = re.search(r"\s(\d+[A-Za-zА-Яа-я\-\/]*)\s*$", s)

    if m:
        num = m.group(1)
        street = s[: m.start(1)].rstrip(" ,№")
        return street.strip(), num.strip()

    return s, ""


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

            # hard validation for Econt before saving the order
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
                messages.error(request, "За да продължите, попълнете: " + ", ".join(missing_parts) + ".")
                return render(
                    request,
                    "checkout/info.html",
                    {"product": product, "form": info_form, "pay_form": pay_form},
                )

            order.save()

            # line item
            OrderItem.objects.create(
                order=order,
                product=product,
                quantity=order.quantity,
                unit_price_bgn=getattr(product, "price_bgn", None),
                unit_price_eur=_safe_product_price_eur(product),
            )

            # totals
            order.recompute_totals()
            order.save(
                update_fields=[
                    "subtotal_bgn",
                    "subtotal_eur",
                    "shipping_bgn",
                    "shipping_eur",
                    "total_bgn",
                    "total_eur",
                    "paid",
                    "payment_method",
                ]
            )

            request.session["current_order_id"] = order.id

            return render(
                request,
                "checkout/info.html",
                {"product": product, "form": info_form, "pay_form": pay_form, "order": order},
            )

        messages.error(request, "Моля, коригирайте грешките във формата.")
        return render(request, "checkout/info.html", {"product": product, "form": info_form, "pay_form": pay_form})

    # GET branch: create order if missing
    order = _get_current_order(request)
    if not order:
        order = Order.objects.create(
            quantity=1,
            delivery_method=DeliveryMethod.TO_ADDRESS,
            payment_method=PaymentMethod.COD,
            paid=False,
        )
        OrderItem.objects.create(
            order=order,
            product=product,
            quantity=1,
            unit_price_bgn=getattr(product, "price_bgn", None),
            unit_price_eur=_safe_product_price_eur(product),
        )
        order.recompute_totals()
        order.save()
        request.session["current_order_id"] = order.id

    info_form = CheckoutInfoForm(instance=order)
    pay_form = PaymentMethodForm(initial={"payment_method": order.payment_method})

    return render(
        request,
        "checkout/info.html",
        {"product": product, "form": info_form, "pay_form": pay_form, "order": order},
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

            if order.payment_method in {PaymentMethod.CARD, PaymentMethod.APPLE_PAY, PaymentMethod.GOOGLE_PAY}:
                if not settings.STRIPE_PUBLIC_LIVE_KEY or not settings.STRIPE_SECRET_LIVE_KEY:
                    messages.error(request, "Stripe не е конфигуриран (липсват STRIPE_PUBLIC_KEY / STRIPE_SECRET_KEY).")
                    return redirect("checkout_payment")
                return redirect("stripe_create_session")

            # COD flow
            order.paid = False
            order.save(update_fields=["paid"])
            request.session.pop("stripe_session_id", None)

            if order.delivery_method == DeliveryMethod.TO_ADDRESS:
                return redirect("econt_collect_address")
            return redirect("econt_collect_office")

        messages.error(request, "Моля, изберете метод на плащане.")
    else:
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

    # Always go to thank_you after Stripe succeeds
    success_url = _site_url(request) + reverse("thank_you") + "?session_id={CHECKOUT_SESSION_ID}"

    try:
        line_items = stripe_checkout_line_items(order, product)
        logger.error("Stripe line_items (EUR): %s", line_items)  # helpful while debugging

        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            line_items=line_items,
            metadata={
                "order_id": str(order.id),
                "delivery_method": str(order.delivery_method),
            },
            success_url=success_url,
            cancel_url=stripe_cancel_url(request),
            customer_email=order.email or None,
        )
    except Exception as e:
        messages.error(request, f"Грешка при свързване със Stripe: {e}")
        return redirect("checkout_info")

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

            # optional: create econt label only if delivery data is present
            if order.city and (order.econt_office_code or order.address_line):
                try:
                    create_econt_label(order)
                except Exception:
                    pass

    return HttpResponse(status=200)


def thank_you(request):
    session_id = request.GET.get("session_id")
    order = _get_current_order(request)

    if session_id and settings.STRIPE_SECRET_LIVE_KEY:
        try:
            sess = stripe.checkout.Session.retrieve(
                session_id,
                api_key=settings.STRIPE_SECRET_LIVE_KEY,
                expand=["payment_intent"],
            )
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
                    order = paid_order
                    request.session["current_order_id"] = paid_order.pk
        except Exception as e:
            logger.error("Stripe verify on thank_you failed: %s", e)

        # Create Econt label after Stripe (card flow) if we can
        if order:
            overrides = {}

            if order.delivery_method == DeliveryMethod.TO_OFFICE:
                if order.econt_office_code:
                    overrides["receiver_office_code"] = order.econt_office_code
            else:
                street_line = (order.address_line or "") or (getattr(order, "billing_street", "") or "")
                street, num = _split_street_num(street_line)
                overrides["receiver_street"] = street
                if num:
                    overrides["receiver_num"] = num

                postcode = (
                    getattr(order, "postal_code", "")
                    or getattr(order, "billing_postcode", "")
                    or getattr(order, "billing_postal_code", "")
                )
                if postcode:
                    overrides["receiver_postcode"] = postcode

            try:
                create_econt_label(order, overrides=overrides)
            except Exception as e:
                logger.error("Econt label after Stripe failed for order %s: %s", order.pk, e)

    # clear session so refresh doesn’t reuse the same order
    request.session.pop("current_order_id", None)
    return render(request, "checkout/thank_you.html", {"order": order})


# ---------- AJAX helpers ----------
@require_POST
def checkout_inline_update(request):
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
        order.delivery_method = DeliveryMethod.TO_ADDRESS if dm == "address" else DeliveryMethod.TO_OFFICE
        changed = True

    if qty:
        try:
            q = int(qty)
            if q > 0:
                order.quantity = q
                order.recompute_totals()
                changed = True
        except ValueError:
            pass

    if changed:
        order.save()

    return JsonResponse({"ok": True})


@require_POST
def checkout_save_inline(request):
    order = _get_current_order(request)
    if not order:
        order = Order.objects.create()
        request.session["current_order_id"] = order.pk

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

    same = request.POST.get("ship_same_as_billing")
    if same is not None:
        order.ship_same_as_billing = (same == "true")

    dm = request.POST.get("delivery_method")
    if dm == "address":
        order.delivery_method = DeliveryMethod.TO_ADDRESS
    elif dm == "office":
        order.delivery_method = DeliveryMethod.TO_OFFICE

    pm = request.POST.get("payment_method")
    if pm:
        if pm == "card":
            order.payment_method = PaymentMethod.CARD
        else:
            order.payment_method = PaymentMethod.COD

    order.save()
    return JsonResponse({"ok": True})


@require_GET
def checkout_summary(request, order_id=None):
    if order_id is not None:
        order = get_object_or_404(Order, pk=order_id)
        request.session["current_order_id"] = order.pk
    else:
        order = _get_current_order(request)
        if not order:
            messages.error(request, "Няма активна поръчка.")
            return redirect("checkout_info")

    item = order.items.first()
    product = item.product if item else get_single_product()

    return render(request, "checkout/summary_readonly.html", {"order": order, "product": product})


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

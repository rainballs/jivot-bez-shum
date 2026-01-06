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
from .econt_service import create_econt_label, calculate_econt_shipping  # <-- ADD THIS

logger = logging.getLogger("gunicorn.error")

stripe.api_key = settings.STRIPE_SECRET_LIVE_KEY

# Stripe no longer supports BGN for Bulgaria -> use EUR only
STRIPE_CURRENCY = "eur"
BGN_PER_EUR = Decimal("1.95583")


# -------------------- money helpers --------------------
def _to_minor_units(amount: Decimal) -> int:
    """EUR cents."""
    return int((amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100))


def _eur_to_bgn(eur: Decimal) -> Decimal:
    return (eur * BGN_PER_EUR).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _bgn_to_eur(bgn: Decimal) -> Decimal:
    return (bgn / BGN_PER_EUR).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _as_decimal(x, default=Decimal("0.00")) -> Decimal:
    try:
        if x is None:
            return default
        return Decimal(str(x))
    except Exception:
        return default


# -------------------- product helpers --------------------
def get_single_product():
    qs = Product.objects.filter(is_active=True).order_by("id")
    return qs.first() or Product.objects.first()


def _safe_product_price_eur(product: Product) -> Decimal:
    p = _as_decimal(getattr(product, "price_eur", None))
    if p > 0:
        return p

    bgn = _as_decimal(getattr(product, "price_bgn", None))
    if bgn > 0:
        return _bgn_to_eur(bgn)

    raise ValueError("Product has no valid price_eur/price_bgn")


# -------------------- urls --------------------
def _site_url(request):
    scheme = "https" if request.is_secure() else "http"
    return f"{scheme}://{request.get_host()}"


def stripe_cancel_url(request):
    return _site_url(request) + reverse("checkout_info")


# -------------------- order helpers --------------------
def _get_current_order(request) -> Order | None:
    oid = request.session.get("current_order_id")
    if not oid:
        oid = request.GET.get("order_id") or request.POST.get("order_id")
    return Order.objects.filter(pk=oid).first() if oid else None


def _ensure_order(request) -> Order:
    order = _get_current_order(request)
    if order:
        return order
    order = Order.objects.create(quantity=1, paid=False)
    request.session["current_order_id"] = order.pk
    return order


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


def _delivery_ready_for_pricing(order: Order) -> bool:
    """Minimal check: enough data to ask Econt for a real price."""
    if order.delivery_method == DeliveryMethod.TO_OFFICE:
        return bool((order.city or "").strip()) and bool((getattr(order, "econt_office_code", "") or "").strip())
    # TO_ADDRESS
    return (
            bool((order.city or "").strip())
            and bool((order.postal_code or "").strip())
            and bool((order.address_line or "").strip())
    )


def _apply_econt_shipping(order: Order, amount: Decimal, currency: str):
    """
    IMPORTANT:
    - If Econt returns EUR -> store as shipping_eur directly (NO DIVIDE BY 1.95583)
    - If Econt returns BGN -> convert once to EUR and store
    """
    currency = (currency or "").upper().strip()
    amount = _as_decimal(amount)

    if currency == "EUR":
        ship_eur = amount
    elif currency == "BGN":
        ship_eur = _bgn_to_eur(amount)
    else:
        # unknown currency -> assume EUR (safer now)
        ship_eur = amount

    if ship_eur < 0:
        ship_eur = Decimal("0.00")

    order.shipping_eur = ship_eur
    order.shipping_bgn = _eur_to_bgn(ship_eur)


def _refresh_shipping_from_econt(order: Order) -> dict:
    """
    Calls Econt calculate API and saves shipping in BOTH currencies.
    Returns dict {ok, shipping_eur, shipping_bgn, error?}
    """
    if not _delivery_ready_for_pricing(order):
        # Not enough data yet, don't call Econt
        order.shipping_eur = Decimal("0.00")
        order.shipping_bgn = Decimal("0.00")
        order.save(update_fields=["shipping_eur", "shipping_bgn"])
        return {"ok": False, "error": "missing_delivery_data", "shipping_eur": "0.00", "shipping_bgn": "0.00"}

    include_cod = (order.payment_method == PaymentMethod.COD)

    res = calculate_econt_shipping(order, include_cod=include_cod)  # <-- your econt_service must implement this
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or "econt_failed"}

    amount = _as_decimal(res.get("amount"))
    currency = res.get("currency") or "EUR"

    _apply_econt_shipping(order, amount, currency)
    order.save(update_fields=["shipping_eur", "shipping_bgn"])

    return {
        "ok": True,
        "shipping_eur": f"{_as_decimal(order.shipping_eur):.2f}",
        "shipping_bgn": f"{_as_decimal(order.shipping_bgn):.2f}",
    }


def _recompute_totals_from_db(order: Order, product: Product):
    """
    Your Order.recompute_totals() can exist, but THIS version is bulletproof:
    - subtotal based on product EUR
    - shipping from order.shipping_eur (already from Econt)
    - totals computed from those
    """
    qty = int(order.quantity or 1)
    unit_eur = _safe_product_price_eur(product)

    subtotal_eur = (unit_eur * qty).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    shipping_eur = _as_decimal(getattr(order, "shipping_eur", None))
    total_eur = (subtotal_eur + shipping_eur).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    order.subtotal_eur = subtotal_eur
    order.total_eur = total_eur

    order.subtotal_bgn = _eur_to_bgn(subtotal_eur)
    order.total_bgn = _eur_to_bgn(total_eur)

    # NOTE: shipping_bgn is already set when we refresh shipping
    if getattr(order, "shipping_bgn", None) is None:
        order.shipping_bgn = _eur_to_bgn(shipping_eur)


# -------------------- pages --------------------
def home(request):
    product = get_single_product()
    return render(request, "pages/home.html", {"product": product})


@transaction.atomic
def checkout_info(request):
    product = get_single_product()
    if not product:
        messages.error(request, "Няма наличен продукт.")
        return redirect("home")

    # Ensure order exists
    order = _get_current_order(request)
    if not order and request.method == "GET":
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
        request.session["current_order_id"] = order.pk

    if request.method == "POST":
        info_form = CheckoutInfoForm(request.POST, instance=order)
        pay_form = PaymentMethodForm(request.POST, instance=order)

        if info_form.is_valid() and pay_form.is_valid():
            order = info_form.save(commit=False)

            dm = request.POST.get("delivery_method", "address")
            order.delivery_method = (
                DeliveryMethod.TO_ADDRESS if dm == "address" else DeliveryMethod.TO_OFFICE
            )

            order.payment_method = pay_form.cleaned_data["payment_method"]
            order.quantity = info_form.cleaned_data["quantity"]
            order.paid = False

            if order.ship_same_as_billing:
                order.full_name = order.billing_full_name or order.full_name
                order.email = order.billing_email or order.email
                order.phone = order.billing_phone or order.phone
                order.city = order.billing_city or order.city
                order.postal_code = order.billing_postcode or order.postal_code
                order.address_line = order.billing_street or order.address_line

            order.save()

            # Ensure we have an item
            if not order.items.exists():
                OrderItem.objects.create(
                    order=order,
                    product=product,
                    quantity=order.quantity,
                    unit_price_bgn=getattr(product, "price_bgn", None),
                    unit_price_eur=_safe_product_price_eur(product),
                )

            # Refresh shipping if possible (won't call Econt if missing data)
            _refresh_shipping_from_econt(order)

            # Recompute totals from the current DB fields
            _recompute_totals_from_db(order, product)
            order.save(update_fields=[
                "subtotal_bgn", "subtotal_eur",
                "shipping_bgn", "shipping_eur",
                "total_bgn", "total_eur",
                "paid", "payment_method",
                "delivery_method", "quantity",
            ])

            request.session["current_order_id"] = order.pk

            return render(request, "checkout/info.html", {
                "product": product,
                "form": CheckoutInfoForm(instance=order),
                "pay_form": PaymentMethodForm(instance=order, initial={"payment_method": order.payment_method}),
                "order": order,
            })

        messages.error(request, "Моля, коригирайте грешките във формата.")
    # GET render
    info_form = CheckoutInfoForm(instance=order)
    pay_form = PaymentMethodForm(instance=order, initial={"payment_method": order.payment_method})

    return render(request, "checkout/info.html", {
        "product": product,
        "form": info_form,
        "pay_form": pay_form,
        "order": order,
    })


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
                    messages.error(request, "Stripe не е конфигуриран.")
                    return redirect("checkout_payment")
                return redirect("stripe_create_session")

            # COD
            order.paid = False
            order.save(update_fields=["paid"])
            request.session.pop("stripe_session_id", None)

            if order.delivery_method == DeliveryMethod.TO_ADDRESS:
                return redirect("econt_collect_address")
            return redirect("econt_collect_office")

        messages.error(request, "Моля, изберете метод на плащане.")
    else:
        form = PaymentMethodForm(instance=order, initial={"payment_method": order.payment_method or PaymentMethod.CARD})

    return render(request, "checkout/payment.html", {"product": product, "order": order, "form": form})


# -------------------- Stripe --------------------
def stripe_checkout_line_items(order: Order, product: Product):
    unit_eur = _safe_product_price_eur(product)
    ship_eur = _as_decimal(getattr(order, "shipping_eur", None))

    unit_cents = _to_minor_units(unit_eur)
    ship_cents = _to_minor_units(ship_eur)

    if unit_cents < 1:
        raise ValueError(f"Product unit amount too small: {unit_eur}")
    if ship_cents < 0:
        raise ValueError(f"Shipping negative: {ship_eur}")

    return [
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


def stripe_create_checkout_session(request):
    order = _get_current_order(request)
    if not order:
        return redirect("checkout_info")

    product = get_single_product()
    if not product:
        messages.error(request, "Няма наличен продукт.")
        return redirect("home")

    # IMPORTANT: refresh shipping right before payment (live Econt price)
    ship_res = _refresh_shipping_from_econt(order)
    if not ship_res.get("ok"):
        messages.error(request, "Не успяхме да изчислим доставка. Моля, проверете данните за доставка.")
        return redirect("checkout_info")

    # totals (optional but nice)
    _recompute_totals_from_db(order, product)
    order.save(update_fields=["subtotal_eur", "subtotal_bgn", "total_eur", "total_bgn", "shipping_eur", "shipping_bgn"])

    success_url = _site_url(request) + reverse("thank_you") + "?session_id={CHECKOUT_SESSION_ID}"

    try:
        line_items = stripe_checkout_line_items(order, product)

        # hard guard
        for it in line_items:
            cur = (it.get("price_data") or {}).get("currency")
            if cur != "eur":
                raise ValueError(f"Non-EUR currency in Stripe line_items: {cur}")

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

            if order.city and (getattr(order, "econt_office_code", "") or order.address_line):
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

        # Create Econt label after Stripe (if possible)
        if order:
            overrides = {}
            if order.delivery_method == DeliveryMethod.TO_OFFICE:
                if getattr(order, "econt_office_code", ""):
                    overrides["receiver_office_code"] = order.econt_office_code
            else:
                street_line = (order.address_line or "") or (getattr(order, "billing_street", "") or "")
                street, num = _split_street_num(street_line)
                overrides["receiver_street"] = street
                if num:
                    overrides["receiver_num"] = num
                postcode = getattr(order, "postal_code", "") or getattr(order, "billing_postcode", "")
                if postcode:
                    overrides["receiver_postcode"] = postcode

            try:
                create_econt_label(order, overrides=overrides)
            except Exception as e:
                logger.error("Econt label after Stripe failed for order %s: %s", order.pk, e)

    request.session.pop("current_order_id", None)
    return render(request, "checkout/thank_you.html", {"order": order})


# -------------------- AJAX: live totals for info.html --------------------
@require_POST
def checkout_preview_totals(request):
    """
    Called by your info.html JS:
    - updates order quantity / payment / delivery fields (lightweight)
    - if delivery data is enough -> calls Econt calculate and stores shipping_eur/bgn
    - returns shipping + totals (both EUR/BGN)
    """
    product = get_single_product()
    if not product:
        return JsonResponse({"ok": False, "error": "no_product"}, status=400)

    order = _ensure_order(request)

    # quantity
    qty = request.POST.get("quantity")
    if qty:
        try:
            q = int(qty)
            if q > 0:
                order.quantity = q
        except ValueError:
            pass

    # payment
    pm = request.POST.get("payment_method")
    if pm:
        if pm == "card":
            order.payment_method = PaymentMethod.CARD
        elif pm == "cod":
            order.payment_method = PaymentMethod.COD

    # delivery method
    dm = request.POST.get("delivery_method")
    if dm == "address":
        order.delivery_method = DeliveryMethod.TO_ADDRESS
    elif dm == "office":
        order.delivery_method = DeliveryMethod.TO_OFFICE

    # delivery data (from inline form)
    # NOTE: keep your own field names consistent with your model
    city = request.POST.get("city")
    if city is not None:
        order.city = city

    # address fields stored in order.address_line + postal_code
    receiver_street = request.POST.get("receiver_street", "").strip()
    receiver_num = request.POST.get("receiver_num", "").strip()
    receiver_postcode = request.POST.get("receiver_postcode", "").strip()
    if receiver_postcode:
        order.postal_code = receiver_postcode
    if receiver_street or receiver_num:
        # store as a single line (your model uses address_line)
        line = receiver_street
        if receiver_num:
            line = (line + " " + receiver_num).strip()
        order.address_line = line

    # office code
    office_code = request.POST.get("econt_office_code") or request.POST.get("office_code")
    if office_code is not None:
        setattr(order, "econt_office_code", office_code)

    order.save()

    # Try refresh shipping (only if enough data)
    ship = _refresh_shipping_from_econt(order)

    # totals
    _recompute_totals_from_db(order, product)
    order.save(update_fields=["subtotal_eur", "subtotal_bgn", "total_eur", "total_bgn", "shipping_eur", "shipping_bgn"])

    return JsonResponse({
        "ok": True,
        "shipping_known": bool(ship.get("ok")),
        "shipping_eur": f"{_as_decimal(order.shipping_eur):.2f}",
        "shipping_bgn": f"{_as_decimal(order.shipping_bgn):.2f}",
        "total_eur": f"{_as_decimal(order.total_eur):.2f}",
        "total_bgn": f"{_as_decimal(order.total_bgn):.2f}",
        "subtotal_eur": f"{_as_decimal(order.subtotal_eur):.2f}",
        "subtotal_bgn": f"{_as_decimal(order.subtotal_bgn):.2f}",
    })

# shop/views.py
import logging
import re
from decimal import Decimal

import stripe
from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST, require_GET

from . import fulfillment, payments
from .address import split_street_num
from .checkout import (
    BILLING_LIMITS,
    SESSION_KEY,
    apply_selection,
    ensure_editable,
    get_session_order,
    readiness_error,
    throttled,
)
from .forms import CheckoutInfoForm, PaymentMethodForm
from .models import (
    DeliveryMethod,
    Order,
    OrderItem,
    PaymentMethod,
    PaymentStatus,
    Product,
    ShipmentStatus,
    StripeEvent,
    _bgn_to_eur,
)
from .utils import notify_order_accepted

logger = logging.getLogger("shop")

SESSION_ID_RE = re.compile(r"^cs_(test|live)_[A-Za-z0-9]{10,200}$")


# ---------- Helpers ----------
def get_single_product():
    qs = Product.objects.filter(is_active=True).order_by("id")
    return qs.first() or Product.objects.first()


def _safe_product_price_eur(product: Product) -> Decimal:
    """Prefer product.price_eur; fall back to converting price_bgn."""
    p = getattr(product, "price_eur", None)
    if p is not None and Decimal(p) > 0:
        return Decimal(p)
    bgn = getattr(product, "price_bgn", None)
    if bgn is None:
        raise ValueError("Product has no price_eur and no price_bgn.")
    return _bgn_to_eur(Decimal(bgn))


def _new_order(request, product, *, quantity=1, payment_method=PaymentMethod.COD) -> Order | None:
    """Create the session's order (rate limited: every anonymous visitor otherwise creates DB rows)."""
    if throttled(request, "new_order", 30, 3600):
        return None
    with transaction.atomic():
        order = Order.objects.create(
            quantity=quantity,
            delivery_method=DeliveryMethod.TO_ADDRESS,
            payment_method=payment_method,
        )
        OrderItem.objects.create(
            order=order,
            product=product,
            quantity=quantity,
            unit_price_bgn=product.price_bgn,
            unit_price_eur=_safe_product_price_eur(product),
        )
        order.recompute_totals()
        order.save()
        order.log_event("created", "checkout started")
    request.session[SESSION_KEY] = order.id
    return order


def _order_is_final(order: Order) -> bool:
    return (
            order.payment_status == PaymentStatus.PAID
            or order.cod_confirmed_at is not None
            or order.shipment_status != ShipmentStatus.NONE
    )


# ---------- Pages ----------
def home(request):
    product = get_single_product()
    return render(request, "pages/home.html", {"product": product})


@require_http_methods(["GET", "POST"])
def checkout_info(request):
    product = get_single_product()
    if not product:
        messages.error(request, "Няма наличен продукт.")
        return redirect("home")

    order = get_session_order(request)
    if order is not None and not ensure_editable(order):
        if _order_is_final(order):
            request.session.pop(SESSION_KEY, None)  # finished order: start a fresh checkout
            order = None
        else:  # a card payment is still being processed
            return redirect("thank_you")

    if request.method == "POST":
        info_form = CheckoutInfoForm(request.POST, instance=order)
        pay_form = PaymentMethodForm(request.POST)

        if order is None and throttled(request, "new_order", 30, 3600):
            return HttpResponse("Твърде много заявки. Опитайте по-късно.", status=429)

        if info_form.is_valid() and pay_form.is_valid():
            with transaction.atomic():
                creating = order is None
                order = info_form.save(commit=False)
                dm = request.POST.get("delivery_method", "address")
                order.delivery_method = DeliveryMethod.TO_ADDRESS if dm == "address" else DeliveryMethod.TO_OFFICE
                order.payment_method = pay_form.cleaned_data["payment_method"]

                if order.ship_same_as_billing:
                    order.full_name = order.billing_full_name or order.full_name
                    order.email = order.billing_email or order.email
                    order.phone = order.billing_phone or order.phone
                    order.city = order.billing_city or order.city
                    order.postal_code = order.billing_postcode or order.postal_code
                    order.address_line = order.billing_street or order.address_line

                qty = info_form.cleaned_data["quantity"]
                missing_parts = []
                if not (order.full_name or "").strip():
                    missing_parts.append("име и фамилия")
                if not (order.phone or "").strip():
                    missing_parts.append("телефон")
                if not (order.city or "").strip():
                    missing_parts.append("град")
                if order.delivery_method == DeliveryMethod.TO_ADDRESS:
                    if not (order.postal_code or "").strip():
                        missing_parts.append("пощенски код")
                    if not (order.address_line or "").strip():
                        missing_parts.append("улица и номер")
                elif not (order.econt_office_code or "").strip():
                    missing_parts.append("офис на Еконт")

                if missing_parts:
                    messages.error(request, "За да продължите, попълнете: " + ", ".join(missing_parts) + ".")
                    return render(request, "checkout/info.html",
                                  {"product": product, "form": info_form, "pay_form": pay_form})

                order.invalidate_quote()
                if order.address_line and not order.receiver_street:
                    order.receiver_street, order.receiver_num = split_street_num(order.address_line)
                order.save()
                if creating or not order.items.exists():
                    OrderItem.objects.create(
                        order=order, product=product, quantity=qty,
                        unit_price_bgn=product.price_bgn, unit_price_eur=_safe_product_price_eur(product),
                    )
                order.set_quantity(qty)
                order.recompute_totals()
                order.save(update_fields=[
                    "subtotal_bgn", "subtotal_eur", "shipping_bgn", "shipping_eur", "total_bgn", "total_eur",
                ])
            request.session[SESSION_KEY] = order.id
            return render(request, "checkout/info.html",
                          {"product": product, "form": info_form, "pay_form": pay_form, "order": order})

        messages.error(request, "Моля, коригирайте грешките във формата.")
        return render(request, "checkout/info.html", {"product": product, "form": info_form, "pay_form": pay_form})

    # GET: create the order lazily (rate limited)
    if order is None:
        order = _new_order(request, product)
        if order is None:
            return HttpResponse("Твърде много заявки. Опитайте по-късно.", status=429)

    info_form = CheckoutInfoForm(instance=order)
    pay_form = PaymentMethodForm(initial={"payment_method": order.payment_method})
    return render(request, "checkout/info.html",
                  {"product": product, "form": info_form, "pay_form": pay_form, "order": order})


# ---------- Stripe integration ----------
@require_http_methods(["GET", "POST"])
def stripe_create_checkout_session(request):
    if request.method == "GET":  # old links/bookmarks: creating a payment session must be a CSRF-protected POST
        return redirect("checkout_summary")

    order = get_session_order(request)
    if not order:
        return redirect("checkout_info")

    if not settings.STRIPE_SECRET_LIVE_KEY:
        messages.error(request, "Плащането с карта временно не е налично.")
        return redirect("checkout_summary")

    if not order.is_card:
        messages.error(request, "Тази поръчка не е с плащане с карта.")
        return redirect("checkout_summary")

    err = readiness_error(order)
    if err:
        messages.error(request, err)
        return redirect("checkout_info")

    try:
        attempt = payments.create_checkout_session(order.pk, settings.SITE_URL.rstrip("/"))
    except payments.PaymentNotAllowed as e:
        logger.warning("payment not allowed order=%s: %s", order.pk, e)
        messages.error(request, "Поръчката не може да бъде платена в момента. Моля, проверете данните си.")
        return redirect("checkout_info")
    except stripe.StripeError:
        logger.exception("Stripe session creation failed for order %s", order.pk)
        messages.error(request, "Временна грешка при свързване със Stripe. Моля, опитайте отново.")
        return redirect("checkout_summary")

    request.session["stripe_session_id"] = attempt.stripe_session_id
    return HttpResponseRedirect(attempt.checkout_url)


@csrf_exempt  # Stripe cannot send a CSRF token; authenticity is established by the signature below
@require_POST
def stripe_webhook(request):
    payload = request.body  # raw bytes: the signature is computed over exactly this
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE", "")
    secret = settings.STRIPE_WEBHOOK_SECRET

    if not secret:
        logger.error("STRIPE_WEBHOOK_SECRET is not configured; rejecting webhook")
        return HttpResponse("webhook not configured", status=500)

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, secret)
    except (ValueError, stripe.SignatureVerificationError):
        return HttpResponse(status=400)

    ev, created = StripeEvent.objects.get_or_create(
        event_id=event["id"],
        defaults={"event_type": event["type"], "livemode": bool(event.get("livemode"))},
    )
    if not created and ev.status in (StripeEvent.Status.PROCESSED, StripeEvent.Status.IGNORED,
                                     StripeEvent.Status.REJECTED):
        return HttpResponse(status=200)  # duplicate delivery: already handled

    try:
        status, order, detail = payments.handle_event(event)
    except Exception as e:
        logger.exception("stripe event %s failed", event["id"])
        StripeEvent.objects.filter(pk=ev.pk).update(status=StripeEvent.Status.ERROR, detail=type(e).__name__)
        return HttpResponse(status=500)  # Stripe retries with backoff; the event row proves we saw it

    StripeEvent.objects.filter(pk=ev.pk).update(
        status=status, order=order, detail=str(detail)[:500], processed_at=timezone.now(),
    )
    if status == StripeEvent.Status.REJECTED:
        logger.error("stripe event %s rejected: %s", event["id"], detail)
    return HttpResponse(status=200)


def thank_you(request):
    order = get_session_order(request)
    session_id = request.GET.get("session_id", "")
    show_details = order is not None

    if session_id and SESSION_ID_RE.match(session_id) and settings.STRIPE_SECRET_LIVE_KEY \
            and not throttled(request, "thank_you", 20, 60):
        try:
            sess = stripe.checkout.Session.retrieve(session_id, api_key=settings.STRIPE_SECRET_LIVE_KEY)
        except Exception:
            logger.exception("Stripe verify on thank_you failed")
        else:
            # Same verified, idempotent function the webhook uses. Never creates a shipment inline.
            result, paid_order, _ = payments.record_session_payment(sess, source="return")
            if result in ("applied", "already_applied", "duplicate_payment") and paid_order is not None:
                if order is None:
                    order = paid_order  # opened in another browser: show minimal info only
                    show_details = False
                elif order.pk != paid_order.pk:
                    order, show_details = paid_order, False
                else:
                    order = paid_order

    if order is not None:
        order.refresh_from_db()
        finished = order.payment_status == PaymentStatus.PAID or order.cod_confirmed_at
        if finished and request.session.get(SESSION_KEY) == order.pk:
            request.session.pop(SESSION_KEY, None)  # finished: refresh must not reuse the order
    return render(request, "checkout/thank_you.html", {"order": order, "show_details": show_details})


# ---------- AJAX helpers ----------
@require_POST
def checkout_save_inline(request):
    order = get_session_order(request)
    if order is None:
        product = get_single_product()
        order = _new_order(request, product) if product else None
        if order is None:
            return JsonResponse({"ok": False, "error": "Твърде много заявки."}, status=429)

    if not ensure_editable(order):
        return JsonResponse({"ok": False, "error": "locked"}, status=409)

    error, touched = apply_selection(order, request.POST)
    if error:
        return JsonResponse({"ok": False, "error": error}, status=400)

    for field, maxlen in BILLING_LIMITS.items():
        val = request.POST.get(field)
        if val is not None:
            val = val.strip()
            if len(val) > maxlen:
                return JsonResponse({"ok": False, "error": "Твърде дълга стойност."}, status=400)
            if val != getattr(order, field):
                setattr(order, field, val)
                touched.add(field)

    same = request.POST.get("ship_same_as_billing")
    if same is not None:
        order.ship_same_as_billing = (same == "true")
        touched.add("ship_same_as_billing")

    if touched:
        order.save(update_fields=sorted(touched))
    return JsonResponse({"ok": True})


@require_GET
def checkout_summary(request):
    order = get_session_order(request)
    if not order:
        messages.error(request, "Няма активна поръчка.")
        return redirect("checkout_info")

    item = order.items.first()
    product = item.product if item else get_single_product()
    return render(request, "checkout/summary_readonly.html", {"order": order, "product": product})


@require_POST
def checkout_confirm_cod(request):
    order = get_session_order(request)
    if not order:
        messages.error(request, "Няма активна поръчка.")
        return redirect("checkout_info")

    if order.payment_status == PaymentStatus.PAID:
        return redirect("thank_you")  # already paid by card: never turn it into cash on delivery
    if order.payment_method != PaymentMethod.COD:
        messages.error(request, "Тази поръчка не е с наложен платеж.")
        return redirect("checkout_summary")
    if order.cod_confirmed_at and order.shipment_status != ShipmentStatus.NONE:
        return redirect("thank_you")  # double submit

    err = readiness_error(order)
    if err:
        messages.error(request, err)
        return redirect("checkout_info")

    # a card attempt that is still open must be made un-payable before we confirm cash on delivery
    if order.payment_status == PaymentStatus.PENDING and not payments.release_pending_payment(order.pk):
        messages.error(request, "Плащането с карта е в процес на обработка. Моля, изчакайте.")
        return redirect("checkout_summary")
    order.refresh_from_db()
    if order.payment_status == PaymentStatus.PAID:
        return redirect("thank_you")

    with transaction.atomic():
        locked = Order.objects.select_for_update().get(pk=order.pk)
        if locked.cod_confirmed_at is None:
            locked.cod_confirmed_at = timezone.now()
            locked.save(update_fields=["cod_confirmed_at"])
            locked.log_event("cod_confirmed", "customer confirmed cash on delivery")
    fulfillment.request_shipment(order.pk)
    outcome = fulfillment.attempt_shipment(order.pk, interactive=True)

    if outcome == "rejected":
        # Econt refused the data (definitive, nothing created): let the customer correct it
        with transaction.atomic():
            locked = Order.objects.select_for_update().get(pk=order.pk)
            locked.cod_confirmed_at = None
            locked.save(update_fields=["cod_confirmed_at"])
            locked.transition_shipment(ShipmentStatus.NONE)
        messages.error(request, f"Грешка при Еконт: {locked.econt_errors or 'Неуспешно създаване на товарителница.'}")
        return redirect("checkout_summary")

    try:
        notify_order_accepted(Order.objects.get(pk=order.pk), event="created")
    except Exception:
        logger.exception("notification failed for order %s", order.pk)
    return redirect("thank_you")

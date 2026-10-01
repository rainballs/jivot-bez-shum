import logging

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils import timezone

logger = logging.getLogger("shop.notify")


def send_order_notification(order, event="created") -> int:
    """
    Send 2 emails:
    1) to ORDER_NOTIFY_EMAIL (admin) with admin template + admin link
    2) to order.email (customer) with customer template (or fallback)
    Failures are logged and never raised: an email problem must not affect order processing.
    Returns the number of e-mails actually sent.
    """
    sent = 0
    site_url = getattr(settings, "SITE_URL", "http://127.0.0.1:8000")
    subject_status = "PAID" if order.paid else "UNPAID"
    admin_email = getattr(settings, "ORDER_NOTIFY_EMAIL", None)
    customer_email = getattr(order, "email", None)

    if admin_email:
        admin_ctx = {"order": order, "event": event, "site_url": site_url, "is_admin_mail": True,
                     "admin_path": settings.ADMIN_URL}
        admin_subject = f"[Order #{order.id}] {event} — {subject_status} — {order.full_name}"
        try:
            admin_body = render_to_string("emails/order_admin.txt", admin_ctx)
            send_mail(
                subject=admin_subject,
                message=admin_body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[admin_email],
                fail_silently=False,
            )
            sent += 1
        except Exception:
            logger.exception("Failed to send admin order notification for order %s", order.id)

    if customer_email:
        customer_ctx = {"order": order, "event": event, "site_url": site_url, "is_admin_mail": False,
                        "admin_path": settings.ADMIN_URL}
        customer_subject = f"Вашата поръчка №{order.id} е приета"
        try:
            try:
                customer_body = render_to_string("emails/order_customer.txt", customer_ctx)
            except Exception:
                customer_body = render_to_string("emails/order_admin.txt", customer_ctx)
            send_mail(
                subject=customer_subject,
                message=customer_body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[customer_email],
                fail_silently=False,
            )
            sent += 1
        except Exception:
            logger.exception("Failed to send customer order email for order %s", order.id)
    return sent


def notify_order_accepted(order, event="created") -> bool:
    """
    Send the order e-mails AT MOST ONCE per order (atomic claim on notified_at), regardless of which
    code path (webhook, browser return, COD confirmation) gets here first.
    """
    from .models import Order

    claimed = Order.objects.filter(pk=order.pk, notified_at__isnull=True).update(notified_at=timezone.now())
    if not claimed:
        return False
    order.refresh_from_db()
    if send_order_notification(order, event=event) == 0:
        # nothing could be sent (SMTP down): release the claim so the sweeper retries
        Order.objects.filter(pk=order.pk).update(notified_at=None)
        return False
    return True


def notify_pending_orders(max_age_days: int = 3, limit: int = 20) -> int:
    """Re-send missing confirmation e-mails for accepted (paid / COD-confirmed) recent orders."""
    from datetime import timedelta

    from django.db.models import Q

    from .models import Order, PaymentStatus

    since = timezone.now() - timedelta(days=max_age_days)
    qs = Order.objects.filter(notified_at__isnull=True, created_at__gte=since).filter(
        Q(payment_status=PaymentStatus.PAID) | Q(cod_confirmed_at__isnull=False)
    )[:limit]
    return sum(1 for o in qs if notify_order_accepted(o, event="paid" if o.paid else "created"))


def send_admin_alert(order, subject: str, detail: str = ""):
    """Operator-facing alert (no customer data besides the order number)."""
    admin_email = getattr(settings, "ORDER_NOTIFY_EMAIL", None)
    if not admin_email:
        return
    body = (
        f"{subject}\n\nOrder: #{order.pk}\nPayment: {order.payment_status} ({order.payment_method})\n"
        f"Shipment: {order.shipment_status}\nDetail: {detail[:500]}\n\n"
        f"Admin: {settings.SITE_URL}/{settings.ADMIN_URL}shop/order/{order.pk}/change/\n"
    )
    try:
        send_mail(
            subject=f"[ALERT] Order #{order.pk}: {subject}"[:200],
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[admin_email],
            fail_silently=False,
        )
    except Exception:
        logger.exception("Failed to send admin alert for order %s", order.pk)

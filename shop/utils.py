from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
import logging

log = logging.getLogger(__name__)


def send_order_notification(order, event="created"):
    """
    Send a single admin notification for the order.
    Never raise to the caller (checkout must not fail if email fails).
    """
    ctx = {
        "order": order,
        "event": event,
        "site_url": getattr(settings, "SITE_URL", "http://127.0.0.1:8000"),
    }
    try:
        subject_status = "PAID" if order.paid else "UNPAID"
        subject = f"[Order #{order.id}] {event} — {subject_status} — {order.full_name}"
        body = render_to_string("emails/order_admin.txt", ctx)
        recipient = getattr(settings, "ORDER_NOTIFY_EMAIL", None) or "admin@example.com"
        send_mail(
            subject=subject,
            message=body,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@localhost"),
            recipient_list=[recipient],
            fail_silently=False,
        )
    except Exception as e:
        # Log but don't interrupt the user flow
        log.exception("Failed to send order notification: %s", e)

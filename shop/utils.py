import logging
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.conf import settings

logger = logging.getLogger(__name__)


def send_order_notification(order, event="created"):
    ctx = {
        "order": order,
        "event": event,
        "site_url": getattr(settings, "SITE_URL", "http://127.0.0.1:8000"),
    }
    subject_status = "PAID" if order.paid else "UNPAID"
    subject = f"[Order #{order.id}] {event} — {subject_status} — {order.full_name}"
    body = render_to_string("emails/order_admin.txt", ctx)
    recipient = getattr(settings, "ORDER_NOTIFY_EMAIL", "admin@example.com")

    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[recipient],
            fail_silently=False,  # keep this!
        )
    except Exception as e:
        # print to console no matter what
        print("SMTP ERROR:", e)
        logger.exception("Failed to send order notification")

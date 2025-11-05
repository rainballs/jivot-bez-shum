import logging
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.conf import settings

logger = logging.getLogger(__name__)


def send_order_notification(order, event="created"):
    site_url = getattr(settings, "SITE_URL", "http://127.0.0.1:8000")
    subject_status = "PAID" if getattr(order, "paid", False) else "UNPAID"

    admin_email = getattr(settings, "ORDER_NOTIFY_EMAIL", None)
    customer_email = getattr(order, "email", None)

    # 1) send to admin
    if admin_email:
        admin_ctx = {
            "order": order,
            "event": event,
            "site_url": site_url,
            "is_admin_mail": True,  # 👈 tell template this is admin
        }
        admin_subject = f"[Order #{order.id}] {event} — {subject_status} — {order.full_name}"
        admin_body = render_to_string("emails/order_admin.txt", admin_ctx)
        try:
            msg = EmailMessage(
                subject=admin_subject,
                body=admin_body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[admin_email],
            )
            msg.send(fail_silently=False)
        except Exception:
            logger.exception("Failed to send admin mail for order %s", order.id)

    # 2) send to customer
    if customer_email:
        customer_ctx = {
            "order": order,
            "event": event,
            "site_url": site_url,
            "is_admin_mail": False,  # 👈 hide admin-only stuff
        }
        customer_subject = f"Вашата поръчка №{order.id} е приета"
        customer_body = render_to_string("emails/order_customer.txt", customer_ctx)
        try:
            cmsg = EmailMessage(
                subject=customer_subject,
                body=customer_body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[customer_email],
            )
            cmsg.send(fail_silently=False)
        except Exception:
            logger.exception("Failed to send customer mail for order %s", order.id)

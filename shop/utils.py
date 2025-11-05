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

    admin_email = getattr(settings, "ORDER_NOTIFY_EMAIL", None)
    customer_email = getattr(order, "email", None)

    # build the list
    to_list = []
    if admin_email:
        to_list.append(admin_email)
    if customer_email:
        to_list.append(customer_email)

    if not to_list:
        return  # nowhere to send

    send_mail(
        subject=subject,
        message=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=to_list,  # 👈 now both are here
        fail_silently=False,
    )

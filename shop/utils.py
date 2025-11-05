import logging
from django.core.mail import send_mail
from django.template.loader import render_to_string
from .models import PaymentMethod
from django.conf import settings

from django.conf import settings

logger = logging.getLogger(__name__)


def send_order_notification(order, event="created"):
    """
    Send 2 emails:
    1) to ORDER_NOTIFY_EMAIL (admin) with admin template + admin link
    2) to order.email (customer) with customer template (or fallback)
    """
    site_url = getattr(settings, "SITE_URL", "http://127.0.0.1:8000")
    subject_status = "PAID" if order.paid else "UNPAID"
    admin_email = getattr(settings, "ORDER_NOTIFY_EMAIL", None)
    customer_email = getattr(order, "email", None)

    # 1) admin mail
    if admin_email:
        admin_ctx = {
            "order": order,
            "event": event,
            "site_url": site_url,
            "is_admin_mail": True,
        }
        admin_subject = f"[Order #{order.id}] {event} — {subject_status} — {order.full_name}"
        admin_body = render_to_string("emails/order_admin.txt", admin_ctx)
        try:
            send_mail(
                subject=admin_subject,
                message=admin_body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[admin_email],
                fail_silently=False,
            )
        except Exception:
            logger.exception("Failed to send admin order notification for order %s", order.id)

    # 2) customer mail
    if customer_email:
        customer_ctx = {
            "order": order,
            "event": event,
            "site_url": site_url,
            "is_admin_mail": False,  # hide admin-only stuff
        }
        # try customer template first, fall back to admin template
        try:
            customer_body = render_to_string("emails/order_customer.txt", customer_ctx)
        except Exception:
            customer_body = render_to_string("emails/order_admin.txt", customer_ctx)

        customer_subject = f"Вашата поръчка №{order.id} е приета"
        try:
            send_mail(
                subject=customer_subject,
                message=customer_body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[customer_email],
                fail_silently=False,
            )
        except Exception:
            logger.exception("Failed to send customer order email for order %s", order.id)


def maybe_send_order_email(order):
    """
    Send email ONLY if:
    - we have econt label
    - AND payment is OK (COD always OK, card/apple/google only if paid)
    """
    # 1) label present?
    has_label = bool(getattr(order, "econt_label_pdf", None)) or bool(getattr(order, "econt_label_id", None))
    if not has_label:
        return

    # 2) payment method present?
    pay_method = getattr(order, "payment_method", None)
    if not pay_method:
        return

    # 3) rules based on your actual choices
    if pay_method == PaymentMethod.COD:
        # COD is fine right after label
        send_order_notification(order, event="created")
        return

    if pay_method in {PaymentMethod.CARD, PaymentMethod.APPLE_PAY, PaymentMethod.GOOGLE_PAY}:
        if getattr(order, "paid", False):
            send_order_notification(order, event="created")
        # else: wait for Stripe/webhook

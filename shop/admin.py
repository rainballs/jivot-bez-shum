from django.contrib import admin, messages
from django.utils.html import format_html

from . import fulfillment
from .models import (
    DeliveryMethod,
    Order,
    OrderEvent,
    OrderItem,
    PaymentAttempt,
    Product,
    ShipmentAttempt,
    ShipmentStatus,
    StripeEvent,
)


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "price_eur", "is_active")
    fields = ("name", "slug", "price_eur", "image", "is_active")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    fields = ("product", "quantity", "unit_price_eur")
    extra = 0


class ReadOnlyInline(admin.TabularInline):
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class OrderEventInline(ReadOnlyInline):
    model = OrderEvent
    fields = ("created_at", "kind", "message", "data")
    readonly_fields = fields


class ShipmentAttemptInline(ReadOnlyInline):
    model = ShipmentAttempt
    fields = ("started_at", "number", "outcome", "http_status", "shipment_num", "error", "request_summary")
    readonly_fields = fields


class PaymentAttemptInline(ReadOnlyInline):
    model = PaymentAttempt
    fields = ("created_at", "stripe_session_id", "payment_intent_id", "amount_minor", "currency", "status", "paid_at")
    readonly_fields = fields


@admin.action(description="Повтори изпращането към Еконт (само при ГРЕШКА)")
def action_requeue_failed(modeladmin, request, queryset):
    n = 0
    for order in queryset.filter(shipment_status=ShipmentStatus.FAILED):
        fulfillment.allow_retry_after_manual_check(order.pk, who=f"admin:{request.user.pk}")
        n += 1
    modeladmin.message_user(request, f"Върнати в опашката: {n}. Ще бъдат изпратени от process_shipments.")


@admin.action(description="НЕЯСЕН резултат: потвърдих в e-Econt, че НЯМА товарителница -> опитай пак")
def action_retry_unknown(modeladmin, request, queryset):
    n = 0
    for order in queryset.filter(shipment_status=ShipmentStatus.UNKNOWN):
        fulfillment.allow_retry_after_manual_check(order.pk, who=f"admin:{request.user.pk}")
        n += 1
    modeladmin.message_user(
        request,
        f"Върнати в опашката: {n}. ВНИМАНИЕ: използвайте само ако сте проверили в e-Econt, че няма създадена товарителница.",
        level=messages.WARNING,
    )


@admin.action(description="Маркирай като прегледано (изчисти флага)")
def action_clear_review(modeladmin, request, queryset):
    for order in queryset:
        order.log_event("review_cleared", f"admin:{request.user.pk}")
    queryset.update(needs_review=False)


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        "id", "full_name", "payment_method", "payment_status", "shipment_status", "needs_review",
        "econt_shipment_num", "econt_status", "label_link", "total_eur", "created_at",
    )
    list_filter = ("needs_review", "payment_status", "shipment_status", "payment_method", "delivery_method", "created_at")
    search_fields = ("full_name", "email", "phone", "city", "office_text", "econt_shipment_num", "stripe_payment_intent_id")
    actions = [action_requeue_failed, action_retry_unknown, action_clear_review]
    inlines = [OrderItemInline, PaymentAttemptInline, ShipmentAttemptInline, OrderEventInline]

    # Verified state is read-only here: payment/shipping fields change only through the code paths that verify them.
    readonly_fields = (
        "public_id", "paid", "payment_status", "paid_at", "paid_amount_minor", "paid_currency",
        "stripe_payment_intent_id", "cod_confirmed_at", "shipping_quoted_at",
        "shipment_status", "shipment_attempts", "shipment_next_attempt_at", "shipment_claimed_at",
        "econt_cod_amount", "econt_cod_currency", "econt_receiver_pays_delivery", "econt_label_url",
        "econt_shipment_num", "econt_status", "econt_status_checked_at", "econt_errors", "econt_label_pdf",
        "needs_review", "review_reason", "notified_at", "created_at", "delivery_preview",
    )

    fieldsets = (
        ("Внимание", {"fields": ("needs_review", "review_reason")}),
        ("Клиент", {"fields": ("full_name", "email", "phone", "payment_method")}),
        ("Плащане (потвърдено)", {
            "fields": ("payment_status", "paid", "paid_at", "paid_amount_minor", "paid_currency",
                       "stripe_payment_intent_id", "cod_confirmed_at")
        }),
        ("Фактуриране", {
            "fields": ("billing_full_name", "billing_email", "billing_phone", "ship_same_as_billing")
        }),
        ("Доставка (реални полета за Еконт)", {
            "fields": (
                "delivery_method", "city", "postal_code", "address_line", "receiver_street", "receiver_num",
                "receiver_entrance", "receiver_floor", "receiver_apartment", "receiver_quarter", "receiver_other", "office_text", "econt_office_code",
                "delivery_preview",
            )
        }),
        ("Еконт (изпращане)", {
            "fields": (
                "shipment_status", "shipment_attempts", "shipment_next_attempt_at", "shipment_claimed_at",
                "econt_shipment_num", "econt_status", "econt_status_checked_at", "econt_errors", "econt_label_url",
                "econt_label_pdf",
                "econt_cod_amount", "econt_cod_currency", "econt_receiver_pays_delivery", "shipping_quoted_at",
            )
        }),
        ("Суми", {
            "fields": (
                "quantity",
                "subtotal_eur", "shipping_eur", "total_eur",
            )
        }),
        ("Технически", {"fields": ("courier", "public_id", "notified_at", "created_at")}),
    )

    @admin.display(description="Етикет")
    def label_link(self, obj):
        if not obj.econt_label_url:
            return "—"
        return format_html('<a href="{}" target="_blank" rel="noopener">PDF</a>', obj.econt_label_url)

    @admin.display(description="Адрес за доставка (преглед)")
    def delivery_preview(self, obj):
        if obj.delivery_method == DeliveryMethod.TO_OFFICE:
            return f"Офис/АПС: {obj.office_text or obj.econt_office_code or '—'}"
        city = obj.city or "—"
        addr = obj.address_line or "—"
        pc = obj.postal_code or "—"
        return f"{city}, {addr}, {pc}"


@admin.register(OrderItem)
class OrderItemAdmin(admin.ModelAdmin):
    list_display = ("order", "product", "quantity", "unit_price_eur")
    fields = ("order", "product", "quantity", "unit_price_eur")


@admin.register(StripeEvent)
class StripeEventAdmin(admin.ModelAdmin):
    list_display = ("event_id", "event_type", "status", "order", "livemode", "received_at", "processed_at")
    list_filter = ("status", "event_type", "livemode")
    search_fields = ("event_id",)
    readonly_fields = [f.name for f in StripeEvent._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

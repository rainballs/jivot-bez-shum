from django.contrib import admin

# Register your models here.
from django.contrib import admin
from .models import Product, Order, OrderItem, DeliveryMethod


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "price_bgn", "price_eur", "is_active")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        "id", "full_name", "email", "phone",
        "delivery_method", "paid", "total_bgn", "created_at",
    )
    list_filter = ("paid", "delivery_method", "created_at")
    search_fields = ("full_name", "email", "phone", "city", "office_text", "econt_shipment_num")

    # IMPORTANT: put computed helpers ONLY here
    readonly_fields = (
        "delivery_preview",
        "econt_shipment_num",
        "econt_status",
        "econt_errors",
        "econt_label_pdf",
        "created_at",
    )

    fieldsets = (
        ("Клиент", {
            "fields": ("full_name", "email", "phone", "paid", "payment_method")
        }),
        ("Фактуриране", {
            "fields": (
                "billing_full_name", "billing_email", "billing_phone",
                "billing_city", "billing_address_line", "billing_postal_code",
                "ship_same_as_billing",
            )
        }),
        ("Доставка (реални полета за Еконт)", {
            "fields": (
                "city", "postal_code", "address_line", "office_text",
                "econt_office_code", "econt_shipment_num",
                "econt_status", "econt_errors", "econt_label_pdf",
                "delivery_preview",  # <- allowed here *because* it's in readonly_fields
            )
        }),
        ("Суми", {
            "fields": ("quantity", "subtotal_bgn", "shipping_bgn", "total_bgn",
                       "subtotal_eur", "shipping_eur", "total_eur")
        }),
        ("Технически", {
            "fields": ("delivery_method", "courier", "created_at")
        }),
    )

    def delivery_preview(self, obj):
        """
        Nice one-line preview in admin.
        Shows either Address or Office depending on delivery_method.
        """
        if obj.delivery_method == obj.DeliveryMethod.TO_OFFICE:
            return f"Офис/АПС: {obj.office_text or obj.econt_office_code or '—'}"
        city = obj.city or "—"
        addr = obj.address_line or "—"
        pc = obj.postal_code or "—"
        return f"{city}, {addr}, {pc}"

    delivery_preview.short_description = "Адрес за доставка (преглед)"


@admin.register(OrderItem)
class OrderItemAdmin(admin.ModelAdmin):
    list_display = ("order", "product", "quantity", "unit_price_bgn")

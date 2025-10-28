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
        "delivery_method", "courier", "paid", "payment_method",
        "city", "created_at",
    )
    list_filter = ("paid", "payment_method", "delivery_method", "courier", "created_at")
    search_fields = (
        "id", "full_name", "email", "phone",
        "billing_full_name", "billing_email", "billing_phone",
        "city", "address_line", "office_text", "econt_shipment_num",
    )
    readonly_fields = ("created_at", "billing_preview", "shipping_preview")

    fieldsets = (
        ("Клиент", {
            "fields": (
                ("full_name", "email", "phone"),
                ("delivery_method", "courier", "payment_method", "paid"),
                "created_at",
            )
        }),
        ("Доставка (реални полета за Еконт)", {
            "fields": (
                ("city", "postal_code"),
                "address_line",
                ("office_text", "econt_office_code"),
                ("econt_shipment_num", "econt_status"),
                "econt_errors",
                "econt_label_pdf",
                "delivery_preview",
            ),
        }),
        ("Фактура (billing)", {
            "fields": (
                ("billing_full_name", "billing_email", "billing_phone"),
                ("billing_city", "billing_postcode"),
                ("billing_street", "billing_num"),
                ("billing_entrance", "billing_floor", "billing_apartment"),
                "ship_same_as_billing",
                "billing_preview",
            )
        }),
        ("Суми", {
            "fields": (
                ("quantity",),
                ("subtotal_bgn", "shipping_bgn", "total_bgn"),
                ("subtotal_eur", "shipping_eur", "total_eur"),
            )
        }),
    )

    def billing_preview(self, obj):
        return obj.billing_full_address()

    billing_preview.short_description = "Фактурен адрес (преглед)"

    def shipping_preview(self, obj):
        return obj.shipping_full_address()

    shipping_preview.short_description = "Адрес за доставка (преглед)"

    @admin.display(description="Адрес за доставка (преглед)")
    def delivery_preview(self, obj):
        if obj.delivery_method == DeliveryMethod.TO_OFFICE:
            return f"{obj.city}, офис {obj.office_text or obj.econt_office_code}"
        elif obj.delivery_method == DeliveryMethod.TO_ADDRESS:
            parts = [obj.city, obj.address_line, obj.postal_code]
            return ", ".join(p for p in parts if p)
        return "-"


# (Optional) if you manage items in admin
@admin.register(OrderItem)
class OrderItemAdmin(admin.ModelAdmin):
    list_display = ("id", "order", "product", "quantity", "unit_price_bgn", "unit_price_eur")
    search_fields = ("order__id", "product__name")

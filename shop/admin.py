from django.contrib import admin

# Register your models here.
from django.contrib import admin
from .models import Product, Order, OrderItem


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
    list_display = ("id", "full_name", "total_bgn", "total_eur", "paid", "created_at")
    readonly_fields = ("subtotal_bgn", "subtotal_eur", "shipping_bgn", "shipping_eur", "total_bgn", "total_eur",
                       "created_at")
    inlines = [OrderItemInline]
    search_fields = ("full_name", "email", "phone")

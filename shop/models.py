from django.db import models

# Create your models here.
from django.db import models
from django.utils.translation import gettext_lazy as _
from decimal import Decimal


class Product(models.Model):
    name = models.CharField(max_length=200, verbose_name=_("Име"))
    slug = models.SlugField(max_length=220, unique=True)
    price_bgn = models.DecimalField(max_digits=10, decimal_places=2, verbose_name=_("Цена (лв)"))
    price_eur = models.DecimalField(max_digits=10, decimal_places=2, verbose_name=_("Цена (€)"))
    image = models.ImageField(upload_to="products/", blank=True, null=True, verbose_name=_("Изображение"))
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = _("Продукт")
        verbose_name_plural = _("Продукти")

    def __str__(self):
        return self.name


class DeliveryMethod(models.TextChoices):
    TO_ADDRESS = "address", _("Доставка до адрес")
    TO_OFFICE = "office", _("Доставка до офис/АПС")


class Courier(models.TextChoices):
    SPEEDY = "speedy", _("Спиди")
    EKONT = "econt", _("Еконт")


class PaymentMethod(models.TextChoices):
    CARD = "card", _("Плащане с карта")
    APPLE_PAY = "apple_pay", _("Apple Pay")
    GOOGLE_PAY = "google_pay", _("Google Pay")
    COD = "cod", _("Наложен платеж")


class Order(models.Model):
    full_name = models.CharField(max_length=150, verbose_name=_("Име и фамилия"))
    email = models.EmailField(verbose_name=_("Имейл адрес"))
    phone = models.CharField(max_length=32, verbose_name=_("Телефон"))

    delivery_method = models.CharField(max_length=16, choices=DeliveryMethod.choices, default=DeliveryMethod.TO_ADDRESS)
    courier = models.CharField(max_length=16, choices=Courier.choices, default=Courier.SPEEDY)

    address_line = models.CharField(max_length=255, blank=True, verbose_name=_("Адрес"))
    city = models.CharField(max_length=120, blank=True, verbose_name=_("Град"))
    postal_code = models.CharField(max_length=16, blank=True, verbose_name=_("Пощенски код"))
    office_text = models.CharField(max_length=255, blank=True, verbose_name=_("Офис / АПС"))

    quantity = models.PositiveIntegerField(default=1)
    subtotal_bgn = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    subtotal_eur = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    shipping_bgn = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    shipping_eur = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_bgn = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_eur = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    payment_method = models.CharField(max_length=20, choices=PaymentMethod.choices, blank=True)
    paid = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("Поръчка")
        verbose_name_plural = _("Поръчки")

    def __str__(self):
        return f"Order #{self.id or '—'} — {self.full_name}"

    def set_shipping_flat(self):
        self.shipping_bgn = Decimal("5.00")
        self.shipping_eur = Decimal("2.50")

    def recompute_totals(self):
        items = list(self.items.all())
        sbgn = sum((i.unit_price_bgn * i.quantity for i in items), start=Decimal("0"))
        seur = sum((i.unit_price_eur * i.quantity for i in items), start=Decimal("0"))
        self.subtotal_bgn = sbgn
        self.subtotal_eur = seur
        self.set_shipping_flat()
        self.total_bgn = sbgn + self.shipping_bgn
        self.total_eur = seur + self.shipping_eur

    def save(self, *args, **kwargs):
        creating = self.pk is None
        super().save(*args, **kwargs)  # first save to obtain PK if creating
        # Only recompute when there are items; skip on the very first save
        if self.items.exists():
            self.recompute_totals()
            super().save(update_fields=[
                "subtotal_bgn", "subtotal_eur", "shipping_bgn", "shipping_eur",
                "total_bgn", "total_eur"
            ])


class OrderItem(models.Model):
    order = models.ForeignKey(Order, related_name="items", on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField(default=1)
    unit_price_bgn = models.DecimalField(max_digits=10, decimal_places=2)
    unit_price_eur = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        verbose_name = _("Артикул")
        verbose_name_plural = _("Артикули")

    def __str__(self):
        return f"{self.product.name} x{self.quantity}"

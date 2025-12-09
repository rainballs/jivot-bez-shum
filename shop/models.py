from django.db import models

# Create your models here.
from django.db import models
from django.utils.translation import gettext_lazy as _
from decimal import Decimal, ROUND_HALF_UP

BGN_PER_EUR = Decimal("1.95583")


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
    EKONT = "econt", _("Еконт")


class PaymentMethod(models.TextChoices):
    CARD = "card", _("Плащане с карта")
    APPLE_PAY = "apple_pay", _("Apple Pay")
    GOOGLE_PAY = "google_pay", _("Google Pay")
    COD = "cod", _("Наложен платеж")


def _bgn_to_eur(amount_bgn: Decimal) -> Decimal:
    return (amount_bgn / BGN_PER_EUR).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class Order(models.Model):
    full_name = models.CharField(max_length=150, verbose_name=_("Име и фамилия"))
    email = models.EmailField(verbose_name=_("Имейл адрес"))
    phone = models.CharField(max_length=32, verbose_name=_("Телефон"))

    delivery_method = models.CharField(max_length=16, choices=DeliveryMethod.choices, default=DeliveryMethod.TO_ADDRESS)
    courier = models.CharField(max_length=16, choices=Courier.choices, default=Courier.EKONT)

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

    econt_shipment_num = models.CharField(max_length=64, blank=True, null=True)
    econt_label_pdf = models.FileField(upload_to="econt_labels/", blank=True, null=True)
    econt_status = models.CharField(max_length=64, blank=True, null=True)
    econt_errors = models.TextField(blank=True, null=True)
    # optional, if you let user pick office:
    econt_office_code = models.CharField(max_length=16, blank=True, null=True)

    payment_method = models.CharField(
        max_length=20,
        choices=PaymentMethod.choices,
        default=PaymentMethod.CARD,
        verbose_name=_("Метод на плащане"),
    )
    paid = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    # --- Billing (invoice) address ---
    billing_full_name = models.CharField(max_length=200, blank=True, default="",
                                         verbose_name=_("Фактура: Име и фамилия"))
    billing_email = models.EmailField(blank=True, default="", verbose_name=_("Фактура: Имейл"))
    billing_phone = models.CharField(max_length=32, blank=True, default="", verbose_name=_("Фактура: Телефон"))
    billing_city = models.CharField(max_length=120, blank=True, default="", verbose_name=_("Фактура: Град"))
    billing_street = models.CharField(max_length=255, blank=True, default="", verbose_name=_("Фактура: Улица/бул."))
    billing_num = models.CharField(max_length=32, blank=True, default="", verbose_name=_("Фактура: №"))
    billing_postcode = models.CharField(max_length=16, blank=True, default="", verbose_name=_("Фактура: Пощ. код"))
    billing_entrance = models.CharField(max_length=16, blank=True, default="", verbose_name=_("Фактура: Вход"))
    billing_floor = models.CharField(max_length=16, blank=True, default="", verbose_name=_("Фактура: Етаж"))
    billing_apartment = models.CharField(max_length=16, blank=True, default="", verbose_name=_("Фактура: Апартамент"))

    # If True, prefill shipping on the next step with the billing data (you already use this in the view)
    ship_same_as_billing = models.BooleanField(default=True, verbose_name=_("Използвай фактурния адрес за доставка"))

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("Поръчка")
        verbose_name_plural = _("Поръчки")

    def __str__(self):
        return f"Order #{self.id or '—'} — {self.full_name}"

    def billing_full_address(self) -> str:
        parts = [
            self.billing_city,
            f"{self.billing_street} №{self.billing_num}".strip(),
            f"вх. {self.billing_entrance}" if self.billing_entrance else "",
            f"ет. {self.billing_floor}" if self.billing_floor else "",
            f"ап. {self.billing_apartment}" if self.billing_apartment else "",
            self.billing_postcode,
        ]
        return ", ".join(p for p in parts if p)

    def shipping_full_address(self) -> str:
        # Uses your existing shipping fields
        parts = [self.city, self.address_line, self.postal_code, self.office_text]
        return ", ".join(p for p in parts if p)

    def set_shipping_flat(self):
        """
        Default доставка: 9.00 лв до адрес, 7.00 лв до офис.

        ВАЖНО:
        - Ако вече имаме реална цена от Еконт (shipping_bgn > 0),
          НЕ я пипаме, само синхронизираме EUR.
        """
        if self.shipping_bgn and self.shipping_bgn > 0:
            # вече имаме цена от Еконт → само синхронизираме евро
            self.shipping_eur = _bgn_to_eur(self.shipping_bgn)
            return

        # иначе – placeholder 9 / 7 лв (преди да сме говорили с Еконт)
        if self.delivery_method == DeliveryMethod.TO_ADDRESS:
            ship_bgn = Decimal("9.00")
        else:
            ship_bgn = Decimal("7.00")

        self.shipping_bgn = ship_bgn
        self.shipping_eur = _bgn_to_eur(ship_bgn)

    def recompute_totals(self):
        items = list(self.items.all())
        sbgn = sum((i.unit_price_bgn * i.quantity for i in items), start=Decimal("0"))
        seur = sum((i.unit_price_eur * i.quantity for i in items), start=Decimal("0"))
        self.subtotal_bgn = sbgn
        self.subtotal_eur = seur
        self.set_shipping_flat()  # ← uses delivery_method
        self.total_bgn = sbgn + self.shipping_bgn
        self.total_eur = seur + self.shipping_eur

    # def save(self, *args, **kwargs):
    #     creating = self.pk is None
    #     super().save(*args, **kwargs)  # first save to obtain PK if creating
    #     # Only recompute when there are items; skip on the very first save
    #     if self.items.exists():
    #         self.recompute_totals()
    #         super().save(update_fields=[
    #             "subtotal_bgn", "subtotal_eur", "shipping_bgn", "shipping_eur",
    #             "total_bgn", "total_eur"
    #         ])


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

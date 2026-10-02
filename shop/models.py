import uuid
from decimal import Decimal, ROUND_HALF_UP

from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _


class Product(models.Model):
    name = models.CharField(max_length=200, verbose_name=_("Име"))
    slug = models.SlugField(max_length=220, unique=True)
    # The shop sells in EUR only. price_bgn is a legacy column kept for old data; it is no longer used anywhere.
    price_bgn = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, editable=False,
                                    verbose_name=_("Цена (лв) - остаряло"))
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


CARD_METHODS = {PaymentMethod.CARD, PaymentMethod.APPLE_PAY, PaymentMethod.GOOGLE_PAY}


class PaymentStatus(models.TextChoices):
    """Verified payment state. Only the server (Stripe-verified) may set PAID for card orders."""
    UNPAID = "unpaid", _("Неплатена")
    PENDING = "pending", _("Чака плащане")
    PAID = "paid", _("Платена")
    FAILED = "failed", _("Неуспешно плащане")


class ShipmentStatus(models.TextChoices):
    """Fulfilment state, deliberately separate from payment state."""
    NONE = "none", _("Няма заявка")
    PENDING = "pending", _("Чака изпращане към Еконт")
    IN_PROGRESS = "in_progress", _("Изпраща се към Еконт")
    CREATED = "created", _("Товарителница създадена")
    FAILED = "failed", _("Грешка при Еконт")
    UNKNOWN = "unknown", _("Неясен резултат (проверка)")


class InvalidTransition(Exception):
    pass


PAYMENT_TRANSITIONS = {
    PaymentStatus.UNPAID: {PaymentStatus.PENDING, PaymentStatus.PAID, PaymentStatus.FAILED},
    PaymentStatus.PENDING: {PaymentStatus.UNPAID, PaymentStatus.PAID, PaymentStatus.FAILED},
    PaymentStatus.FAILED: {PaymentStatus.UNPAID, PaymentStatus.PENDING, PaymentStatus.PAID},
    PaymentStatus.PAID: set(),  # terminal - refunds are handled manually in Stripe
}

SHIPMENT_TRANSITIONS = {
    ShipmentStatus.NONE: {ShipmentStatus.PENDING},
    ShipmentStatus.PENDING: {ShipmentStatus.IN_PROGRESS, ShipmentStatus.NONE},
    ShipmentStatus.IN_PROGRESS: {
        ShipmentStatus.CREATED, ShipmentStatus.FAILED, ShipmentStatus.UNKNOWN, ShipmentStatus.PENDING,
    },
    ShipmentStatus.FAILED: {ShipmentStatus.PENDING, ShipmentStatus.NONE},
    ShipmentStatus.UNKNOWN: {ShipmentStatus.CREATED, ShipmentStatus.PENDING},
    ShipmentStatus.CREATED: set(),
}


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
    # Structured receiver address (Econt requires street and number separately). `address_line` is the
    # human-readable combination; these fields are what the shipment payload is built from.
    receiver_street = models.CharField(max_length=255, blank=True, default="")
    receiver_num = models.CharField(max_length=32, blank=True, default="")
    receiver_entrance = models.CharField(max_length=16, blank=True, default="")
    receiver_floor = models.CharField(max_length=16, blank=True, default="")
    receiver_apartment = models.CharField(max_length=16, blank=True, default="")
    # Econt asks for these when a street exists in several quarters / for "ж.к." addresses (needs a block)
    receiver_quarter = models.CharField(max_length=64, blank=True, default="")
    receiver_other = models.CharField(max_length=128, blank=True, default="")

    quantity = models.PositiveIntegerField(default=1)
    # Amounts are in EUR. The *_bgn columns only hold the values of old orders (kept for history, never written
    # or displayed any more).
    subtotal_bgn = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    subtotal_eur = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    shipping_bgn = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    shipping_eur = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_bgn = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_eur = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    econt_shipment_num = models.CharField(max_length=64, blank=True, null=True)
    econt_label_pdf = models.FileField(upload_to="econt_labels/", blank=True, null=True)
    econt_status = models.CharField(max_length=64, blank=True, null=True)  # Econt's own status text (refreshed)
    econt_status_checked_at = models.DateTimeField(null=True, blank=True)
    econt_errors = models.TextField(blank=True, null=True)
    # optional, if you let user pick office:
    econt_office_code = models.CharField(max_length=16, blank=True, null=True)

    payment_method = models.CharField(
        max_length=20,
        choices=PaymentMethod.choices,
        default=PaymentMethod.CARD,
        verbose_name=_("Метод на плащане"),
    )
    # Legacy flag, kept in sync with payment_status by Order.transition_payment(); never edit directly.
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

    # --- Unguessable public identifier (never expose the sequential pk in URLs) ---
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)

    # --- Verified payment state (source of truth; `payment_method` is only the customer's choice) ---
    payment_status = models.CharField(
        max_length=16, choices=PaymentStatus.choices, default=PaymentStatus.UNPAID, db_index=True,
    )
    paid_at = models.DateTimeField(null=True, blank=True)
    paid_amount_minor = models.PositiveIntegerField(null=True, blank=True)
    paid_currency = models.CharField(max_length=3, blank=True, default="")
    stripe_payment_intent_id = models.CharField(max_length=80, blank=True, default="", db_index=True)

    # COD must be explicitly confirmed by the customer before anything is shipped.
    cod_confirmed_at = models.DateTimeField(null=True, blank=True)
    # Set when Econt returned a live price for the current address/method; cleared when delivery data changes.
    shipping_quoted_at = models.DateTimeField(null=True, blank=True)

    # --- Fulfilment state ---
    shipment_status = models.CharField(
        max_length=16, choices=ShipmentStatus.choices, default=ShipmentStatus.NONE, db_index=True,
    )
    shipment_attempts = models.PositiveSmallIntegerField(default=0)
    shipment_next_attempt_at = models.DateTimeField(null=True, blank=True)
    shipment_claimed_at = models.DateTimeField(null=True, blank=True)
    # What we actually asked Econt to collect (recorded at submission, for audit/reconciliation).
    econt_cod_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    econt_cod_currency = models.CharField(max_length=3, blank=True, default="")
    econt_receiver_pays_delivery = models.BooleanField(null=True, blank=True)
    econt_label_url = models.URLField(max_length=500, blank=True, default="")

    # --- Operator attention ---
    needs_review = models.BooleanField(default=False, db_index=True)
    review_reason = models.TextField(blank=True, default="")
    notified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("Поръчка")
        verbose_name_plural = _("Поръчки")
        constraints = [
            models.UniqueConstraint(
                fields=["econt_shipment_num"],
                condition=Q(econt_shipment_num__isnull=False) & ~Q(econt_shipment_num=""),
                name="uniq_order_econt_shipment_num",
            ),
            models.CheckConstraint(
                condition=~Q(shipment_status="created")
                          | (Q(econt_shipment_num__isnull=False) & ~Q(econt_shipment_num="")),
                name="order_created_shipment_has_number",
            ),
        ]

    def __str__(self):
        return f"Order #{self.id or '—'} — {self.full_name}"

    # ------------------------------------------------------------------ state helpers
    @property
    def is_card(self) -> bool:
        return self.payment_method in CARD_METHODS

    @property
    def is_locked(self) -> bool:
        """Once a payment attempt exists / money moved / a label exists, checkout data is frozen."""
        return (
            self.payment_status in (PaymentStatus.PENDING, PaymentStatus.PAID)
            or self.cod_confirmed_at is not None
            or self.shipment_status != ShipmentStatus.NONE
        )

    @property
    def delivery_ready(self) -> bool:
        """Complete, quoted delivery data - required before taking money or confirming COD."""
        if not (self.full_name or "").strip() or not (self.phone or "").strip() or not (self.city or "").strip():
            return False
        if self.delivery_method == DeliveryMethod.TO_OFFICE:
            ok = bool((self.econt_office_code or "").strip())
        else:
            ok = (
                bool((self.receiver_street or "").strip())
                and bool((self.receiver_num or "").strip())
                and bool((self.postal_code or "").strip())
            )
        return ok and self.shipping_quoted_at is not None

    def log_event(self, kind: str, message: str = "", **data):
        return OrderEvent.objects.create(order=self, kind=kind, message=message[:255], data=data)

    def flag_review(self, reason: str, save: bool = True):
        """Make an ambiguous/inconsistent state visible to operators. Idempotent per reason."""
        if reason not in (self.review_reason or ""):
            self.review_reason = (f"{self.review_reason}\n" if self.review_reason else "") + reason
        self.needs_review = True
        if save and self.pk:
            Order.objects.filter(pk=self.pk).update(needs_review=True, review_reason=self.review_reason)
            self.log_event("review_flagged", reason)

    def transition_payment(self, new, save: bool = True):
        new = PaymentStatus(new)
        old = PaymentStatus(self.payment_status)
        if new == old:
            return
        if new not in PAYMENT_TRANSITIONS[old]:
            raise InvalidTransition(f"payment {old} -> {new} not allowed (order {self.pk})")
        self.payment_status = new
        if new == PaymentStatus.PAID:
            self.paid = True
        if save:
            self.save(update_fields=["payment_status", "paid"])
            self.log_event("payment_status", f"{old} -> {new}")

    def transition_shipment(self, new, save: bool = True, extra_fields=()):
        new = ShipmentStatus(new)
        old = ShipmentStatus(self.shipment_status)
        if new == old:
            return
        if new not in SHIPMENT_TRANSITIONS[old]:
            raise InvalidTransition(f"shipment {old} -> {new} not allowed (order {self.pk})")
        self.shipment_status = new
        if save:
            self.save(update_fields=["shipment_status", *extra_fields])
            self.log_event("shipment_status", f"{old} -> {new}")

    def set_quantity(self, qty: int):
        """Single place that keeps Order.quantity and the OrderItem in sync."""
        self.quantity = qty
        self.items.update(quantity=qty)

    def invalidate_quote(self):
        """Delivery data changed: the previous Econt price/quote is no longer valid."""
        self.shipping_quoted_at = None

    # ------------------------------------------------------------------ display helpers
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

    @property
    def econt_tracking_url(self) -> str:
        return f"https://www.econt.com/services/track-shipment/{self.econt_shipment_num}" if self.econt_shipment_num else ""

    @property
    def delivery_target(self) -> str:
        """Human-readable destination for e-mails/admin (office code is the source of truth for office orders)."""
        if self.delivery_method == DeliveryMethod.TO_OFFICE:
            office = (self.office_text or "").strip()
            code = (self.econt_office_code or "").strip()
            return f"Офис на Еконт {code}" + (f" – {office}" if office else "") + (f", {self.city}" if self.city else "")
        return ", ".join(p for p in (self.address_line, self.postal_code, self.city) if p)

    def shipping_full_address(self) -> str:
        # Uses your existing shipping fields
        parts = [self.city, self.address_line, self.postal_code, self.office_text]
        return ", ".join(p for p in parts if p)

    # Placeholder delivery estimate (EUR) shown before Econt has quoted the real price. It is NEVER charged:
    # payment and COD confirmation require shipping_quoted_at (see delivery_ready).
    ESTIMATE_SHIPPING_ADDRESS = Decimal("4.60")
    ESTIMATE_SHIPPING_OFFICE = Decimal("3.58")

    def set_shipping_flat(self):
        """Keep a real Econt price if there is one; otherwise use the placeholder estimate for the method."""
        if self.shipping_eur and self.shipping_eur > 0:
            return
        self.shipping_eur = (
            self.ESTIMATE_SHIPPING_ADDRESS if self.delivery_method == DeliveryMethod.TO_ADDRESS
            else self.ESTIMATE_SHIPPING_OFFICE
        )

    def recompute_totals(self):
        items = list(self.items.all())
        self.subtotal_eur = sum((i.unit_price_eur * i.quantity for i in items), start=Decimal("0"))
        self.set_shipping_flat()
        self.total_eur = self.subtotal_eur + self.shipping_eur


class OrderItem(models.Model):
    order = models.ForeignKey(Order, related_name="items", on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField(default=1)
    unit_price_bgn = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, editable=False)
    unit_price_eur = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        verbose_name = _("Артикул")
        verbose_name_plural = _("Артикули")

    def __str__(self):
        return f"{self.product.name} x{self.quantity}"


class PaymentAttempt(models.Model):
    """One Stripe Checkout Session created for an order, with the amount we expect to be paid."""

    class Status(models.TextChoices):
        OPEN = "open", "open"
        PAID = "paid", "paid"
        EXPIRED = "expired", "expired"
        FAILED = "failed", "failed"
        SUPERSEDED = "superseded", "superseded"

    order = models.ForeignKey(Order, related_name="payment_attempts", on_delete=models.CASCADE)
    stripe_session_id = models.CharField(max_length=120, unique=True)
    payment_intent_id = models.CharField(max_length=120, blank=True, default="")
    amount_minor = models.PositiveIntegerField()
    currency = models.CharField(max_length=3)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN, db_index=True)
    checkout_url = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.stripe_session_id} ({self.status})"


class StripeEvent(models.Model):
    """Durable webhook de-duplication / audit. event_id is unique: each Stripe event is processed once."""

    class Status(models.TextChoices):
        RECEIVED = "received", "received"
        PROCESSED = "processed", "processed"
        IGNORED = "ignored", "ignored"
        REJECTED = "rejected", "rejected"  # verified event that did not match our records
        ERROR = "error", "error"  # processing raised: Stripe will retry

    event_id = models.CharField(max_length=80, unique=True)
    event_type = models.CharField(max_length=80)
    livemode = models.BooleanField(default=False)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RECEIVED)
    order = models.ForeignKey(Order, null=True, blank=True, on_delete=models.SET_NULL, related_name="stripe_events")
    detail = models.TextField(blank=True, default="")
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-received_at"]

    def __str__(self):
        return f"{self.event_id} {self.event_type} {self.status}"


class OrderEvent(models.Model):
    """Append-only audit trail of payment and shipping changes. Never store personal data in `data`."""
    order = models.ForeignKey(Order, related_name="events", on_delete=models.CASCADE)
    kind = models.CharField(max_length=40, db_index=True)
    message = models.CharField(max_length=255, blank=True, default="")
    data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"#{self.order_id} {self.kind}"


class ShipmentAttempt(models.Model):
    """One call to Econt createLabel. The row is written BEFORE the call so a crash is visible afterwards."""

    class Outcome(models.TextChoices):
        STARTED = "started", "started (no result recorded)"
        CREATED = "created", "created"
        REJECTED = "rejected", "rejected by Econt (definitive)"
        NOT_SENT = "not_sent", "request never reached Econt"
        UNKNOWN = "unknown", "unknown - Econt may have created it"

    order = models.ForeignKey(Order, related_name="shipment_attempts_log", on_delete=models.CASCADE)
    uid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    number = models.PositiveSmallIntegerField(default=1)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField(max_length=16, choices=Outcome.choices, default=Outcome.STARTED, db_index=True)
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    shipment_num = models.CharField(max_length=64, blank=True, default="")
    error = models.TextField(blank=True, default="")
    request_summary = models.JSONField(default=dict, blank=True)
    response_summary = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"order {self.order_id} attempt {self.number} {self.outcome}"

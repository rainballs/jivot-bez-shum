# shop/forms.py
from django import forms
from django.core.validators import RegexValidator
from .models import Order, PaymentMethod

phone_validator = RegexValidator(
    regex=r"^\+?\d[\d\s\-]{6,}$",
    message="Моля, въведете валиден телефон (пример: +359 888 123 456).",
)

postcode_validator = RegexValidator(
    regex=r"^\d{4}$",
    message="Пощенският код трябва да е 4 цифри.",
)


class CheckoutInfoForm(forms.ModelForm):
    # keep your existing quantity setup
    quantity = forms.IntegerField(
        min_value=1,
        initial=1,
        widget=forms.NumberInput(attrs={"class": "qty-input", "inputmode": "numeric"}),
        label="",
    )

    # Billing (invoice) section + toggle (names match your Order fields)
    billing_full_name = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "Име и фамилия "}),
        label="Име и фамилия (фактура)",
    )
    billing_email = forms.EmailField(
        required=False,
        widget=forms.EmailInput(attrs={"placeholder": "name@example.com"}),
        label="Имейл (фактура)",
    )
    billing_phone = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "+359 ..."}),
        label="Телефон (фактура)",
    )
    billing_city = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "Град "}),
        label="Град (фактура)",
    )
    billing_street = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "ул. / бул., №, вх., ет., ап. "}),
        label="Адрес (фактура)",
    )
    billing_postcode = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "Пощ. код "}),
        label="Пощ. код (фактура)",
    )
    ship_same_as_billing = forms.BooleanField(
        required=False,
        initial=True,
        label="Използвай същия адрес за доставка",
    )

    class Meta:
        model = Order
        # Public contact + qty + billing (delivery fields collected on next step)
        fields = [
            "full_name", "email", "phone", "quantity",
            "billing_full_name", "billing_email", "billing_phone",
            "billing_city", "billing_street", "billing_postcode",
            "ship_same_as_billing",
        ]
        widgets = {
            "full_name": forms.TextInput(attrs={"placeholder": "Име и фамилия"}),
            "email": forms.EmailInput(attrs={"placeholder": "name@example.com"}),
            "phone": forms.TextInput(attrs={"placeholder": "+359 ..."}),

            # Kept for compatibility where referenced in templates; NOT in fields above:
            "delivery_method": forms.Select(attrs={"class": "select"}),
            "courier": forms.Select(attrs={"class": "select"}),
            "address_line": forms.TextInput(attrs={"placeholder": "ул. / бул., №, вх., ет., ап."}),
            "city": forms.TextInput(attrs={"placeholder": "Град"}),
            "postal_code": forms.TextInput(attrs={"placeholder": "Пощ. код"}),
            "office_text": forms.TextInput(attrs={"placeholder": "Офис/АПС код или адрес"}),
        }

    # --- Validators you had, kept the same ---
    def clean_phone(self):
        v = self.cleaned_data.get("phone", "")
        phone_validator(v)
        return v

    def clean(self):
        """
        We don't validate the delivery address here (done on the Econt pages).
        If 'same as billing' is ticked, require the essential billing bits.
        """
        cleaned = super().clean()

        if cleaned.get("ship_same_as_billing"):
            missing = []
            if not cleaned.get("billing_full_name"):  missing.append("Име и фамилия (фактура)")
            if not cleaned.get("billing_email"):      missing.append("Имейл (фактура)")
            if not cleaned.get("billing_city"):       missing.append("Град (фактура)")
            if not cleaned.get("billing_street"):     missing.append("Адрес (фактура)")
            if missing:
                raise forms.ValidationError("Моля, попълнете: " + ", ".join(missing))

        bp = cleaned.get("billing_postcode") or ""
        if bp:
            try:
                postcode_validator(bp)
            except Exception:
                self.add_error("billing_postcode", "Невалиден пощенски код.")

        return cleaned


class PaymentMethodForm(forms.ModelForm):
    class Meta:
        model = Order
        fields = ["payment_method"]
        widgets = {
            "payment_method": forms.RadioSelect(choices=PaymentMethod.choices)
        }
        labels = {"payment_method": ""}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        f = self.fields["payment_method"]
        f.required = True
        f.choices = [(v, l) for (v, l) in f.choices if v]  # drop the empty one
        f.choices = [
            (PaymentMethod.CARD, "Плащане с карта"),
            (PaymentMethod.COD, "Наложен платеж"),
        ]

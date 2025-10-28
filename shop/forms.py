from django import forms
from django.core.validators import RegexValidator
from .models import Order, DeliveryMethod, Courier, PaymentMethod

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
        widget=forms.NumberInput(attrs={"class": "qty-input", "inputmode": "numeric"})
    )

    # NEW — billing (invoice) section + toggle
    billing_full_name = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "Име и фамилия (за фактура)"}),
        label="Име и фамилия (фактура)"
    )
    billing_email = forms.EmailField(
        required=False,
        widget=forms.EmailInput(attrs={"placeholder": "name@example.com"}),
        label="Имейл (фактура)"
    )
    billing_phone = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "+359 ..."}),
        label="Телефон (фактура)"
    )
    billing_city = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "Град (фактура)"}),
        label="Град (фактура)"
    )
    billing_street = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "ул. / бул., №, вх., ет., ап. (фактура)"}),
        label="Адрес (фактура)"
    )
    billing_postcode = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "Пощ. код (фактура)"}),
        label="Пощ. код (фактура)"
    )
    ship_same_as_billing = forms.BooleanField(
        required=False,
        initial=True,
        label="Използвай същия адрес за доставка"
    )

    class Meta:
        model = Order
        # ⚠️ Minimal change: keep your original public contact fields + quantity.
        # Delivery/office details are still handled on the next step (address/office pages).
        fields = ["full_name", "email", "phone", "quantity",
                  # Include the new billing + toggle in the form binding
                  "billing_full_name", "billing_email", "billing_phone",
                  "billing_city", "billing_street", "billing_postcode",
                  "ship_same_as_billing",
                  ]
        widgets = {
            "full_name": forms.TextInput(attrs={"placeholder": "Име и фамилия"}),
            "email": forms.EmailInput(attrs={"placeholder": "name@example.com"}),
            "phone": forms.TextInput(attrs={"placeholder": "+359 ..."}),

            # You can keep these widget specs if you still use them elsewhere in templates,
            # but note they are NOT in `fields` (delivery details are collected later):
            "delivery_method": forms.Select(attrs={"class": "select"}),
            "courier": forms.Select(attrs={"class": "select"}),
            "address_line": forms.TextInput(attrs={"placeholder": "ул. / бул., №, вх., ет., ап."}),
            "city": forms.TextInput(attrs={"placeholder": "Град"}),
            "postal_code": forms.TextInput(attrs={"placeholder": "Пощ. код"}),
            "office_text": forms.TextInput(attrs={"placeholder": "Офис/АПС код или адрес"}),
        }

    # --- Validators you already had, kept safe/minimal ---

    def clean_phone(self):
        v = self.cleaned_data.get("phone", "")
        # keep your existing validator
        phone_validator(v)
        return v

    def clean(self):
        """
        Minimal changes:
        - DO NOT force delivery address validation here anymore (it happens on the next step).
        - If user ticks 'ship_same_as_billing', ensure billing basics are present.
        """
        cleaned = super().clean()

        # Only validate billing fields if the toggle is True (or if you want them always required, flip logic)
        if cleaned.get("ship_same_as_billing"):
            bf = cleaned.get("billing_full_name")
            be = cleaned.get("billing_email")
            bc = cleaned.get("billing_city")
            bs = cleaned.get("billing_street")
            # keep it light: require the essential bits for an invoice
            missing = []
            if not bf: missing.append("Име и фамилия (фактура)")
            if not be: missing.append("Имейл (фактура)")
            if not bc: missing.append("Град (фактура)")
            if not bs: missing.append("Адрес (фактура)")
            if missing:
                raise forms.ValidationError("Моля, попълнете: " + ", ".join(missing))

        # If you still want to keep postal code format validation for billing:
        bp = cleaned.get("billing_postcode") or ""
        if bp:
            try:
                postcode_validator(bp)
            except Exception:
                # soften failure: turn it into a form error instead of raising
                self.add_error("billing_postcode", "Невалиден пощенски код.")

        # ❗️Old delivery-step validation removed:
        # - We no longer enforce delivery_method/address_line/city/postal_code/office_text here,
        #   because those are collected and validated on econt address/office pages.

        return cleaned


from django import forms
from .models import Order, PaymentMethod


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
        # Robustly remove any empty ("") choice even if model has blank=True
        f.choices = [(v, l) for (v, l) in f.choices if v]  # drop the empty one
        # Optional: if you want to show only Card + COD:
        f.choices = [
            (PaymentMethod.CARD, "Плащане с карта"),
            (PaymentMethod.COD, "Наложен платеж"),
        ]

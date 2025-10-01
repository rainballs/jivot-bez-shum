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
    quantity = forms.IntegerField(
        min_value=1,
        initial=1,
        widget=forms.NumberInput(attrs={"class": "qty-input", "inputmode": "numeric"})
    )

    class Meta:
        model = Order
        fields = [
            "full_name", "email", "phone",
            "delivery_method", "courier",
            "address_line", "city", "postal_code",
            "office_text",
        ]
        widgets = {
            "full_name": forms.TextInput(attrs={"placeholder": "Име и фамилия"}),
            "email": forms.EmailInput(attrs={"placeholder": "name@example.com"}),
            "phone": forms.TextInput(attrs={"placeholder": "+359 ..."}),
            "delivery_method": forms.Select(attrs={"class": "select"}),
            "courier": forms.Select(attrs={"class": "select"}),
            "address_line": forms.TextInput(attrs={"placeholder": "ул. / бул., №, вх., ет., ап."}),
            "city": forms.TextInput(attrs={"placeholder": "Град"}),
            "postal_code": forms.TextInput(attrs={"placeholder": "Пощ. код"}),
            "office_text": forms.TextInput(attrs={"placeholder": "Офис/АПС код или адрес"}),
        }

    def clean_phone(self):
        v = self.cleaned_data.get("phone", "")
        phone_validator(v)
        return v

    def clean_postal_code(self):
        v = self.cleaned_data.get("postal_code", "")
        # only validate when delivery is to address
        if self.cleaned_data.get("delivery_method") == DeliveryMethod.TO_ADDRESS:
            postcode_validator(v)
        return v

    def clean(self):
        cleaned = super().clean()
        dmethod = cleaned.get("delivery_method")
        if dmethod == DeliveryMethod.TO_ADDRESS:
            if not (cleaned.get("address_line") and cleaned.get("city") and cleaned.get("postal_code")):
                raise forms.ValidationError("Моля, въведете адрес, град и пощенски код.")
        elif dmethod == DeliveryMethod.TO_OFFICE:
            if not cleaned.get("office_text"):
                raise forms.ValidationError("Моля, посочете офис/АПС.")
        return cleaned


class PaymentMethodForm(forms.ModelForm):
    # force required -> no empty "---------" choice
    payment_method = forms.ChoiceField(
        choices=PaymentMethod.choices,
        widget=forms.RadioSelect,
        required=True,
    )

    class Meta:
        model = Order
        fields = ["payment_method"]

# shop/econt_service.py
import base64
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
import requests

import json
import requests
from django.conf import settings
from .econt_client import EcontClient, EcontError, build_create_label_json, build_packing_list_from_order
from .models import PaymentMethod, _bgn_to_eur
from typing import List, Dict

BGN_PER_EUR = Decimal("1.95583")


def _q2(x: Decimal) -> Decimal:
    return Decimal(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _money_to_eur_bgn(amount: Decimal, currency: str) -> tuple[Decimal, Decimal]:
    """
    Convert amount in given currency to (eur, bgn).
    Handles EUR/BGN. If unknown -> treat as BGN (safe fallback).
    """
    cur = (currency or "").strip().upper()

    amount = _q2(Decimal(str(amount)))

    if cur == "EUR":
        eur = amount
        bgn = _q2(eur * BGN_PER_EUR)
        return eur, bgn

    if cur == "BGN" or cur == "":
        bgn = amount
        eur = _q2(bgn / BGN_PER_EUR)
        return eur, bgn

    # fallback: treat as BGN to avoid "too low" EUR
    bgn = amount
    eur = _q2(bgn / BGN_PER_EUR)
    return eur, bgn


def create_econt_label(order, overrides: dict | None = None) -> dict:
    overrides = overrides or {}

    # 🚨 HARD GUARD: вече имаме товарителница → не създаваме нова
    if getattr(order, "econt_shipment_num", None):
        return {
            "ok": True,
            "shipment_num": order.econt_shipment_num,
            "saved_pdf": bool(getattr(order, "econt_label_pdf", None)),
            "error": None,
        }

    d = settings.ECONT["DEFAULTS"]

    # --- receiver data (с fallback към order.* полетата) ---
    receiver_name = (
            order.full_name
            or f"{getattr(order, 'first_name', '')} {getattr(order, 'last_name', '')}".strip()
    )
    receiver_phone = order.phone or ""
    receiver_city = order.city or order.billing_city or ""

    # office code – override -> order
    receiver_office_code = (
            overrides.get("receiver_office_code")
            or (order.econt_office_code or "")
    )

    # адрес за ДО АДРЕС – override -> order.address_line/billing_street
    raw_street = (
            overrides.get("receiver_street")
            or (order.address_line or "")
            or (getattr(order, "billing_street", "") or "")
    )
    r_street = (raw_street or "").strip()

    r_num = overrides.get("receiver_num")

    # пощенски код – override -> order.postal_code -> billing_postcode / billing_postal_code
    r_postcode = (
            overrides.get("receiver_postcode")
            or (order.postal_code or "")
            or (getattr(order, "billing_postcode", "") or "")
            or (getattr(order, "billing_postal_code", "") or "")
    )
    r_postcode = (r_postcode or "").strip()

    r_entrance = overrides.get("receiver_entrance")
    r_floor = overrides.get("receiver_floor")
    r_apartment = overrides.get("receiver_apartment")

    # 🔴 минимална валидация
    errors: list[str] = []

    if not (receiver_name or "").strip():
        errors.append("липсва име на получателя")
    if not (receiver_phone or "").strip():
        errors.append("липсва телефон на получателя")
    if not (receiver_city or "").strip():
        errors.append("липсва град за доставка")

    to_office = bool(receiver_office_code)

    if to_office:
        if not (receiver_office_code or "").strip():
            errors.append("липсва избран офис на Еконт")
    else:
        if not r_street:
            errors.append("липсва улица за доставка до адрес")
        if not r_postcode:
            errors.append("липсва пощенски код за доставка до адрес")

    if errors:
        err = " / ".join(errors)
        order.econt_errors = err
        order.save(update_fields=["econt_errors"])
        return {
            "ok": False,
            "shipment_num": None,
            "saved_pdf": False,
            "error": err,
        }

    # --- пари ---
    subtotal = Decimal(str(order.subtotal_bgn or "0")).quantize(Decimal("0.01"))
    pm = getattr(order, "payment_method", None)
    is_cod = (
            pm == PaymentMethod.COD
            or (isinstance(pm, str) and str(pm).lower() == "cod")
    )

    payer: str
    cod_bgn: float
    cod_agreement: str | None = None
    invoice_num: str | None = None

    if is_cod:
        payer = "RECEIVER"  # клиентът плаща доставка + COD такса
        cod_bgn = float(subtotal)  # НП = стойност на книгите

        # напр. "CD250332" – взимаме от settings, ако го има
        cod_agreement = d.get("cod_agreement_number") or "CD250332"

        # фактура / опис – примерно "{order_id} DD.MM.YY"
        invoice_num = f"{order.pk} {date.today().strftime('%d.%m.%y')}"
    else:
        payer = "SENDER"  # при карта ти плащаш доставката
        cod_bgn = 0.0

    declared_value_bgn = float(subtotal)

    # --- данни за Бърз опис / packingList ---
    # При теб е 1 продукт (книга) → 1 ред
    qty = getattr(order, "quantity", None) or 1
    total_weight_kg = float(getattr(order, "total_weight_kg", 0.800) or 0.800)
    # единична цена = subtotal / qty
    try:
        unit_price = float((subtotal / Decimal(qty)).quantize(Decimal("0.01")))
    except Exception:
        unit_price = float(subtotal)
    packing_list = build_packing_list_from_order(order)

    payload = build_create_label_json(
        sender_name=d["sender_name"],
        sender_phone=d["sender_phone"],
        sender_city=d["sender_city"],
        sender_address=d["sender_address"],
        sender_office_code=(d.get("sender_office") or None),
        receiver_name=receiver_name,
        receiver_phone=receiver_phone,
        receiver_city=receiver_city,
        receiver_office_code=receiver_office_code or None,
        receiver_street=r_street,
        receiver_num=r_num,
        receiver_postcode=r_postcode,
        receiver_entrance=r_entrance,
        receiver_floor=r_floor,
        receiver_apartment=r_apartment,
        weight_kg=total_weight_kg,
        parcels=1,
        cod_bgn=cod_bgn,
        declared_value_bgn=declared_value_bgn,
        payer=payer,  # ⬅️ кой плаща доставката
        label_format=d.get("label_format", "10x9"),
        cod_agreement_number=cod_agreement,
        invoice_num=invoice_num,
        sms_notification=True,  # винаги включен SMS
        # packing_inventory_num="1",  # № артикул
        # packing_description="Живот без шум - книга",
        # packing_weight_kg=total_weight_kg,
        # packing_count=qty,
        # packing_price_bgn=unit_price,
        packing_list=packing_list,  # ✅ add this
        packing_list_type="digital",  # ✅ add this (or omit, default is "digital")
    )

    print(
        "ECONT ▶ OUTGOING JSON:\n",
        json.dumps({"mode": "create", "label": payload}, ensure_ascii=False, indent=2),
    )

    client = EcontClient()
    try:
        res = client.create_label(payload)
    except EcontError as e:
        err = f"Econt error: {e}"
        order.econt_errors = err
        order.save(update_fields=["econt_errors"])
        return {"ok": False, "shipment_num": None, "saved_pdf": False, "error": err}
    except Exception as e:
        err = f"Econt transport error: {e}"
        order.econt_errors = err
        order.save(update_fields=["econt_errors"])
        return {"ok": False, "shipment_num": None, "saved_pdf": False, "error": err}

    shipment_num = (
            res.get("shipmentNumber")
            or res.get("num")
            or res.get("shipment_num")
    )
    pdf_bytes = None
    if "pdfBase64" in res:
        try:
            pdf_bytes = base64.b64decode(res["pdfBase64"])
        except Exception:
            pdf_bytes = None

    saved_pdf = False
    with transaction.atomic():
        if shipment_num:
            order.econt_shipment_num = shipment_num
        order.econt_errors = None
        order.save(update_fields=["econt_shipment_num", "econt_errors"])

        if pdf_bytes:
            fname = f"econt_label_{order.pk}_{shipment_num or 'unknown'}.pdf"
            order.econt_label_pdf.save(fname, ContentFile(pdf_bytes), save=True)
            saved_pdf = True

    return {
        "ok": True,
        "shipment_num": shipment_num,
        "saved_pdf": saved_pdf,
        "error": None,
    }


def calculate_econt_shipping(order, overrides: dict | None = None, include_cod: bool = True) -> dict:
    """
    Ask Econt in 'calculate' mode for shipping price.

    - If include_cod=True and order is COD, shipping price includes:
        C (куриерска услуга) + OC (обявена стойност) + CD (такса наложен платеж).
    - If include_cod=False, COD е 0 и получаваш само доставка + обявена стойност.
    """
    overrides = overrides or {}
    d = settings.ECONT["DEFAULTS"]

    receiver_name = (
            order.full_name
            or f"{getattr(order, 'first_name', '')} {getattr(order, 'last_name', '')}".strip()
    )
    receiver_phone = order.phone or ""
    receiver_city = order.city or ""

    receiver_office_code = (order.econt_office_code or "") or overrides.get("receiver_office_code")

    r_street = overrides.get("receiver_street")
    r_num = overrides.get("receiver_num")
    r_postcode = overrides.get("receiver_postcode")
    r_entrance = overrides.get("receiver_entrance")
    r_floor = overrides.get("receiver_floor")
    r_apartment = overrides.get("receiver_apartment")

    # --- минимална валидация ---
    errors: list[str] = []
    if not (receiver_name or "").strip():
        errors.append("липсва име на получателя")
    if not (receiver_phone or "").strip():
        errors.append("липсва телефон на получателя")
    if not (receiver_city or "").strip():
        errors.append("липсва град за доставка")

    to_office = bool(receiver_office_code)
    if to_office:
        if not (receiver_office_code or "").strip():
            errors.append("липсва избран офис на Еконт")
    else:
        if not (r_street or "").strip():
            errors.append("липсва улица за доставка до адрес")
        if not (r_postcode or "").strip():
            errors.append("липсва пощенски код за доставка до адрес")

    if errors:
        err = " / ".join(errors)
        order.econt_errors = err
        order.save(update_fields=["econt_errors"])
        return {"ok": False, "error": err}

    # --- money: subtotal for goods (без доставка) ---
    subtotal = Decimal(str(order.subtotal_bgn or "0")).quantize(Decimal("0.01"))

    pm = getattr(order, "payment_method", None)
    is_cod = (pm == PaymentMethod.COD) or (isinstance(pm, str) and str(pm).lower() == "cod")

    if is_cod and include_cod:
        cod_bgn = float(subtotal)  # НП = стойност на стоката
        payer = "RECEIVER"
        # 👇 същият COD договор, както при create_econt_label
        cod_agreement = d.get("cod_agreement_number") or "CD250332"
    else:
        cod_bgn = 0.0
        payer = "SENDER"
        cod_agreement = None

    declared_value_bgn = float(subtotal)
    packing_list = build_packing_list_from_order(order)
    payload = build_create_label_json(
        sender_name=d["sender_name"],
        sender_phone=d["sender_phone"],
        sender_city=d["sender_city"],
        sender_address=d["sender_address"],
        sender_office_code=(d.get("sender_office") or None),
        receiver_name=receiver_name,
        receiver_phone=receiver_phone,
        receiver_city=receiver_city,
        receiver_office_code=receiver_office_code or None,
        receiver_street=r_street,
        receiver_num=r_num,
        receiver_postcode=r_postcode,
        receiver_entrance=r_entrance,
        receiver_floor=r_floor,
        receiver_apartment=r_apartment,
        weight_kg=float(getattr(order, "total_weight_kg", 0.800) or 0.800),
        parcels=1,
        cod_bgn=cod_bgn,
        declared_value_bgn=declared_value_bgn,
        payer=payer,
        label_format=d.get("label_format", "10x9"),
        cod_agreement_number=cod_agreement,  # 👉 вече влиза и при calculate
        sms_notification=True,
        # invoice_num можеш да пропуснеш тук – не е нужно за калкулацията
        packing_list=packing_list,  # ✅ add this
        packing_list_type="digital",  # ✅ add this (or omit, default is "digital")
    )

    print(
        "ECONT ▶ CALCULATE OUTGOING JSON:\n",
        json.dumps({"mode": "calculate", "label": payload}, ensure_ascii=False, indent=2),
    )

    client = EcontClient()
    try:
        label = client.calculate_label(payload)  # очаквам да връща dict-а под "label"
    except EcontError as e:
        err = f"Econt calculate error: {e}"
        order.econt_errors = err
        order.save(update_fields=["econt_errors"])
        return {"ok": False, "error": err}
    except Exception as e:
        err = f"Econt calculate transport error: {e}"
        order.econt_errors = err
        order.save(update_fields=["econt_errors"])
        return {"ok": False, "error": err}

    # --- extract shipping price + currency (Econt may now return EUR) ---
    services_list = label.get("services") or []
    total_dec: Decimal | None = None

    if isinstance(services_list, list) and services_list:
        total_dec = sum(Decimal(str(s.get("price", 0) or 0)) for s in services_list)

    if total_dec is None:
        total_raw = label.get("totalPrice")
        if total_raw is not None:
            total_dec = Decimal(str(total_raw))

    if total_dec is None:
        err = "Еконт не върна цена на доставка (виж CALCULATE логовете)."
        order.econt_errors = err
        order.save(update_fields=["econt_errors"])
        return {"ok": False, "error": err}

    # ✅ currency from label/services (Econt returns label.currency)
    currency = (label.get("currency") or "").strip()
    if not currency and isinstance(services_list, list) and services_list:
        currency = (services_list[0].get("currency") or "").strip()

    ship_eur, ship_bgn = _money_to_eur_bgn(total_dec, currency)

    return {
        "ok": True,
        "ship_eur": ship_eur,
        "ship_bgn": ship_bgn,
        "currency": currency or None,
        "raw": label,
    }


def search_offices(city: str, query: str = "") -> list[dict]:
    """
    Return [{"code":"1501","name":"Бургас Роял","address":"ул. ...", "city":"Бургас"}...]
    Implement using Econt's 'getOffices' / nomenclatures API.
    """
    # TODO: replace with real Econt call. Example structure only.
    # resp = requests.post(ECONT_URL, json=payload, timeout=15)
    # data = resp.json()
    # return [{"code": o["officeCode"], "name": o["name"], "address": o["address"]["full"], "city": o["city"]["name"]} for o in data["offices"]]

    raise NotImplementedError("Hook me up to Econt getOffices")


def _nomenclatures_url(method: str) -> str:
    base = getattr(settings, "ECONT_NOMENCLATURES_BASE", "https://ee.econt.com/services/Nomenclatures/")
    if not base.endswith("/"):
        base += "/"
    return f"{base}NomenclaturesService.{method}.json"


def _auth():
    user = getattr(settings, "ECONT_USERNAME", None)
    pwd = getattr(settings, "ECONT_PASSWORD", None)
    return (user, pwd) if user and pwd else None


def _post_json(url: str, payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False)
    r = requests.post(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        timeout=20,
        auth=_auth(),
    )
    r.raise_for_status()
    return r.json()


def get_cities(country_code: str = "BGR", name_query: str = "") -> list[dict]:
    """
    Calls getCities. Returns compact list:
    [{"id": 47, "name": "Шумен", "nameEn": "...", "postCode": "9700"}]
    """
    url = _nomenclatures_url("getCities")

    # Econt accepts various filters; keeping it minimal + per docs.
    payload = {"countryCode": country_code}
    # Some Econt versions accept "name" or "nameLike". If your instance
    # needs a different key, switch it here:
    if name_query:
        payload["name"] = name_query

    data = _post_json(url, payload)
    cities = []
    for c in data.get("cities", []):
        cities.append({
            "id": c.get("id"),
            "name": c.get("name"),
            "nameEn": c.get("nameEn"),
            "postCode": c.get("postCode"),
        })
    return cities


def get_offices_by_city_id(city_id: int, country_code: str = "BGR") -> List[Dict]:
    """
    Calls getOffices with {"countryCode":"BGR", "cityID": <int>}.

    We EXCLUDE APS/MPS because they don't support COD and Econt returns 517
    if you try to create a COD shipment to them.

    Returns compact list:
    [{"code":"9709","name":"Шумен","address":"Шумен бул. Мадара №1"}, ...]
    """
    url = _nomenclatures_url("getOffices")
    payload = {"countryCode": country_code, "cityID": int(city_id)}
    data = _post_json(url, payload)

    offices: List[Dict] = []
    for o in data.get("offices", []):
        # raw flags from Econt
        is_aps = o.get("isAPS")
        is_mps = o.get("isMPS")

        # 1) skip lockers / machines
        if is_aps or is_mps:
            continue

        addr = o.get("address") or {}
        parts = [
            addr.get("city", {}).get("name") or "",
            addr.get("quarter") or "",
            addr.get("street") or "",
            f"№{addr.get('num')}" if addr.get("num") else "",
            addr.get("other") or "",
        ]
        pretty = " ".join(p for p in parts if p).strip()

        offices.append({
            "code": o.get("code"),
            "name": o.get("name"),
            "address": pretty,
            # keep flags just in case you want them in frontend later
            "isAPS": is_aps,
            "isMPS": is_mps,
        })

    return offices


def apply_econt_calculated_price(order, label_block: dict) -> None:
    """
    Взима 'label' от отговора при mode='calculate' и записва:
      - order.shipping_bgn / shipping_eur
      - order.total_bgn / total_eur
    """
    total_price = label_block.get("totalPrice")
    if total_price is None:
        return

    price_bgn = Decimal(str(total_price))
    order.shipping_bgn = price_bgn
    order.shipping_eur = _bgn_to_eur(price_bgn)

    # ако вече имаме subtotal_* от OrderItem
    order.total_bgn = (order.subtotal_bgn or Decimal("0")) + order.shipping_bgn
    order.total_eur = (order.subtotal_eur or Decimal("0")) + order.shipping_eur

    order.save(update_fields=["shipping_bgn", "shipping_eur", "total_bgn", "total_eur"])

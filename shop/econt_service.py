# shop/econt_service.py
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
import requests

import json
import requests
from django.conf import settings
from .econt_client import EcontClient, EcontError, build_create_label_json
from .models import PaymentMethod
import base64


def create_econt_label(order, overrides: dict | None = None) -> dict:
    """
    Creates an Econt label (JSON API) using settings.ECONT.

    Rules:
    - if order.paid == True AND payment_method != COD → cdAmount = 0.00, payer = SENDER
    - else (not paid OR payment_method == COD) → cdAmount = order.total_bgn, payer = RECEIVER
    """
    overrides = overrides or {}
    d = settings.ECONT["DEFAULTS"]

    # 1) receiver data
    receiver_name = (
            getattr(order, "full_name", "") or
            f"{getattr(order, 'first_name', '')} {getattr(order, 'last_name', '')}".strip()
    )
    receiver_phone = getattr(order, "phone", "")
    receiver_city = getattr(order, "city", "")

    receiver_office_code = getattr(order, "econt_office_code", "") or overrides.get("receiver_office_code")

    # structured address (for toDoor)
    r_street = overrides.get("receiver_street")
    r_num = overrides.get("receiver_num")
    r_postcode = overrides.get("receiver_postcode")
    r_entrance = overrides.get("receiver_entrance")
    r_floor = overrides.get("receiver_floor")
    r_apartment = overrides.get("receiver_apartment")

    # 2) payment / COD decision
    total_bgn = float(getattr(order, "total_bgn", 0.0) or 0.0)

    is_paid = bool(getattr(order, "paid", False))
    pm = getattr(order, "payment_method", None)
    is_cod_payment = (pm == PaymentMethod.COD)

    if is_paid and not is_cod_payment:
        # CARD / STRIPE – already paid
        cod_bgn = 0.0
        payer = "sender"  # you (merchant) pay the courier
    else:
        # Cash on delivery OR not marked as paid
        cod_bgn = total_bgn
        payer = "receiver"  # customer pays courier and COD

    declared_bgn = total_bgn
    weight_kg = float(getattr(order, "total_weight_kg", 0.800) or 0.800)

    # 3) build payload
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

        weight_kg=weight_kg,
        parcels=1,
        cod_bgn=cod_bgn,
        declared_value_bgn=declared_bgn,
        payer=payer,
        label_format=d.get("label_format", "10x9"),
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

    # 4) success → save shipment num + pdf
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


def get_offices_by_city_id(city_id: int, country_code: str = "BGR") -> list[dict]:
    """
    Calls getOffices with {"countryCode":"BGR", "cityID": <int>}.
    Returns compact list:
    [{"code":"9709","name":"Шумен","address":"Шумен бул. Мадара №1"}, ...]
    """
    url = _nomenclatures_url("getOffices")
    payload = {"countryCode": country_code, "cityID": int(city_id)}
    data = _post_json(url, payload)

    offices = []
    for o in data.get("offices", []):
        addr = o.get("address") or {}
        # Build a human readable one-liner address
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
        })
    return offices

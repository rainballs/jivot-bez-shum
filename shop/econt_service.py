# shop/econt_service.py
import base64

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
import requests

import json
import requests
from django.conf import settings
from .econt_client import EcontClient, EcontError, build_create_label_json
from .models import PaymentMethod
from typing import List, Dict


def create_econt_label(order, overrides: dict | None = None) -> dict:
    overrides = overrides or {}

    # 🚨 HARD GUARD: if this order already has a shipment number,
    # DO NOT create a new label in Econt.
    if getattr(order, "econt_shipment_num", None):
        return {
            "ok": True,
            "shipment_num": order.econt_shipment_num,
            "saved_pdf": bool(getattr(order, "econt_label_pdf", None)),
            "error": None,
        }

    d = settings.ECONT["DEFAULTS"]

    # --- receiver data ---
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

    # 🔴 NEW: minimal server-side validation – do NOT call Econt with empty data
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
        return {
            "ok": False,
            "shipment_num": None,
            "saved_pdf": False,
            "error": err,
        }

    # --- money ---
    total_bgn = float(order.total_bgn or 0.0)
    pm = getattr(order, "payment_method", None)
    is_cod = (pm == PaymentMethod.COD) or (isinstance(pm, str) and pm.lower() == "cod")
    cod_bgn = total_bgn if is_cod else 0.0

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
        cod_bgn=cod_bgn,  # COD only if COD chosen
        declared_value_bgn=total_bgn,  # always show product value
        payer="sender",
        label_format=d.get("label_format", "10x9"),
    )

    print("ECONT ▶ OUTGOING JSON:\n", json.dumps({"mode": "create", "label": payload}, ensure_ascii=False, indent=2))

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

    shipment_num = res.get("shipmentNumber") or res.get("num") or res.get("shipment_num")
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

    return {"ok": True, "shipment_num": shipment_num, "saved_pdf": saved_pdf, "error": None}


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

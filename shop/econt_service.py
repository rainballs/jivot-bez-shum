# shop/econt_service.py
"""Price quoting / address validation and Econt nomenclatures. Shipment CREATION lives in fulfillment.py."""
import json
import logging
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List

import requests
from django.conf import settings

from .econt_client import (
    EcontClient,
    EcontError,
    EcontNotSent,
    EcontOutcomeUnknown,
    EcontRejected,
)
from .fulfillment import build_label_for_order, plan_for_order

log = logging.getLogger("econt")

BGN_PER_EUR = Decimal("1.95583")  # only used if Econt ever answers in BGN


def _q2(x: Decimal) -> Decimal:
    return Decimal(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _to_eur(amount: Decimal, currency: str) -> Decimal:
    """Econt answers in EUR. A BGN answer (legacy accounts) is converted at the fixed rate."""
    cur = (currency or "").strip().upper()
    amount = _q2(Decimal(str(amount)))
    if cur in ("BGN", "ЛВ", "ЛВ."):
        return _q2(amount / BGN_PER_EUR)
    return amount


def quote_shipping(order, validate_first: bool = True) -> dict:
    """
    Validate the address with Econt (mode=validate: checks street/number/postcode/office, unlike mode=calculate)
    and ask Econt for the delivery price. Uses the SAME payload builder as shipment creation and the payment the
    customer selected, but never creates anything.

    Returns {"ok": True, "ship_eur", "currency"} or {"ok": False, "error": <customer-safe text>,
    "retryable": bool}.
    """
    try:
        plan = plan_for_order(order, for_create=False)
        payload = build_label_for_order(order, plan)
    except ValueError as e:
        return {"ok": False, "error": f"Непълни данни за доставка: {e}", "retryable": False}
    except Exception as e:  # NotFulfillable etc.
        return {"ok": False, "error": str(e), "retryable": False}

    client = EcontClient()
    try:
        if validate_first and getattr(settings, "ECONT_VALIDATE_ADDRESS", True):
            client.validate_label(payload)
        label = client.calculate_label(payload)
    except EcontRejected as e:
        return {"ok": False, "error": f"Еконт: {e}", "retryable": False}
    except (EcontNotSent, EcontOutcomeUnknown, EcontError) as e:
        log.warning("econt quote unavailable for order %s: %s", order.pk, type(e).__name__)
        return {"ok": False, "error": "Еконт временно не отговаря. Опитайте отново след малко.", "retryable": True}

    services_list = label.get("services") or []
    total_dec = None
    if isinstance(services_list, list) and services_list:
        total_dec = sum(Decimal(str(s.get("price", 0) or 0)) for s in services_list)
    if total_dec is None and label.get("totalPrice") is not None:
        total_dec = Decimal(str(label["totalPrice"]))
    if total_dec is None or total_dec <= 0:
        return {"ok": False, "error": "Еконт не върна цена на доставка.", "retryable": True}

    currency = (label.get("currency") or "").strip()
    if not currency and isinstance(services_list, list) and services_list:
        currency = (services_list[0].get("currency") or "").strip()

    return {"ok": True, "ship_eur": _to_eur(total_dec, currency), "currency": currency or None}


# ----------------------------------------------------------------------------- nomenclatures
def _nomenclatures_url(method: str) -> str:
    base = getattr(settings, "ECONT_NOMENCLATURES_BASE", "https://ee.econt.com/services/Nomenclatures/")
    if not base.endswith("/"):
        base += "/"
    return f"{base}NomenclaturesService.{method}.json"


def _post_json(url: str, payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False)
    r = requests.post(
        url,
        data=body.encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        timeout=(5, 20),
    )
    r.raise_for_status()
    return r.json()


def get_cities(country_code: str = "BGR", name_query: str = "") -> list[dict]:
    """[{"id": 47, "name": "Шумен", "nameEn": "...", "postCode": "9700"}]"""
    payload = {"countryCode": country_code}
    if name_query:
        payload["name"] = name_query

    data = _post_json(_nomenclatures_url("getCities"), payload)
    return [
        {"id": c.get("id"), "name": c.get("name"), "nameEn": c.get("nameEn"), "postCode": c.get("postCode")}
        for c in data.get("cities", [])
    ]


def get_offices_by_city_id(city_id: int, country_code: str = "BGR") -> List[Dict]:
    """
    Offices for a city. APS/MPS (lockers/machines) are excluded: they do not support COD and Econt
    answers with error 517 if a COD shipment is created for them.
    """
    data = _post_json(_nomenclatures_url("getOffices"), {"countryCode": country_code, "cityID": int(city_id)})

    offices: List[Dict] = []
    for o in data.get("offices", []):
        is_aps = o.get("isAPS")
        is_mps = o.get("isMPS")
        if is_aps or is_mps:
            continue

        addr = o.get("address") or {}
        parts = [
            (addr.get("city") or {}).get("name") or "",
            addr.get("quarter") or "",
            addr.get("street") or "",
            f"№{addr.get('num')}" if addr.get("num") else "",
            addr.get("other") or "",
        ]
        offices.append({
            "code": o.get("code"),
            "name": o.get("name"),
            "address": " ".join(p for p in parts if p).strip(),
            "isAPS": is_aps,
            "isMPS": is_mps,
        })
    return offices

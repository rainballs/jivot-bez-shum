# shop/econt_client.py
from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Optional, Tuple
import base64

import requests
from django.conf import settings
from lxml import etree

from shop.models import Order

from datetime import date, timedelta

log = logging.getLogger("econt")


class EcontError(RuntimeError):
    pass


def _next_workday(d: date) -> date:
    wd = d.weekday()
    if wd >= 4:  # Fri–Sun → Monday
        return d + timedelta(days=7 - wd)
    return d + timedelta(days=1)


def _first_nonempty(*vals) -> Optional[str]:
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def receiver_phone_fmt(phone: str) -> str:
    """Normalize to +359XXXXXXXXX (best effort)."""
    if not phone:
        return ""
    d = "".join(ch for ch in phone if ch.isdigit())
    if d.startswith("359"):
        d = d[3:]
    elif d.startswith("0"):
        d = d[1:]
    return "+359" + d


class EcontClient:
    """
    Minimal JSON client for:
      {BASE_URL}/Shipments/LabelService.createLabel.json
    Example BASE_URL: https://demo.econt.com/ee/services
    """

    def __init__(self, timeout: int = 30):
        base = settings.ECONT["BASE_URL"].rstrip("/")
        self.create_label_url = f"{base}/Shipments/LabelService.createLabel.json"

        self.username = settings.ECONT.get("USER", "")
        self.password = settings.ECONT.get("PASS", "")
        self.timeout = timeout

        self.sess = requests.Session()
        self.sess.auth = (self.username, self.password)
        self.sess.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
        })

    def create_label(self, label_payload: dict) -> dict:
        """
        Call createLabel (JSON). Returns dict with Econt response's 'label' block (or the root).
        Raises EcontError with a readable message on failure.
        """
        body = {"mode": "create", "label": label_payload}
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")

        log.error("ECONT ▶ POST %s\n%s", self.create_label_url, data.decode("utf-8"))
        r = self.sess.post(self.create_label_url, data=data, timeout=self.timeout)
        log.error("ECONT ◀ %s %s\n%s", r.status_code, r.reason, r.text[:2000])

        if r.status_code != 200:
            raise EcontError(f"HTTP {r.status_code} {r.reason}")

        try:
            resp = r.json()
        except Exception:
            raise EcontError("Non-JSON response from Econt")

        # Econt may surface errors in several places; collect the first useful message.
        err = _first_nonempty(
            resp.get("error"),
            resp.get("message"),
            (resp.get("label") or {}).get("error"),
        )
        if not err:
            # sometimes it's an array like {'errors': [{'message': '...'}]}
            errs = resp.get("errors") or (resp.get("label") or {}).get("errors")
            if isinstance(errs, list) and errs:
                err = _first_nonempty(*(str(e.get("message") or e.get("text") or e) for e in errs))

        if err:
            raise EcontError(err)

        return resp.get("label") or resp

    def calculate_label(self, label_payload: dict) -> dict:
        """
        Call createLabel in 'calculate' mode.
        Returns the 'label' block (or root) similar to create_label.
        """
        body = {"mode": "calculate", "label": label_payload}
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        log.error("ECONT ▶ CALCULATE %s\n%s", self.create_label_url, data.decode("utf-8"))

        r = self.sess.post(self.create_label_url, data=data, timeout=self.timeout)
        log.error("ECONT ◀ CALCULATE %s %s\n%s", r.status_code, r.reason, r.text[:2000])

        if r.status_code != 200:
            raise EcontError(f"HTTP {r.status_code} {r.reason}")

        try:
            resp = r.json()
        except Exception:
            raise EcontError("Non-JSON response from Econt")

        # same error extraction as in create_label
        err = _first_nonempty(
            resp.get("error"),
            resp.get("message"),
            (resp.get("label") or {}).get("error"),
        )
        if not err:
            errs = resp.get("errors") or (resp.get("label") or {}).get("errors")
            if isinstance(errs, list) and errs:
                err = _first_nonempty(
                    *(str(e.get("message") or e.get("text") or e) for e in errs)
                )
        if err:
            raise EcontError(err)

        return resp.get("label") or resp

    def _post_json(self, url: str, payload: dict) -> dict:
        """POST JSON to Econt and return parsed JSON or raise EcontError."""
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        log.error("ECONT ▶ POST %s\n%s", url, data.decode("utf-8"))
        r = self.sess.post(url, data=data, timeout=self.timeout)
        log.error("ECONT ◀ %s %s\n%s", r.status_code, r.reason, r.text[:2000])

        if r.status_code != 200:
            raise EcontError(f"HTTP {r.status_code} {r.reason}")

        try:
            return r.json()
        except Exception:
            raise EcontError("Non-JSON response from Econt")

    def find_city(self, name: str, country_code: str = "BGR") -> dict | None:
        """Return the best city object for a human name like 'Бургас' or 'София'."""
        url = settings.ECONT["BASE_URL"].rstrip("/") + "/Nomenclatures/NomenclaturesService.getCities.json"
        resp = self._post_json(url, {"countryCode": country_code, "name": name})
        items = resp.get("cities") or resp.get("items") or []

        # 1) exact match
        exact = [c for c in items if c.get("name") == name]
        if exact:
            return exact[0]

        # 2) plain (no parentheses), contains name
        plain = [
            c for c in items
            if "(" not in (c.get("name") or "") and name in (c.get("name") or "")
        ]

        # prefer ones with expressCityDeliveries True (actual cities)
        plain_sorted = sorted(
            plain,
            key=lambda c: (not c.get("expressCityDeliveries", False), len(c.get("name", "")))
        )
        if plain_sorted:
            return plain_sorted[0]

        # 3) fallback: first item
        return items[0] if items else None


# def build_create_label_json(
#         *,
#         sender_name: str,
#         sender_phone: str,
#         sender_city: str,
#         sender_address: str | None,
#         sender_office_code: str | None,
#         receiver_name: str,
#         receiver_phone: str,
#         receiver_city: str,
#         receiver_office_code: str | None = None,
#         receiver_street: str | None = None,
#         receiver_num: str | None = None,
#         receiver_postcode: str | None = None,
#         receiver_entrance: str | None = None,
#         receiver_floor: str | None = None,
#         receiver_apartment: str | None = None,
#         weight_kg: float = 0.8,
#         parcels: int = 1,
#         cod_bgn: float = 0.0,
#         declared_value_bgn: float = 0.0,
#         payer: str = "receiver",
#         label_format: str = "10x9",
# ) -> dict:
#     if not Order.paid:
#         declared_value_bgn = Order.total_bgn
#
#     payload = {
#         "shipmentType": "PACK",  # ← REQUIRED for JSON API
#         "service": None,  # set below
#         "packCount": int(parcels),
#         "weight": float(weight_kg),
#         "shipmentDescription": "Книга",
#         "payer": (payer or "receiver").upper(),  # "RECEIVER" or "SENDER"
#         "declaredValue": float(declared_value_bgn),
#         "label": {"format": label_format},
#
#         "senderClient": {"name": sender_name, "phones": [sender_phone]},
#         "senderAgent": {"name": "Филип Стоянов", "phones": [sender_phone]},
#         "senderAddress": {
#             "city": {
#                 "country": {
#                     "code3": "BGR"
#                 },
#                 "name": sender_city,
#                 "postCode": "8000"
#             }
#         },
#         "receiverClient": {"name": receiver_name, "phones": [receiver_phone]},
#         "receiverAddress": {
#             "city": {
#                 "country": {
#                     "code3": "BGR"
#                 },
#                 "name": receiver_city,
#                 "postCode": receiver_postcode
#             },
#         },
#         "delivery": {"date": date, "timeIntervalId": 0}
#     }
#
#     # Sender: office OR address
#     if sender_office_code:
#         payload["senderOfficeCode"] = str(sender_office_code)
#     else:
#         # Econt accepts a free-form street when needed; if you have split fields, map them here.
#         payload["senderAddress"]["street"] = sender_address or ""
#
#     # Route: office vs door
#     if receiver_office_code:
#         payload["service"] = "toOffice"
#         payload["receiverOfficeCode"] = str(receiver_office_code)
#     else:
#         payload["service"] = "toDoor"
#         ra = payload["receiverAddress"]
#         ra["street"] = receiver_street or ""
#         if receiver_num:
#             ra["num"] = str(receiver_num)
#         if receiver_postcode:
#             ra["postCode"] = str(receiver_postcode)
#         if receiver_entrance:
#             ra["entrance"] = receiver_entrance
#         if receiver_floor:
#             ra["floor"] = receiver_floor
#         if receiver_apartment:
#             ra["apartment"] = receiver_apartment
#
#         delivery_day = _next_workday(date.today()).isoformat()
#         payload["delivery"] = {"date": delivery_day, "timeIntervalId": 0}
#     # COD (optional)
#     if cod_bgn and float(cod_bgn) > 0:
#         payload["cdAmount"] = float(cod_bgn)
#
#     return payload

def build_create_label_json(
        *,
        sender_name: str,
        sender_phone: str,
        sender_city: str,
        sender_address: str | None,
        sender_office_code: str | None,
        receiver_name: str,
        receiver_phone: str,
        receiver_city: str,
        receiver_office_code: str | None = None,
        receiver_street: str | None = None,
        receiver_num: str | None = None,
        receiver_postcode: str | None = None,
        receiver_entrance: str | None = None,
        receiver_floor: str | None = None,
        receiver_apartment: str | None = None,
        weight_kg: float = 0.8,
        parcels: int = 1,
        cod_bgn: float = 0.0,
        declared_value_bgn: float = 0.0,
        payer: str = "SENDER",  # default like in the other app
        label_format: str = "10x9",
        cod_agreement_number: str | None = None,
        invoice_num: str | None = None,  # 👈 фактура/опис
        sms_notification: bool = False,  # 👈 SMS известяване
        # # 👇 данни за „Бърз опис“ / packing list
        # packing_inventory_num: str | None = None,
        # packing_description: str | None = None,
        # packing_weight_kg: float | None = None,
        # packing_count: int | None = None,
        # packing_price_bgn: float | None = None,
        packing_list: list[dict] | None = None,
        packing_list_type: str | None = "digital",  # "digital" is what you want for “Бърз опис” entered as rows
) -> dict:
    """
    Build the JSON payload for Econt LabelService (create / calculate).

    - `payer`              → кой плаща куриерската услуга (SENDER / RECEIVER)
    - `cod_bgn`            → Наложен платеж (само при COD)
    - `declared_value_bgn` → Обявена стойност (стойност на стоката)
    - `packingList`        → ред/ове за бърз опис (инв. №, описание, тегло, бр., цена)
    """

    # --- normalize basics ---
    payer_upper = (payer or "SENDER").upper()
    if payer_upper not in ("SENDER", "RECEIVER"):
        payer_upper = "SENDER"

    cod_bgn = float(cod_bgn or 0.0)
    declared_value_bgn = float(declared_value_bgn or 0.0)

    # --- mandatory receiver fields ---
    rn = (receiver_name or "").strip()
    rp = (receiver_phone or "").strip()
    rc = (receiver_city or "").strip()

    if not rn or not rp or not rc:
        raise ValueError(
            "Missing mandatory receiver fields: "
            f"name='{rn}', phone='{rp}', city='{rc}'"
        )

    to_office = bool(receiver_office_code)

    if not to_office:
        rs = (receiver_street or "").strip()
        rpc = (receiver_postcode or "").strip()
        if not rs or not rpc:
            raise ValueError(
                "Missing address for toDoor shipment: "
                f"street='{rs}', postcode='{rpc}'"
            )

    # --- base label ---
    payload: dict = {
        "shipmentType": "PACK",
        "service": None,  # set below
        "packCount": int(parcels),
        "weight": float(weight_kg),
        "shipmentDescription": "Книга",
        "payer": payer_upper,  # това е „Платец“ в Еконт
        "declaredValue": declared_value_bgn,
        "label": {"format": label_format},

        "senderClient": {"name": sender_name, "phones": [sender_phone]},
        "senderAgent": {"name": sender_name, "phones": [sender_phone]},
        "senderAddress": {
            "city": {
                "country": {"code3": "BGR"},
                "name": sender_city,
                "postCode": "8000",  # твоя фиксиран пощ. код
            }
        },

        "receiverClient": {"name": rn, "phones": [rp]},
    }

    # sender: office vs address
    if sender_office_code:
        payload["senderOfficeCode"] = str(sender_office_code)
    else:
        payload["senderAddress"]["street"] = sender_address or ""

    # дата на изпращане (следващ работен ден)
    delivery_day = _next_workday(date.today()).isoformat()
    payload["sendDate"] = delivery_day

    # --- receiver: офис / адрес ---
    if to_office:
        payload["service"] = "toOffice"
        payload["receiverOfficeCode"] = str(receiver_office_code)
    else:
        payload["service"] = "toDoor"
        addr = {
            "city": {
                "country": {"code3": "BGR"},
                "name": rc,
                "postCode": (receiver_postcode or "").strip(),
            },
            "street": (receiver_street or "").strip(),
        }
        if receiver_num:
            addr["num"] = str(receiver_num)
        if receiver_entrance:
            addr["entrance"] = receiver_entrance
        if receiver_floor:
            addr["floor"] = receiver_floor
        if receiver_apartment:
            addr["apartment"] = receiver_apartment

        payload["receiverAddress"] = addr

    # --- services (Обявена стойност + НП + SMS) ---
    services: dict = {}

    if declared_value_bgn > 0:
        services["declaredValueAmount"] = declared_value_bgn
        services["declaredValueCurrency"] = "BGN"

    if cod_bgn > 0:
        # Наложен платеж (стойност на стоката)
        services["cdAmount"] = cod_bgn
        services["cdCurrency"] = "BGN"
        services["cdType"] = "get"

        # шаблон / споразумение за изплащане на НП – напр. "CD250332"
        if cod_agreement_number:
            services["cdPayOptionsTemplate"] = cod_agreement_number

        # за споразумения „по департамент“ Еконт иска фактура или опис
        if invoice_num:
            services["invoiceNum"] = invoice_num
            # това e точно чекчето „Предай ф-ра преди плащане на НП“
            services["invoiceBeforePayCD"] = True

        # получателят плаща НП в брой
        payload["paymentReceiverMethod"] = "CASH"
        payload["paymentReceiverAmount"] = cod_bgn

    if sms_notification:
        services["smsNotification"] = True

    if services:
        payload["services"] = services

    # --- packingList = „Бърз опис“ ---
    # --- packingList = „Бърз опис“ ---
    if packing_list:
        plt = (packing_list_type or "digital").strip().lower()
        if plt not in ("file", "digital", "loading"):
            plt = "digital"

        payload["packingListType"] = plt
        payload["packingList"] = packing_list

    return payload


def build_packing_list_from_order(order) -> list[dict]:
    """
    Builds Econt packingList (array of PackingListElement) from your Order items.

    Expected output item keys:
      - inventoryNum (string)   -> SKU / index
      - description (string)    -> product name
      - weight (double)         -> kg
      - count (int)             -> qty
      - price (double)          -> unit price (BGN)
    """

    # 1) Try to find the related manager with items.
    # Adjust this if your related name is different.
    items_manager = None
    for attr in ("items", "order_items", "orderitem_set"):
        maybe = getattr(order, attr, None)
        if maybe is not None and hasattr(maybe, "all"):
            items_manager = maybe
            break

    if items_manager is None:
        # No related items found -> return empty (no packing list)
        return []

    UNIT_WEIGHT_KG = Decimal("0.400")

    packing = []
    for idx, it in enumerate(items_manager.all(), start=1):
        # --- sku / inventory num ---
        sku = (
                getattr(it, "sku", None)
                or getattr(getattr(it, "product", None), "sku", None)
                or str(idx)
        )

        # --- name / description ---
        desc = (
                getattr(it, "name", None)
                or getattr(it, "title", None)
                or getattr(getattr(it, "product", None), "name", None)
                or str(getattr(it, "product", "Артикул"))
        )

        # --- qty ---
        qty = getattr(it, "quantity", None)
        if qty is None:
            qty = getattr(it, "qty", 1)
        qty = int(qty or 1)

        # --- unit price ---
        unit_price = (
                getattr(it, "unit_price_bgn", None)
                or getattr(it, "price_bgn", None)
                or getattr(it, "price", None)
                or 0
        )
        unit_price = float(unit_price or 0)

        # ✅ HARD FIX: packingList weight = 0.400 * quantity
        row_weight = (UNIT_WEIGHT_KG * Decimal(qty)).quantize(Decimal("0.001"))
        row_weight_f = float(row_weight)

        packing.append({
            "inventoryNum": str(sku),
            "description": str(desc),
            "weight": row_weight_f,
            "count": qty,
            "price": unit_price,
        })

    return packing


# --- Optional XML helpers (kept only if you still need them somewhere else) ---

def parse_label_response(xml_text: str) -> Tuple[Optional[str], Optional[bytes], Optional[str]]:
    """
    (shipment_num, pdf_bytes, error) — defensive against minor schema variants.
    Not used by the JSON flow, but kept here in case other parts still post XML.
    """
    try:
        root = etree.fromstring(xml_text.encode("utf-8"))
    except Exception as e:
        return None, None, f"Invalid XML from Econt: {e}"

    num = None
    for xp in ["//shipmentNumber", "//shipment/num", "//num"]:
        node = root.xpath(xp)
        if node and (node[0].text or "").strip():
            num = node[0].text.strip()
            break

    pdf_b64 = None
    for xp in ["//labelPDF", "//pdf", "//label"]:
        node = root.xpath(xp)
        if node and (node[0].text or "").strip():
            pdf_b64 = node[0].text.strip()
            break

    pdf_bytes = None
    if pdf_b64:
        try:
            pdf_bytes = base64.b64decode(pdf_b64)
        except Exception:
            pdf_bytes = None

    err = None
    errs = root.xpath("//error | //errors/error | //message[@type='error']")
    if errs:
        msgs = []
        for n in errs:
            t = (n.text or "").strip()
            if t:
                msgs.append(t)
        if msgs:
            err = "; ".join(msgs)

    return num, pdf_bytes, err

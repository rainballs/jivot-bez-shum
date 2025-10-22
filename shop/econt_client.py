# shop/econt_client.py
from __future__ import annotations

import json
import logging
from typing import Optional, Tuple
import base64

import requests
from django.conf import settings
from lxml import etree

log = logging.getLogger("econt")


class EcontError(RuntimeError):
    pass


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
        payer: str = "receiver",
        label_format: str = "10x9",
) -> dict:
    payload = {
        "shipmentType": "PACK",  # ← REQUIRED for JSON API
        "service": None,  # set below
        "shipmentPackCount": int(parcels),
        "shipmentPackWeight": float(weight_kg),
        "payer": (payer or "receiver").upper(),  # "RECEIVER" or "SENDER"
        "declaredValue": float(declared_value_bgn),
        "label": {"format": label_format},

        "senderClient": {"name": sender_name, "phones": [sender_phone]},
        "senderAddress": {"countryCode": "BG", "city": sender_city},

        "receiverClient": {"name": receiver_name, "phones": [receiver_phone]},
        "receiverAddress": {"countryCode": "BG", "city": receiver_city},
    }

    # Sender: office OR address
    if sender_office_code:
        payload["senderOfficeCode"] = str(sender_office_code)
    else:
        # Econt accepts a free-form street when needed; if you have split fields, map them here.
        payload["senderAddress"]["street"] = sender_address or ""

    # Route: office vs door
    if receiver_office_code:
        payload["service"] = "toOffice"
        payload["receiverOfficeCode"] = str(receiver_office_code)
    else:
        payload["service"] = "toDoor"
        ra = payload["receiverAddress"]
        ra["street"] = receiver_street or ""
        if receiver_num:
            ra["num"] = str(receiver_num)
        if receiver_postcode:
            ra["postCode"] = str(receiver_postcode)
        if receiver_entrance:
            ra["entrance"] = receiver_entrance
        if receiver_floor:
            ra["floor"] = receiver_floor
        if receiver_apartment:
            ra["apartment"] = receiver_apartment

    # COD (optional)
    if cod_bgn and float(cod_bgn) > 0:
        payload["cdAmount"] = float(cod_bgn)

    return payload


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

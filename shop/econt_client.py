# shop/econt_client.py
"""
Thin, strict client for the Econt JSON API (https://ee.econt.com/services/...).

Facts verified against the Econt DEMO server (see report):
  * validation / business errors come back as HTTP 517 (not 200) with a JSON body
    {"type": "Ex...", "message": " ", "innerErrors": [...]} - the real reason is in `innerErrors`;
  * a successful createLabel returns HTTP 200 with {"label": {"shipmentNumber": ..., "pdfURL": ...}, ...};
  * there is NO `payer` field on the label: the delivery payer is controlled by `paymentReceiverMethod`
    (unknown values are silently ignored -> the sender pays);
  * mode "calculate" does NOT check street/number completeness, mode "validate" does;
  * a door delivery with sendDate on a FRIDAY is rejected ("Моля, изберете ден за доставка") unless
    `holidayDeliveryDay` is sent; `calculate` does not reveal this, `validate`/`create` do.

Outcome classification (important for not creating duplicate shipments):
  EcontRejected        provider answered and refused  -> nothing was created, safe to fix & retry
  EcontNotSent         request provably never left     -> safe to retry
  EcontOutcomeUnknown  timeout / 5xx / garbage / 200 without shipment number -> Econt MAY have created
                       the shipment: never retry blindly, reconcile first.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

import requests
import urllib3
from django.conf import settings

log = logging.getLogger("econt")


# --------------------------------------------------------------------------- errors
class EcontError(RuntimeError):
    pass


class EcontRejected(EcontError):
    """Econt answered and refused the request (definitive; nothing created)."""

    def __init__(self, message: str, http_status: int | None = None):
        super().__init__(message)
        self.http_status = http_status


class EcontAuthError(EcontRejected):
    """401/403: credentials / account configuration problem. Fix configuration, then retry."""


class EcontNotSent(EcontError):
    """The request provably never reached Econt (DNS failure, connection refused, connect timeout)."""


class EcontOutcomeUnknown(EcontError):
    """Econt may or may not have processed the request."""

    def __init__(self, message: str, http_status: int | None = None):
        super().__init__(message)
        self.http_status = http_status


# --------------------------------------------------------------------------- helpers
def _next_workday(d: date) -> date:
    wd = d.weekday()
    if wd >= 4:  # Fri-Sun -> Monday
        return d + timedelta(days=7 - wd)
    return d + timedelta(days=1)


def flatten_errors(body) -> str:
    """Collect every non-blank `message` in an Econt error tree (top-level message is often blank)."""
    out: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            msg = node.get("message") or node.get("text") or node.get("error")
            if isinstance(msg, str) and msg.strip():
                out.append(msg.strip())
            for key in ("innerErrors", "errors"):
                for child in node.get(key) or []:
                    walk(child)
        elif isinstance(node, str) and node.strip():
            out.append(node.strip())

    walk(body)
    seen, uniq = set(), []
    for m in out:
        if m not in seen:
            seen.add(m)
            uniq.append(m)
    return " / ".join(uniq)[:1000]


def _is_error_body(body) -> bool:
    if not isinstance(body, dict) or "label" in body:
        return False
    t = body.get("type")
    return (isinstance(t, str) and t.startswith("Ex")) or "innerErrors" in body or bool(
        body.get("error") or body.get("errors")
    )


def _never_sent(exc: Exception) -> bool:
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return True
    if isinstance(exc, requests.exceptions.ConnectionError):
        reason = getattr(exc.args[0], "reason", None) if exc.args else None
        return isinstance(
            reason,
            (
                urllib3.exceptions.NewConnectionError,
                urllib3.exceptions.NameResolutionError,
                urllib3.exceptions.ConnectTimeoutError,
            ),
        )
    return False


class EcontClient:
    """JSON client. Never logs payloads or response bodies (they contain customer personal data)."""

    def __init__(self, timeout=None):
        base = settings.ECONT["BASE_URL"].rstrip("/")
        self.base = base
        self.create_label_url = f"{base}/Shipments/LabelService.createLabel.json"
        self.my_awb_url = f"{base}/Shipments/ShipmentService.getMyAWB.json"
        self.statuses_url = f"{base}/Shipments/ShipmentService.getShipmentStatuses.json"
        self.timeout = timeout or settings.ECONT.get("TIMEOUT", (5, 25))

        self.sess = requests.Session()
        self.sess.auth = (settings.ECONT.get("USER", ""), settings.ECONT.get("PASS", ""))
        self.sess.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
        })

    # ------------------------------------------------------------------ transport
    def _post(self, url: str, body: dict, what: str):
        import json as _json

        data = _json.dumps(body, ensure_ascii=False).encode("utf-8")
        try:
            r = self.sess.post(url, data=data, timeout=self.timeout)
        except requests.exceptions.RequestException as e:
            if _never_sent(e):
                log.warning("econt %s: request not sent (%s)", what, type(e).__name__)
                raise EcontNotSent(f"{what}: connection failed ({type(e).__name__})") from e
            log.error("econt %s: outcome unknown (%s)", what, type(e).__name__)
            raise EcontOutcomeUnknown(f"{what}: {type(e).__name__}") from e

        try:
            parsed = r.json()
        except ValueError:
            parsed = None
        return r.status_code, parsed, r

    def _interpret(self, status: int, body, what: str) -> dict:
        """Turn (status, body) into a successful dict or the right exception."""
        if status == 200 and isinstance(body, dict):
            if _is_error_body(body):
                msg = flatten_errors(body) or "Econt returned an error without a message"
                log.warning("econt %s: HTTP 200 business error: %s", what, msg)
                raise EcontRejected(msg, http_status=200)
            return body

        if status in (401, 403):
            raise EcontAuthError(f"Econt authentication/authorization failed (HTTP {status})", http_status=status)
        if status == 429:
            raise EcontNotSent("Econt rate limit (HTTP 429)")
        if _is_error_body(body):
            msg = flatten_errors(body) or f"HTTP {status}"
            log.warning("econt %s: rejected HTTP %s: %s", what, status, msg)
            raise EcontRejected(msg, http_status=status)
        if 400 <= status < 500 and status != 408:
            raise EcontRejected(f"HTTP {status}", http_status=status)
        raise EcontOutcomeUnknown(
            f"{what}: HTTP {status} {'(non-JSON body)' if body is None else '(unexpected body)'}",
            http_status=status,
        )

    # ------------------------------------------------------------------ operations
    def create_label(self, label_payload: dict) -> dict:
        """
        Create a REAL shipment. Returns {"shipment_num", "pdf_url", "label", "body"}.
        A 200 without label.shipmentNumber is NOT treated as success (-> EcontOutcomeUnknown).
        """
        status, body, _ = self._post(self.create_label_url, {"mode": "create", "label": label_payload}, "create")
        body = self._interpret(status, body, "create")
        label = body.get("label") if isinstance(body.get("label"), dict) else {}
        num = str(label.get("shipmentNumber") or "").strip()
        if not num:
            raise EcontOutcomeUnknown("create: HTTP 200 but no shipmentNumber in response", http_status=200)
        return {"shipment_num": num, "pdf_url": label.get("pdfURL") or "", "label": label, "body": body}

    def validate_label(self, label_payload: dict) -> dict:
        """mode=validate: full server-side validation (incl. street/number) without creating anything."""
        status, body, _ = self._post(self.create_label_url, {"mode": "validate", "label": label_payload}, "validate")
        return self._interpret(status, body, "validate")

    def calculate_label(self, label_payload: dict) -> dict:
        """mode=calculate: price only. Returns the `label` block."""
        status, body, _ = self._post(self.create_label_url, {"mode": "calculate", "label": label_payload}, "calculate")
        body = self._interpret(status, body, "calculate")
        return body.get("label") or body

    def my_awb(self, date_from: date, date_to: date, page: int = 1, side: str = "sender") -> dict:
        """Read-only listing of our shipments (used for reconciliation)."""
        status, body, _ = self._post(
            self.my_awb_url,
            {"dateFrom": date_from.isoformat(), "dateTo": date_to.isoformat(), "page": page, "side": side},
            "getMyAWB",
        )
        return self._interpret(status, body, "getMyAWB")

    def shipment_statuses(self, numbers: list[str]) -> dict:
        status, body, _ = self._post(self.statuses_url, {"shipmentNumbers": list(numbers)}, "getShipmentStatuses")
        return self._interpret(status, body, "getShipmentStatuses")


# --------------------------------------------------------------------------- payload builder
def _money(x) -> float:
    return float(Decimal(str(x or 0)).quantize(Decimal("0.01")))


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
        receiver_quarter: str | None = None,
        receiver_other: str | None = None,
        weight_kg: float = 0.8,
        parcels: int = 1,
        cod_amount=0,
        cod_currency: str = "EUR",
        declared_value=0,
        declared_currency: str = "EUR",
        receiver_pays_delivery: bool = False,
        label_format: str = "10x9",
        cod_agreement_number: str | None = None,
        invoice_num: str | None = None,
        sms_notification: bool = False,
        order_number: str | None = None,
        holiday_delivery_day: str | None = "workday",
        packing_list: list[dict] | None = None,
        packing_list_type: str | None = "digital",
) -> dict:
    """
    Build the JSON label for Econt (create / validate / calculate).

    Payment semantics (Econt-verified):
      * cod_amount > 0           -> services.cdAmount: merchandise cash-on-delivery the recipient pays.
      * receiver_pays_delivery   -> paymentReceiverMethod=CASH: recipient pays courier charges (delivery, COD fee,
                                    SMS, declared value). When False nothing is set and the SENDER pays Econt.
      Both are independent: prepaid card orders use cod_amount=0 and receiver_pays_delivery=False.
    """
    cod = _money(cod_amount)
    declared = _money(declared_value)

    rn = (receiver_name or "").strip()
    rp = (receiver_phone or "").strip()
    rc = (receiver_city or "").strip()
    if not rn or not rp or not rc:
        raise ValueError("Missing mandatory receiver fields (name/phone/city)")

    to_office = bool(receiver_office_code)
    if not to_office:
        if not (receiver_street or "").strip() or not (receiver_postcode or "").strip() or not (receiver_num or "").strip():
            raise ValueError("Missing address for toDoor shipment (street/number/postcode)")

    payload: dict = {
        "shipmentType": "PACK",
        "service": None,  # set below
        "packCount": int(parcels),
        "weight": float(weight_kg),
        "shipmentDescription": "Книга",
        "label": {"format": label_format},
        "senderClient": {"name": sender_name, "phones": [sender_phone]},
        "senderAgent": {"name": sender_name, "phones": [sender_phone]},
        "senderAddress": {
            "city": {
                "country": {"code3": "BGR"},
                "name": sender_city,
                "postCode": "8000",  # fixed sender postcode
            }
        },
        "receiverClient": {"name": rn, "phones": [rp]},
    }
    if order_number:
        payload["orderNumber"] = str(order_number)

    if sender_office_code:
        payload["senderOfficeCode"] = str(sender_office_code)
    else:
        payload["senderAddress"]["street"] = sender_address or ""

    payload["sendDate"] = _next_workday(date.today()).isoformat()
    # Econt REJECTS door deliveries whose send date is a Friday ("Моля, изберете ден за доставка на пратката") unless
    # the day is chosen explicitly; the next-workday rule above yields Friday for every order handled on a Thursday.
    # Verified on the Econt demo server (mode=validate): accepted on every weekday when sent. Always sending it also
    # covers public holidays. Values: "workday" | "Halfday" | yyyy-mm-dd.
    if holiday_delivery_day:
        payload["holidayDeliveryDay"] = holiday_delivery_day

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
            "num": str(receiver_num).strip(),
        }
        if receiver_entrance:
            addr["entrance"] = receiver_entrance
        if receiver_floor:
            addr["floor"] = receiver_floor
        if receiver_apartment:
            addr["apartment"] = receiver_apartment
        if receiver_quarter:
            addr["quarter"] = receiver_quarter
        if receiver_other:
            addr["other"] = receiver_other
        payload["receiverAddress"] = addr

    services: dict = {}
    if declared > 0:
        services["declaredValueAmount"] = declared
        services["declaredValueCurrency"] = declared_currency

    if cod > 0:
        services["cdAmount"] = cod
        services["cdCurrency"] = cod_currency
        services["cdType"] = "get"
        if cod_agreement_number:
            services["cdPayOptionsTemplate"] = cod_agreement_number
        if invoice_num:
            services["invoiceNum"] = invoice_num
            services["invoiceBeforePayCD"] = True

    if sms_notification:
        services["smsNotification"] = True
    if services:
        payload["services"] = services

    if receiver_pays_delivery:
        payload["paymentReceiverMethod"] = "CASH"

    if packing_list:
        plt = (packing_list_type or "digital").strip().lower()
        if plt not in ("file", "digital", "loading"):
            plt = "digital"
        payload["packingListType"] = plt
        payload["packingList"] = packing_list

    return payload


def summarize_payload(payload: dict) -> dict:
    """Personal-data-free description of what we asked Econt for (stored in ShipmentAttempt)."""
    services = payload.get("services") or {}
    return {
        "service": payload.get("service"),
        "receiver_office_code": payload.get("receiverOfficeCode"),
        "receiver_city": ((payload.get("receiverAddress") or {}).get("city") or {}).get("name"),
        "cd_amount": services.get("cdAmount"),
        "cd_currency": services.get("cdCurrency"),
        "has_cd_agreement": bool(services.get("cdPayOptionsTemplate")),
        "declared_value": services.get("declaredValueAmount"),
        "receiver_pays_delivery": payload.get("paymentReceiverMethod") is not None,
        "payment_receiver_method": payload.get("paymentReceiverMethod"),
        "pack_count": payload.get("packCount"),
        "send_date": payload.get("sendDate"),
    }


def build_packing_list_from_order(order) -> list[dict]:
    """
    Econt packingList (array of PackingListElement) from the order's items.
      inventoryNum -> SKU/index, description -> product name, weight -> kg for the row,
      count -> qty, price -> unit price (EUR)
    """
    UNIT_WEIGHT_KG = Decimal("0.400")
    packing = []
    for idx, it in enumerate(order.items.select_related("product").all(), start=1):
        qty = int(it.quantity or 1)
        packing.append({
            "inventoryNum": str(idx),
            "description": str(it.product.name),
            "weight": float((UNIT_WEIGHT_KG * Decimal(qty)).quantize(Decimal("0.001"))),
            "count": qty,
            "price": float(it.unit_price_eur or 0),
        })
    return packing

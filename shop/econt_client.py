# shop/econt_client.py
import base64
import logging, textwrap, requests
from typing import Optional, Tuple
import requests
from django.conf import settings
from lxml import etree

log = logging.getLogger("econt")


class EcontClient:
    def __init__(self):
        base = settings.ECONT["BASE_URL"].rstrip("/")
        # try both common variants
        self.endpoints = [
            f"{base}/createLabel",
            f"{base}/ShipmentsService.createLabel",
        ]
        self.headers_xml = {"Content-Type": "application/xml; charset=utf-8"}
        self.headers_form = {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"}
        self.auth = (settings.ECONT["USER"], settings.ECONT["PASS"])

    def _post_xml(self, xml_bytes: bytes):
        """
        Try both post styles (form xml=<payload> and raw XML) against both endpoints.
        Return first non-empty response; raise otherwise.
        """
        xml_str = xml_bytes.decode("utf-8", errors="ignore")
        last_err = None

        for url in self.endpoints:
            # 1) form-encoded
            try:
                log.error(
                    "ECONT ▶ FORM %s\n%s",
                    url,
                    textwrap.shorten(xml_str, width=4000, placeholder="…"),
                )
                r = requests.post(
                    url,
                    data={"xml": xml_str},
                    headers=self.headers_form,
                    auth=self.auth,
                    timeout=30,
                )
                log.error(
                    "ECONT ◀ %s %s (form)\n%s",
                    r.status_code,
                    r.reason,
                    textwrap.shorten((r.text or ""), width=4000, placeholder="…"),
                )
                r.raise_for_status()
                if (r.text or "").strip():
                    return r.text
                last_err = RuntimeError("Empty body on form post")
            except Exception as e:
                last_err = e

            # 2) raw xml
            try:
                log.error(
                    "ECONT ▶ RAW  %s\n%s",
                    url,
                    textwrap.shorten(xml_str, width=4000, placeholder="…"),
                )
                r = requests.post(
                    url,
                    data=xml_bytes,
                    headers=self.headers_xml,
                    auth=self.auth,
                    timeout=30,
                )
                log.error(
                    "ECONT ◀ %s %s (raw)\n%s",
                    r.status_code,
                    r.reason,
                    textwrap.shorten((r.text or ""), width=4000, placeholder="…"),
                )
                r.raise_for_status()
                if (r.text or "").strip():
                    return r.text
                last_err = RuntimeError("Empty body on raw post")
            except Exception as e:
                last_err = e

        raise RuntimeError(f"Econt createLabel failed on all attempts: {last_err}")


def build_create_label_xml(*,
                           sender_name: str, sender_phone: str, sender_city: str,
                           sender_address: str = "", sender_office_code: str = "",
                           receiver_name: str, receiver_phone: str, receiver_city: str,
                           # to-office (if set, structured address is ignored)
                           receiver_office_code: str = "",
                           # to-door (structured)
                           receiver_street: str = "", receiver_num: str = "",
                           receiver_postcode: str = "",
                           receiver_entrance: str = "", receiver_floor: str = "",
                           receiver_apartment: str = "",
                           # shipment
                           weight_kg: float = 0.8, parcels: int = 1,
                           cod_bgn: float = 0.0, declared_value_bgn: float = 0.0,
                           payer: str = "receiver"):
    """
    Econt /ee/services/createLabel:
    - service MUST be present: toOffice | toDoor
    - payer is often required uppercase: SENDER | RECEIVER
    """
    # Normalize enums
    payer_up = (payer or "").strip().upper()
    if payer_up not in {"SENDER", "RECEIVER", "THIRD_PARTY"}:
        payer_up = "RECEIVER"  # safe default
    service = "toOffice" if receiver_office_code else "toDoor"

    root = etree.Element("createLabelRequest")

    # sender
    s = etree.SubElement(root, "sender")
    etree.SubElement(s, "name").text = sender_name
    try:
        phone_norm = receiver_phone_fmt(sender_phone)
    except NameError:
        phone_norm = sender_phone
    etree.SubElement(s, "phone").text = phone_norm
    etree.SubElement(s, "countryCode").text = "BG"
    etree.SubElement(s, "city").text = sender_city
    if sender_office_code:
        etree.SubElement(s, "officeCode").text = sender_office_code
    if sender_address:
        etree.SubElement(s, "address").text = sender_address

    # receiver
    r = etree.SubElement(root, "receiver")
    etree.SubElement(r, "name").text = receiver_name
    try:
        r_phone_norm = receiver_phone_fmt(receiver_phone)
    except NameError:
        r_phone_norm = receiver_phone
    etree.SubElement(r, "phone").text = r_phone_norm
    etree.SubElement(r, "countryCode").text = "BG"
    etree.SubElement(r, "city").text = receiver_city

    if receiver_office_code:
        etree.SubElement(r, "officeCode").text = receiver_office_code
    else:
        addr = etree.SubElement(r, "address")
        if receiver_street:   etree.SubElement(addr, "street").text = receiver_street
        if receiver_num:      etree.SubElement(addr, "num").text = receiver_num
        if receiver_postcode: etree.SubElement(addr, "postCode").text = receiver_postcode
        extras = []
        if receiver_entrance:  extras.append(f"вх. {receiver_entrance}")
        if receiver_floor:     extras.append(f"ет. {receiver_floor}")
        if receiver_apartment: extras.append(f"ап. {receiver_apartment}")
        if extras:
            etree.SubElement(addr, "other").text = ", ".join(extras)

    # shipment
    sh = etree.SubElement(root, "shipment")
    etree.SubElement(sh, "type").text = "PACK"
    etree.SubElement(sh, "service").text = service  # <-- REQUIRED by many tenants
    etree.SubElement(sh, "weight").text = f"{float(weight_kg):.3f}"
    etree.SubElement(sh, "parcels").text = str(parcels)
    etree.SubElement(sh, "payer").text = payer_up  # <-- uppercase
    etree.SubElement(sh, "declaredValue").text = f"{float(declared_value_bgn):.2f}"
    if float(cod_bgn) > 0:
        etree.SubElement(sh, "cod").text = f"{float(cod_bgn):.2f}"

    lbl = etree.SubElement(root, "label")
    etree.SubElement(lbl, "format").text = "10x9"

    return etree.tostring(root, xml_declaration=True, encoding="UTF-8")


def receiver_phone_fmt(phone: str) -> str:
    if not phone:
        return ""
    d = "".join(ch for ch in phone if ch.isdigit())
    if d.startswith("359"):
        d = d[3:]
    elif d.startswith("0"):
        d = d[1:]
    return "+359" + d


def parse_label_response(xml_text: str) -> Tuple[Optional[str], Optional[bytes], Optional[str]]:
    """
    (shipment_num, pdf_bytes, error) — defensive against minor schema variants.
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

    # collect any error messages
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

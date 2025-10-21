# shop/econt_client.py
import base64
import logging
from typing import Optional, Tuple
import requests
from django.conf import settings
from lxml import etree

log = logging.getLogger(__name__)


class EcontClient:
    def __init__(self):
        base = settings.ECONT["BASE_URL"].rstrip("/")
        self.create_label_url = f"{base}/createLabel"
        self.headers = {"Content-Type": "application/xml; charset=utf-8"}
        self.auth = (settings.ECONT["USER"], settings.ECONT["PASS"])

    def _post_xml(self, url: str, xml_bytes: bytes):
        log.info("ECONT POST %s\n%s", url, xml_bytes.decode("utf-8", errors="ignore"))
        r = requests.post(url, data=xml_bytes, headers=self.headers, auth=self.auth, timeout=30)
        log.info("ECONT RESP %s %s\n%s", url, r.status_code, (r.text or "")[:3000])
        r.raise_for_status()
        if not r.text.strip():
            # Make the failure explicit (this is what you saw)
            raise RuntimeError("Empty response from Econt (check office code / required fields).")
        return r.text


def build_create_label_xml(*,
                           sender_name: str, sender_phone: str, sender_city: str,
                           sender_address: str = "", sender_office_code: str = "",
                           receiver_name: str, receiver_phone: str, receiver_city: str,
                           receiver_address: str = "", receiver_office_code: str = "",
                           weight_kg: float = 0.8, parcels: int = 1,
                           cod_bgn: float = 0.0, declared_value_bgn: float = 0.0,
                           payer: str = "receiver"):
    """
    Minimal, but works for both to-office and to-door on /ee/services/createLabel.
    """
    root = etree.Element("createLabelRequest")

    s = etree.SubElement(root, "sender")
    etree.SubElement(s, "name").text = sender_name
    etree.SubElement(s, "phone").text = receiver_phone_fmt(sender_phone)  # ensure +359…
    etree.SubElement(s, "city").text = sender_city
    if sender_office_code:
        etree.SubElement(s, "officeCode").text = sender_office_code
    if sender_address:
        etree.SubElement(s, "address").text = sender_address

    r = etree.SubElement(root, "receiver")
    etree.SubElement(r, "name").text = receiver_name
    etree.SubElement(r, "phone").text = receiver_phone_fmt(receiver_phone)
    etree.SubElement(r, "city").text = receiver_city
    if receiver_office_code:
        etree.SubElement(r, "officeCode").text = receiver_office_code
    if receiver_address:
        etree.SubElement(r, "address").text = receiver_address

    sh = etree.SubElement(root, "shipment")
    etree.SubElement(sh, "weight").text = f"{float(weight_kg):.3f}"
    etree.SubElement(sh, "parcels").text = str(parcels)
    etree.SubElement(sh, "payer").text = payer
    etree.SubElement(sh, "cod").text = f"{float(cod_bgn):.2f}"
    etree.SubElement(sh, "declaredValue").text = f"{float(declared_value_bgn):.2f}"
    etree.SubElement(sh, "service").text = "toOffice" if receiver_office_code else "toDoor"

    return etree.tostring(root, xml_declaration=True, encoding="UTF-8")


def receiver_phone_fmt(phone: str) -> str:
    """Return +359XXXXXXXXX (digits only), tolerant to input."""
    if not phone:
        return ""
    digits = "".join(ch for ch in phone if ch.isdigit())
    if digits.startswith("359"):
        digits = digits[3:]
    elif digits.startswith("0"):
        digits = digits[1:]
    return "+359" + digits


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

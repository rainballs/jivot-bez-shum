# shop/econt_service.py
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from .econt_client import (
    EcontClient,
    build_create_label_xml,
    parse_label_response,
)


def create_econt_label(order, overrides: dict | None = None) -> dict:
    """
    Create an Econt label for the given order.
    - Supports BOTH: "to office" (via order.econt_office_code) and "to door" (via structured overrides).
    - If `order.paid` is True -> no COD; else COD equals total_bgn.
    - Returns: { ok, shipment_num, saved_pdf, error, raw? }
    """
    overrides = overrides or {}
    d = settings.ECONT["DEFAULTS"]

    # ---- Receiver core fields (take from order) ----
    receiver_name = getattr(order, "full_name",
                            "") or f"{getattr(order, 'first_name', '')} {getattr(order, 'last_name', '')}".strip()
    receiver_phone = getattr(order, "phone", "")
    receiver_city = getattr(order, "city", "")

    # ---- Decide COD & weights ----
    cod_bgn = 0.0 if getattr(order, "paid", False) else float(getattr(order, "total_bgn", 0.0) or 0.0)
    declared_bgn = float(getattr(order, "total_bgn", 0.0) or 0.0)
    weight_kg = float(getattr(order, "total_weight_kg", 0.800) or 0.800)

    # ---- Routing: office vs door ----
    receiver_office_code = getattr(order, "econt_office_code", "") or overrides.get("receiver_office_code", "")

    # Structured address (used only when NOT shipping to office)
    receiver_street = overrides.get("receiver_street", "")
    receiver_num = overrides.get("receiver_num", "")
    receiver_postcode = overrides.get("receiver_postcode", "")
    receiver_entrance = overrides.get("receiver_entrance", "")
    receiver_floor = overrides.get("receiver_floor", "")
    receiver_apartment = overrides.get("receiver_apartment", "")

    # ---- Build XML ----
    xml_payload = build_create_label_xml(
        # sender
        sender_name=d["sender_name"],
        sender_phone=d["sender_phone"],
        sender_city=d["sender_city"],
        sender_address=d["sender_address"],
        sender_office_code=d["sender_office"],

        # receiver (common)
        receiver_name=receiver_name,
        receiver_phone=receiver_phone,
        receiver_city=receiver_city,

        # to-office path
        receiver_office_code=receiver_office_code,

        # to-door structured path (ignored if office_code is set)
        receiver_street=receiver_street,
        receiver_num=receiver_num,
        receiver_postcode=receiver_postcode,
        receiver_entrance=receiver_entrance,
        receiver_floor=receiver_floor,
        receiver_apartment=receiver_apartment,

        # shipment
        weight_kg=weight_kg,
        parcels=1,
        cod_bgn=cod_bgn,
        declared_value_bgn=declared_bgn,
        payer="receiver" if cod_bgn == 0 else "sender",
    )

    client = EcontClient()
    try:
        xml_resp = client._post_xml(xml_payload)
    except Exception as e:
        err = str(e)
        order.econt_errors = err
        order.save(update_fields=["econt_errors"])
        return {"ok": False, "shipment_num": None, "saved_pdf": False, "error": err}

    ship_num, pdf_bytes, parse_err = parse_label_response(xml_resp)
    if parse_err:
        order.econt_errors = parse_err
        order.save(update_fields=["econt_errors"])
        return {"ok": False, "shipment_num": None, "saved_pdf": False, "error": parse_err}

    saved_pdf = False
    with transaction.atomic():
        if ship_num:
            order.econt_shipment_num = ship_num
        order.econt_errors = None
        order.save(update_fields=["econt_shipment_num", "econt_errors"])

        if pdf_bytes:
            fname = f"econt_label_{order.pk}_{ship_num or 'unknown'}.pdf"
            order.econt_label_pdf.save(fname, ContentFile(pdf_bytes), save=True)
            saved_pdf = True

    return {"ok": True, "shipment_num": ship_num, "saved_pdf": saved_pdf, "error": None}

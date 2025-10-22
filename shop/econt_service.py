# shop/econt_service.py
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction

from .econt_client import EcontClient, EcontError, build_create_label_json


def create_econt_label(order, overrides: dict | None = None) -> dict:
    """
    Creates an Econt label (JSON API) using settings.ECONT.
    Returns: { ok, shipment_num, saved_pdf, error }
    """
    overrides = overrides or {}
    d = settings.ECONT["DEFAULTS"]

    # Receiver data
    receiver_name = (
            getattr(order, "full_name", "") or
            f"{getattr(order, 'first_name', '')} {getattr(order, 'last_name', '')}".strip()
    )
    receiver_phone = getattr(order, "phone", "")
    receiver_city = getattr(order, "city", "")

    # Office vs address
    receiver_office_code = getattr(order, "econt_office_code", "") or overrides.get("receiver_office_code")

    # Structured address (to door)
    r_street = overrides.get("receiver_street")
    r_num = overrides.get("receiver_num")
    r_postcode = overrides.get("receiver_postcode")
    r_entrance = overrides.get("receiver_entrance")
    r_floor = overrides.get("receiver_floor")
    r_apartment = overrides.get("receiver_apartment")

    # Shipping economics
    cod_bgn = 0.0 if getattr(order, "paid", False) else float(getattr(order, "total_bgn", 0.0) or 0.0)
    declared_bgn = float(getattr(order, "total_bgn", 0.0) or 0.0)
    weight_kg = float(getattr(order, "total_weight_kg", 0.800) or 0.800)
    payer = "receiver" if cod_bgn == 0 else "sender"

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

    # Success fields (shipment number + optional base64 PDF)
    shipment_num = res.get("shipmentNumber") or res.get("num") or res.get("shipment_num")
    pdf_bytes = None
    if "pdfBase64" in res:
        try:
            import base64
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

# shop/econt_service.py
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from .econt_client import EcontClient, build_create_label_xml, parse_label_response


def create_econt_label(order):
    """
    Creates an Econt label for this order.
    Returns dict: {ok, shipment_num, saved_pdf, error}
    """
    d = settings.ECONT["DEFAULTS"]

    receiver_office = getattr(order, "econt_office_code", "") or ""
    receiver_addr = getattr(order, "address_line", "") or getattr(order, "address", "")
    receiver_city = getattr(order, "city", "")
    receiver_name = getattr(order, "full_name", "") or f"{order.first_name} {order.last_name}".strip()
    receiver_phone = getattr(order, "phone", "")

    # Decide COD based on payment status/method
    cod = 0.0 if getattr(order, "paid", False) else float(order.total_bgn or 0.0)
    declared = float(getattr(order, "total_bgn", 0.0) or 0.0)
    weight = float(getattr(order, "total_weight_kg", 0.800) or 0.800)

    xml = build_create_label_xml(
        sender_name=d["sender_name"],
        sender_phone=d["sender_phone"],
        sender_city=d["sender_city"],
        sender_address=d["sender_address"],
        sender_office_code=d["sender_office"],
        receiver_name=receiver_name,
        receiver_phone=receiver_phone,
        receiver_city=receiver_city,
        receiver_address=receiver_addr,
        receiver_office_code=receiver_office,
        weight_kg=weight,
        parcels=1,
        cod_bgn=cod,
        declared_value_bgn=declared,
        payer="receiver" if cod == 0 else "sender",  # example rule
    )

    client = EcontClient()
    try:
        xml_resp = client._post_xml(client.create_label_url, xml)
    except Exception as e:
        order.econt_errors = str(e)
        order.save(update_fields=["econt_errors"])
        return {"ok": False, "shipment_num": None, "saved_pdf": False, "error": str(e)}

    ship_num, pdf_bytes, parse_err = parse_label_response(xml_resp)
    if parse_err:
        order.econt_errors = parse_err
        order.save(update_fields=["econt_errors"])
        return {"ok": False, "shipment_num": None, "saved_pdf": False, "error": parse_err}

    with transaction.atomic():
        if ship_num:
            order.econt_shipment_num = ship_num
        order.econt_errors = None
        order.save(update_fields=["econt_shipment_num", "econt_errors"])

        saved_pdf = False
        if pdf_bytes:
            fname = f"econt_label_{order.pk}_{ship_num or 'unknown'}.pdf"
            order.econt_label_pdf.save(fname, ContentFile(pdf_bytes), save=True)
            saved_pdf = True

    return {"ok": True, "shipment_num": ship_num, "saved_pdf": saved_pdf, "error": None}

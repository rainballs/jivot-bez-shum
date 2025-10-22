# shop/management/commands/econt_probe.py
from django.core.management.base import BaseCommand
from django.conf import settings
from shop.econt_client import EcontClient, build_create_label_xml


class Command(BaseCommand):
    help = "Probe Econt createLabel with hardcoded sane data to verify endpoint/creds/schema."

    def add_arguments(self, parser):
        parser.add_argument("--office", action="store_true", help="Send to office (Sofia 1501).")
        parser.add_argument("--door", action="store_true", help="Send to address (Burgas).")

    def handle(self, *args, **opts):
        c = EcontClient()
        d = settings.ECONT["DEFAULTS"]

        if opts["office"]:
            xml = build_create_label_xml(
                sender_name=d["sender_name"], sender_phone=d["sender_phone"],
                sender_city=d["sender_city"], sender_address=d["sender_address"],
                sender_office_code=d["sender_office"],
                receiver_name="Тест Клиент", receiver_phone="+359888000000",
                receiver_city="София", receiver_office_code="1501",
                weight_kg=0.800, parcels=1,
                cod_bgn=0.00, declared_value_bgn=20.00, payer="RECEIVER",
            )
        else:
            # default to address variant
            xml = build_create_label_xml(
                sender_name=d["sender_name"], sender_phone=d["sender_phone"],
                sender_city=d["sender_city"], sender_address=d["sender_address"],
                sender_office_code=d["sender_office"],
                receiver_name="Тест Клиент", receiver_phone="+359888000000",
                receiver_city="Бургас",
                receiver_street="ул. Александър Велики", receiver_num="12",
                receiver_postcode="8000",
                weight_kg=0.800, parcels=1,
                cod_bgn=0.00, declared_value_bgn=20.00, payer="RECEIVER",
            )

        try:
            body = c._post_xml(xml)
        except Exception as e1:
            self.stderr.write(self.style.ERROR(f"Primary endpoint failed: {e1}"))
            # try alt (if you added it earlier)
            try:
                body = c._post_xml(getattr(c, "create_label_url_alt"), xml)
            except Exception as e2:
                self.stderr.write(self.style.ERROR(f"Alt endpoint failed: {e2}"))
                return

        # Print first 1000 chars of the response
        print((body or "")[:1000])
        self.stdout.write(self.style.SUCCESS("Probe sent. Check gunicorn error log for XML and full response."))

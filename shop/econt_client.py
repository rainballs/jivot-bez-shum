# shop/econt_client.py
from __future__ import annotations
import base64, json, logging
from typing import Optional, Tuple
import requests
from django.conf import settings
from lxml import etree

log = logging.getLogger("econt")


class EcontError(RuntimeError):
    pass


class EcontClient:
    """
    JSON client with resilient fallback:
      - POST {BASE_URL}/Shipments/LabelService.createLabel.json   (primary)
      - POST {BASE_URL}/Shipments/LabelService.createLabel        (fallback)
    """

    def __init__(self):
        base = settings.ECONT["BASE_URL"].rstrip("/")
        self.urls = [
            f"{base}/Shipments/LabelService.createLabel.json",
            f"{base}/Shipments/LabelService.createLabel",
        ]
        self.username = settings.ECONT.get("USER", "")
        self.password = settings.ECONT.get("PASS", "")

        self.sess = requests.Session()
        self.sess.auth = (self.username, self.password)
        self.sess.headers.update({
            "Accept": "application/json",
            # IMPORTANT: omit charset to avoid gateway quirks
            "Content-Type": "application/json",
            "User-Agent": "FilYaka-Econt/1.0",
        })

    def _post_once(self, url: str, body: dict) -> requests.Response:
        # Use json= so requests serializes and sets headers correctly
        log.error("ECONT ▶ POST %s\n%s", url, json.dumps(body, ensure_ascii=False, indent=2))
        r = self.sess.post(url, json=body, timeout=30)
        log.error("ECONT ◀ %s %s\n%s", r.status_code, r.reason, (r.text or "")[:2000])
        return r

    def create_label(self, label_payload: dict) -> dict:
        body = {"mode": "create", "label": label_payload}

        last_err = None
        for i, url in enumerate(self.urls):
            r = self._post_once(url, body)

            # Retry on 517 or 404/415/500-ish from first URL; try second variant.
            if i == 0 and r.status_code in (517, 404, 415, 500, 502, 503):
                last_err = f"HTTP {r.status_code} {r.reason}"
                continue

            if r.status_code == 401:
                raise EcontError("Невалидни Econt креденшъли (HTTP 401). Проверете ECONT_USERNAME / ECONT_PASSWORD.")

            if r.status_code != 200:
                raise EcontError(f"HTTP {r.status_code} {r.reason}")

            # Some gateways return text 'null' or empty – treat as error
            txt = (r.text or "").strip()
            if not txt or txt.lower() == "null":
                last_err = "Празен отговор от Econt"
                continue

            # Decode JSON
            try:
                resp = r.json()
            except Exception:
                raise EcontError("Отговорът не е JSON (gateway формат).")

            # Econt often nests errors here:
            if isinstance(resp, dict) and (resp.get("error") or resp.get("message")):
                raise EcontError(str(resp.get("error") or resp.get("message")))

            # Success: return the 'label' object if present
            if isinstance(resp, dict) and "label" in resp:
                return resp["label"]
            return resp  # already a flat dict with fields

        # If we got here, both attempts failed
        raise EcontError(last_err or "Неуспешен опит за създаване на товарителница.")

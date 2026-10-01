import re
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]
# Templates that are not rendered by any route (dead code); their files may be missing.
UNUSED = {"checkout/payment.html", "partials/seo.html"}
STATIC_TAG = re.compile(r"""\{%\s*static\s+['"]([^'"]+)['"]""")


class StaticReferenceTests(SimpleTestCase):
    def test_every_static_file_used_by_a_live_template_exists(self):
        """
        With ManifestStaticFilesStorage (production, DEBUG off) `{% static 'x' %}` on a missing file raises and
        the whole page returns HTTP 500 - base.html pointed at an og-default.jpg that was never committed.
        """
        missing, checked = [], 0
        for path in (ROOT / "templates").rglob("*.html"):
            rel = path.relative_to(ROOT / "templates").as_posix()
            if rel in UNUSED:
                continue
            for ref in STATIC_TAG.findall(path.read_text(encoding="utf-8")):
                checked += 1
                if not (ROOT / "static" / ref).exists():
                    missing.append(f"{rel}: {ref}")
        self.assertGreater(checked, 5, "the scan found no static references - the pattern is broken")
        self.assertEqual(missing, [])

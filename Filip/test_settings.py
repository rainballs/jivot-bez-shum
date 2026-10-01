"""
Settings for running the test-suite:  python manage.py test --settings=Filip.test_settings

* Fake provider credentials: a test can never reach live Stripe / live Econt even if a mock is forgotten.
* SQLite by default. Set TEST_DB=postgres to run against the DB_* settings from .env instead
  (needed for the real row-lock concurrency tests, which are skipped on SQLite).
"""
import os

from .settings import *  # noqa: F401,F403

if os.environ.get("TEST_DB", "").lower() != "postgres":
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}

SECRET_KEY = "test-secret-key-not-used-anywhere-else"
DEBUG = False
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
SITE_URL = "https://shop.test"
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SECURE_SSL_REDIRECT = False

STRIPE_SECRET_LIVE_KEY = "sk_test_fake"
STRIPE_PUBLIC_LIVE_KEY = "pk_test_fake"
STRIPE_WEBHOOK_SECRET = "whsec_test_secret"

ECONT = {
    "BASE_URL": "https://econt.invalid/ee/services",  # .invalid never resolves
    "USER": "test",
    "PASS": "test",
    "DEFAULTS": {
        "sender_name": "Test Sender",
        "sender_phone": "+359888000000",
        "sender_city": "Бургас",
        "sender_address": "ул. Тест 1",
        "sender_office": None,
        "label_format": "10x9",
        "cd_template": "DEFAULT",
        "cod_agreement_number": "CD000000",
        "holiday_delivery_day": "workday",
    },
    "TIMEOUT": (1, 1),
}

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
ORDER_NOTIFY_EMAIL = "admin@shop.test"
DEFAULT_FROM_EMAIL = "shop@shop.test"
SHIPMENT_INLINE_DISPATCH = True
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
SILENCED_SYSTEM_CHECKS = ["shop.W004"]

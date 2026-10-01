from django.apps import AppConfig
from django.core.checks import Warning, register


class ShopConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "shop"


@register()
def econt_configuration_check(app_configs, **kwargs):
    """Warn about configurations that make Econt calls silently useless (demo server) or unauthenticated."""
    from django.conf import settings

    problems = []
    cfg = getattr(settings, "ECONT", {})
    base = cfg.get("BASE_URL", "")
    if not settings.DEBUG and "demo.econt.com" in base:
        problems.append(Warning(
            "ECONT BASE_URL points to the Econt DEMO server while DEBUG is off: labels created there are not "
            "real shipments. Set ECONT_LIVE_BASE_URL (and ECONT_LIVE_USERNAME / ECONT_LIVE_PASSWORD).",
            id="shop.W001",
        ))
    if not settings.DEBUG and not (cfg.get("USER") and cfg.get("PASS")):
        problems.append(Warning("ECONT credentials are empty.", id="shop.W002"))
    if not settings.DEBUG and not settings.STRIPE_WEBHOOK_SECRET:
        problems.append(Warning("STRIPE_WEBHOOK_SECRET is empty: card payments cannot be confirmed.", id="shop.W003"))
    key = getattr(settings, "STRIPE_SECRET_LIVE_KEY", "")
    if not settings.DEBUG and key and not key.startswith(("sk_live", "rk_live")):
        problems.append(Warning(
            "STRIPE_SECRET_LIVE_KEY is not a live key while DEBUG is off (test-mode payments would be accepted "
            "and live-mode webhook events ignored).", id="shop.W004"))
    return problems

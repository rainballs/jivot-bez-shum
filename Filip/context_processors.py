import json, os, time
from django.conf import settings


def static_build_hash(request):
    """
    Prefer WhiteNoise manifest (staticfiles/staticfiles.json)['hash'].
    Fallback: mtime of STATIC_ROOT; last resort: current time.
    """
    manifest = os.path.join(getattr(settings, 'STATIC_ROOT', ''), 'staticfiles.json')
    build = None
    try:
        with open(manifest, 'r', encoding='utf-8') as f:
            build = json.load(f).get('hash')
    except Exception:
        pass
    if not build:
        try:
            build = str(int(os.stat(getattr(settings, 'STATIC_ROOT', '')).st_mtime))
        except Exception:
            build = str(int(time.time()))
    return {'STATIC_BUILD_HASH': build}

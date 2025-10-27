from django.contrib import admin
from django.urls import path
from shop import views, econt_views

from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path("r3gUp7g5b8xjOw8Eu2E8lONZyxHPectd/", admin.site.urls),
    path("", views.home, name="home"),
    path("checkout/", views.checkout_info, name="checkout_info"),
    path("checkout/payment/", views.checkout_payment, name="checkout_payment"),  # hidden for now
    path("checkout/thank-you/", views.thank_you, name="thank_you"),

    # Stripe (disabled for now)
    path("pay/stripe/create-session/", views.stripe_create_checkout_session, name="stripe_create_session"),
    path("pay/stripe/webhook/", views.stripe_webhook, name="stripe_webhook"),

    # ECONT
    path("api/econt/cities/", econt_views.api_econt_cities, name="api_econt_cities"),
    path("api/econt/offices/", econt_views.api_econt_offices, name="api_econt_offices"),
    path("econt/collect/", econt_views.econt_collect, name="econt_collect"),
    path("delivery/econt/address/", econt_views.econt_collect_address, name="econt_collect_address"),
    path("delivery/econt/office/", econt_views.econt_collect_office, name="econt_collect_office"),
    path("econt/submit/", econt_views.econt_submit, name="econt_submit"),
]
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    # (You usually don't need STATIC_URL here because django.contrib.staticfiles handles it in dev)

from django.http import HttpResponse


def test_mail(request):
    from django.core.mail import send_mail
    send_mail(
        "SMTP test",
        "If you see this, SMTP works.",
        "zhivotbezshum@gmail.com",
        ["philipstoyanov@icloud.com"],
        fail_silently=False,
    )
    return HttpResponse("ok")

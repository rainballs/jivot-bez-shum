# Operating filyaka.com (VPS notes)

The deploy script itself lives only on the VPS and is not part of this repository.

## 1. One-time setup on the VPS: shipment scheduler + health check
Without the shipment timer the retry fallback does not exist (only the best-effort attempt inside the web request).
Replace `USER` with the account that runs gunicorn (`systemctl show -p User --value gunicorn`).

`/etc/systemd/system/filyaka-shipments.service`
```ini
[Unit]
Description=Filyaka - create pending Econt shipments and resend missing order e-mails
After=network-online.target postgresql.service

[Service]
Type=oneshot
User=USER
WorkingDirectory=/srv/jivot-bez-shum/app
ExecStart=/srv/jivot-bez-shum/venv/bin/python manage.py process_shipments
TimeoutStartSec=300
```
`/etc/systemd/system/filyaka-shipments.timer`
```ini
[Unit]
Description=Run Filyaka shipment dispatcher every minute

[Timer]
OnBootSec=60
OnUnitActiveSec=60
AccuracySec=5s

[Install]
WantedBy=timers.target
```
`/etc/systemd/system/filyaka-health.service`
```ini
[Unit]
Description=Filyaka - production health check (read-only)
After=postgresql.service

[Service]
Type=oneshot
User=USER
WorkingDirectory=/srv/jivot-bez-shum/app
ExecStart=/srv/jivot-bez-shum/venv/bin/python manage.py check_production
```
`/etc/systemd/system/filyaka-health.timer`
```ini
[Unit]
Description=Run Filyaka health check every 10 minutes

[Timer]
OnBootSec=120
OnUnitActiveSec=600

[Install]
WantedBy=timers.target
```
```bash
systemctl daemon-reload
systemctl enable --now filyaka-shipments.timer filyaka-health.timer
systemctl list-timers 'filyaka-*'
journalctl -u filyaka-shipments -n 20 --no-pager
```

## 2. Suggested additions to the VPS deploy script
* before `migrate`:  `"$PYTHON" manage.py check_production --config-only || die "Fix the FAIL lines in .env"`
* after the health checks: POST an unsigned request to `/pay/stripe/webhook/` and expect HTTP **400**
  (500 = webhook secret missing; 301/403/404 = nginx or routing problem)
* at the end: `"$PYTHON" manage.py check_production` (read-only report; warnings do not fail)

## 3. Daily operation
| What | Command (in `/srv/jivot-bez-shum/app`, venv python) |
|---|---|
| Is everything healthy? | `python manage.py check_production` |
| Orders needing attention | Django admin -> Orders -> filter *needs review* / *shipment status* |
| Historical / consistency report (read-only) | `python manage.py reconcile_orders --econt --stripe --since 400 --csv /root/recon.csv` |
| Link an existing Econt label / re-queue after manual check | `python manage.py resolve_shipment ID --adopt NUMBER` / `--retry --confirm-no-label-in-econt` (dry-run unless `--yes`) |

## 4. nginx: send `www` to the apex domain
`https://www.filyaka.com` currently serves the site itself. Stripe returns customers to `https://filyaka.com`
(`SITE_URL`), so a customer who started on `www` loses their session cookie on the way back. Add (certificate must
cover `www`):
```nginx
server {
    listen 443 ssl;
    server_name www.filyaka.com;
    # ssl_certificate ... (same as the main server block)
    return 301 https://filyaka.com$request_uri;
}
```
Then `nginx -t && systemctl reload nginx`.

## 5. Stripe dashboard (live mode)
Developers -> Webhooks -> endpoint `https://filyaka.com/pay/stripe/webhook/` with events
`checkout.session.completed`, `checkout.session.async_payment_succeeded`, `checkout.session.async_payment_failed`,
`checkout.session.expired`; its signing secret is `STRIPE_WEBHOOK_SECRET`. After the first real order the delivery
list must show **200**.

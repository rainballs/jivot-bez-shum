# Order processing, Stripe, Econt and security review

Scope: `filyaka.com` Django store (Django 5.2.6, PostgreSQL, Stripe Checkout, Econt JSON API, no Celery/worker).
Everything below was derived from the code in this repository; where a statement rests on evidence other than the
code it says so. **Production was not touched** (no production access existed). Evidence labels:

* **[code]** read in the repository · **[test]** reproduced/guarded by an automated test (mocked providers)
* **[demo]** observed against the Econt **demo** server (`demo.econt.com`, `validate`/`calculate` modes plus a few
  `create` calls whose labels were deleted again) · **[prod?]** cannot be proven without production evidence

---------------------------------------------------------------------------------------------------------------------
## 1. Architecture and (old) order flow

```
GET /checkout/  -> creates Order row (payment_method defaults to COD)            views.checkout_info
radio / qty / address changes -> fire-and-forget autosave POSTs                   views.checkout_save_inline
inline address submit -> Econt "calculate" -> shipping_bgn/eur                    econt_views.econt_submit_inline
summary page:  card -> GET link to Stripe session | COD -> POST confirm          views.checkout_summary
  card: Stripe Checkout (EUR) -> success_url=/checkout/thank-you/?session_id=
        webhook checkout.session.completed -> paid=True + create_econt_label()   views.stripe_webhook
        thank_you() -> retrieve session -> paid=True + create_econt_label()       views.thank_you
  cod : checkout_confirm_cod -> create_econt_label() -> e-mail
  legacy: econt_submit (POST) -> create_econt_label()                             econt_views.econt_submit
```

Old sources of truth: payment = `Order.paid` (bool) **and** the mutable `Order.payment_method`; the Econt COD decision
used **only** `payment_method` (`create_econt_label`: `is_cod = pm == COD`), never `paid`. There were three places that
could create a label (webhook, `thank_you`, `econt_submit`/`checkout_confirm_cod`) with three different payload inputs
(webhook: no overrides; `thank_you`: re-split street; forms: POSTed overrides).

Stripe collects **goods + delivery** (two line items: product × qty and "Доставка" = the Econt quote) → card orders are
fully prepaid. Intended matrix (unchanged business policy, now enforced in code):

| Verified state | Econt `services.cdAmount` (COD) | `paymentReceiverMethod` | Who pays Econt's courier charges | Recipient asked to pay |
|---|---|---|---|---|
| Card, **payment verified** (`payment_status=paid`) | none | none | **sender** (shop; customer already paid delivery to Stripe) | **nothing** |
| COD, customer confirmed | goods subtotal only (qty × unit), BGN | `CASH` | **recipient** (+ Econt COD fee) | COD amount **+** courier charges (not duplicated) |
| Card pending / failed / expired / abandoned | — nothing is created — | | | |
| Paid by card **and** stale/other method = COD | none (paid wins) | none | sender | nothing (+ operator flag if a label already existed) |

[demo] verified with real `create` calls: card-style label → `senderDueAmount = total`, `receiverDueAmount = 0`, no `CD`
service; COD-style label → `CD` service, `receiverDueAmount = delivery`, `senderDueAmount = 0`. `paymentReceiverAmount`
had **no effect** (identical result with/without it) and the undocumented `"payer"` key is **ignored entirely**.

---------------------------------------------------------------------------------------------------------------------
## 2. Findings (ordered by severity)

| # | Sev. | Finding | Evidence | Fix |
|---|---|---|---|---|
| F1 | **High** | **Ship without paying.** `thank_you` created an Econt label for the session's order on *any* `?session_id=…` (verification errors were only logged), and `econt_submit` created labels for unpaid card orders. Anyone could get goods shipped free. | [code] old `views.thank_you`, `econt_views.econt_submit` · [test] `NoLabelWithoutPaymentTests` | Only `fulfillment.attempt_shipment` creates labels; requires verified payment or confirmed COD. `thank_you` never ships inline. |
| F2 | **High** | **IDOR / customer-data exposure.** `_get_current_order` accepted `order_id` from GET/POST; `/checkout/summary/<int:id>/` rendered any order (name, e-mail, phone, address) by sequential id; save/update endpoints could modify other people's orders. | [code] · [test] `AccessControlTests` | Order is found **only** via the server session; id routes removed (404). |
| F3 | **High** | **Root cause #2 (card paid → COD label).** Label type came from the mutable `payment_method`, which defaults to **COD** at order creation, is rewritten by unauthenticated autosave calls (`checkout_save_inline`: *any* value ≠ "card" → COD), is **not** bound to the Stripe session, and is **not** touched when Stripe confirms payment. Switching to COD after a Stripe session was opened (back button / second tab) and paying that still-open session → `paid=True` + COD label. The "label already exists" guard then prevents any correction. | [code] · [test] `test_paid_order_with_stale_cod_method…`, `test_card_paid_but_stored_method_is_cod…`, `SwitchingAndLatePaymentTests` · [prod?] which variant happened | Decision from **verified** `payment_status`; method is forced to `card` on verified payment; open Stripe sessions are expired when the customer switches; late success after a COD label → flagged, never a 2nd label. |
| F4 | **High** | **Root cause #1a – Friday send-date rejection.** Door deliveries with `sendDate` = Friday are rejected by Econt (HTTP 517 *"Моля, изберете ден за доставка на пратката"*) unless `holidayDeliveryDay` is sent. `_next_workday()` returns **Friday for every order handled on a Thursday**. `calculate` (used for the price) does **not** detect it. Date-dependent ⇒ "some orders silently fail". | [demo] reproduced for every weekday; accepted once `holidayDeliveryDay` is present · [test] | Payload always sends `holidayDeliveryDay` (`workday`, configurable `ECONT_HOLIDAY_DELIVERY_DAY`). |
| F4b | **High** | **Root cause #1d – street spelling and missing block/quarter.** Econt's validator rejects many addresses exactly as customers type them: `ул. Витоша` (София) is "ambiguous – give a quarter" while `Витоша` passes; `ул. Княз Александър` (Пловдив) is "street not found" while `Княз Александър` passes; `ж.к. …` addresses need a block ("Друго"). The form placeholder asks for "ул. / бул.", so a large share of door addresses failed at label creation *after* payment. | [demo] `validate` matrix · [browser] reproduced in the checkout UI | Street spellings are tried against Econt **before** payment and the accepted one is stored; new optional fields *Квартал* and *Блок / друго* (`receiver_quarter`, `receiver_other`, migration 0007); Econt's own message is shown to the customer. |
| F5 | **High** | **Root cause #1b – webhook builds a different (invalid) payload.** The webhook called `create_econt_label(order)` with no overrides → street and number not split → Econt rejects (*"Улицата … Нужно е да посочите квартал"*, *"добавите блок…"*); exception swallowed (`except: pass`), `ok:false` ignored; whole label step silently skipped if `order.city`/address fields missing. Entrance/floor/apartment were never persisted. | [demo] address without separate `num` → 517 · [code] | One shared builder from persisted structured fields (`receiver_street/num/entrance/floor/apartment`). |
| F6 | **High** | **Root cause #1c – price quote ≠ validation.** `calculate` accepts addresses that `create` rejects (missing number, ambiguous street). Placeholder shipping (9/7 лв) let customers reach Stripe with **no delivery data at all** → paid order that can never be registered. | [demo] `validate` vs `calculate` · [code] `stripe_create_checkout_session` guard was `shipping_eur > 0` (always true) | Address is checked with Econt `validate` + priced before any payment/COD confirmation; payment refused without a live quote (`delivery_ready`). |
| F7 | **High** | **Silent failure modes in the Econt client.** HTTP 517 bodies discarded (*"HTTP 517 Unknown Status"* – real reason, nested in `innerErrors` with blank top-level `message`, only in a log line); `ok:true` even when no `shipmentNumber` returned; label PDF read from `pdfBase64`, which Econt never sends (it sends `pdfURL`) so PDFs/links never worked; no outcome classification (timeout ⇒ maybe created). | [demo] response shapes · [code] · [test] `ClientParsingTests` | New client: nested error extraction, success requires `shipmentNumber`, outcome classes (rejected / not-sent / **unknown**). |
| F8 | **High** | **Duplicate shipments.** webhook + `thank_you` (+ retries) each created labels; guard read a stale in-memory attribute; no locks. | [code] · [test] sequential; real parallel test needs PostgreSQL – **not run here** | Claim under `SELECT … FOR UPDATE` committed *before* the Econt call; unique constraint on `econt_shipment_num`. |
| F9 | Med | **Wrong destination type.** Office vs address decided by `bool(econt_office_code)`; the code survives a switch back to "address" in `checkout_save_inline` → label to an office instead of the address. | [code] · [test] | `delivery_method` decides. |
| F10 | Med | **Quantity never saved.** The UI autosaves `quantity` to `save-inline`, which has no quantity handling ⇒ server keeps 1 (customer charged for one copy) unless the main form POST happened. | [code] | Validated 1…20 (`MAX_ORDER_QUANTITY`), applied atomically with the submit; Stripe amount from the `OrderItem` snapshot. |
| F11 | Med | **Lost updates.** Full-row `order.save()` from instances loaded before a ≤30 s Econt call overwrote concurrent changes (e.g. payment method). | [code] | `update_fields` saves; final selections travel with the submit. |
| F12 | Low | **Admin delivery preview always blank**: `obj.DeliveryMethod` raised `AttributeError`, which Django silently turns into an empty read-only value (the page itself did *not* crash – an earlier draft of this report wrongly said it did). Operators could still see `econt_errors`, but it only contained "HTTP 517 Unknown Status". | [code] · [test] | Fixed; admin now shows payment/shipment status, attempts, events and review flags, filterable. |
| F13 | Med | **Stripe handling gaps:** no amount/currency/order verification; no event de-duplication; handler swallowed all exceptions and still answered 200 (event lost forever); success/cancel URLs built from the request host (plain `http` without proxy header; arbitrary allowed hosts); product price re-read instead of snapshot. | [code] · [test] | `payments.py` (see §4). |
| F14 | Med | **PII in logs:** full Econt request/response (name, phone, address) printed/logged at ERROR for every call. | [code] | Payloads/bodies never logged; audit rows hold a PII-free summary. |
| F15 | Med | **Abuse:** every anonymous `GET /checkout/` inserts an Order; Econt nomenclature proxies unthrottled/uncached and echoed exception text; Stripe session creation was a state-changing **GET** (CSRF-able). | [code] · [test] | Per-IP throttles, caching, generic errors, POST-only. |
| F16 | Med | **Errors leaked to customers** (`f"…Stripe: {e}"`, `str(e)` JSON). | [code] · [test] | Generic messages, details only in logs. |
| F17 | Low–Med | **Settings:** `SECRET_KEY` falls back to `"dev-secret-key"`; Econt base URL silently falls back to the **demo** server; no Secure/HttpOnly/SameSite cookie settings; `MEDIA_URL="media/"` (relative); admin path hard-coded in repo. | [code] | Fail-fast `SECRET_KEY`; system checks `shop.W001–W004`; cookie flags; `ADMIN_URL` env. |
| F18 | Low | **Label PDFs** (name/phone/address) were linked publicly from the thank-you page with guessable file names (in practice never stored, because Econt returns `pdfURL`, not `pdfBase64`). | [code] | Link removed; `pdfURL` kept admin-only. |
| F19 | Low | **XSS hardening:** Econt names/addresses interpolated into `innerHTML`. Django templates were already auto-escaping. | [code] | `escHtml()` for all interpolations. |
| F20 | Info | `db.sqlite3.backup` tracked in Git (contains **no** shop tables/PII – checked; now untracked + ignored); `econt_probe` command imported a non-existent function (removed); stray `shop/tests.py`/`shop/test/test.py`. | [code] | cleaned |
| F21 | **High** | **Vulnerable dependencies.** `pip-audit` on the old `requirements.txt`: **Django 5.2.6 – 32 advisories** (incl. ORM SQL-injection issues in `QuerySet.annotate/alias/filter` and `FilteredRelation`, admin permission bypasses, multipart parser DoS, cache-header issues; all fixed in 5.2.17), Pillow 11.3.0 (18, via admin image upload), urllib3 2.5.0 (6), sqlparse 0.5.3 (6), requests 2.32.5, idna 3.10, lxml 6.0.2 (now unused and removed). | [tool] `pip-audit` | `requirements.txt` rewritten (UTF-8): Django 5.2.17, Pillow 12.3.0, urllib3 2.8.0, requests 2.34.2, sqlparse 0.6.0, idna 3.15, certifi 2026.7.22; `pip-audit` now reports **no known vulnerabilities**; full suite passes on the new set (clean venv built from the file). stripe 9.9.0 left as is (not flagged; major upgrade should be separate). |
| F24 | Med | **Rate limiter would lock out the shop behind a reverse proxy** (found in the final code review): REMOTE_ADDR is the proxy for every visitor, so per-IP limits became site-wide. | [review] · [test] | Loopback/private sources get a ×50 limit unless `TRUST_X_FORWARDED_FOR` supplies the real client IP (last entry appended by our proxy). |
| F25 | Low–Med | Other review findings fixed: `release`/expire treated a still-open Stripe session as expired; sweeper died on the first exception; `thank_you` hit the Stripe API without limit and could drop an unrelated in-progress order; form POST path bypassed the order-creation limit; failed confirmation e-mails were never retried. | [review] · [test] | all fixed with regression tests (mutation-checked). |
| F22 | Info | Customer e-mail template hard-codes *"Очаквайте доставка след 30.11.2025"* (stale). Not changed (business text). | [code] | — |
| F23 | Info | The (git-ignored, never committed – verified in history) `.env` on this machine contains **live** Econt credentials and `settings` use the `*_LIVE_*` variables, so a local `runserver` hits live Econt. | [code] | `ECONT_ENV=demo` switch. |

Not confirmed / deliberately **not** claimed: SQL injection (ORM only), unsafe deserialization, SSRF (the only outbound
URLs are fixed provider endpoints; `pdfURL` is stored, never fetched), open redirects (none: all redirects are named
URLs). `DEBUG`/`ALLOWED_HOSTS`/proxy/TLS of production could not be inspected.

---------------------------------------------------------------------------------------------------------------------
## 3. Root cause analysis of the two reported problems

### Problem 1 – orders silently not registered in Econt
Confirmed contributing defects (any one of them is sufficient to lose a label):
1. **Friday send date (F4)** – deterministic by weekday; every address-delivery label attempted on a **Thursday** is
   rejected by Econt. *Falsifiable prediction for production:* gunicorn error log lines `ECONT ◀ 517 … изберете ден за
   доставка` clustered on Thursdays (the old code logged every Econt response at ERROR level).
2. **Webhook payload without street number (F5)** and **no delivery data required before paying (F6)**.
3. Failures were swallowed/invisible: webhook `except: pass`, discarded 517 bodies, `ok:true` without a number (F7).
   `econt_errors` only held *"Econt error: HTTP 517 Unknown Status"*, and nothing surfaced unregistered paid orders.
4. Dependence on the customer returning to `thank_you` for the only "full" attempt (closed tab / in-app browser).
5. Timeouts: 30 s read timeout, outcome unknown ⇒ maybe a label exists while the order shows none (now explicit state).

Remaining uncertainty [prod?]: the relative weight of 1–5, and whether some failures are Econt-side (sender/COD
agreement, office ids). Needed evidence is listed in §9.

### Problem 2 – card-paid orders requesting COD
The Econt payload for a card order **cannot** contain COD (no `cdAmount`, no `paymentReceiverMethod`); so a COD label can
only come from the order being **COD at label time** or from a **pre-existing COD label** (F3). Mechanisms confirmed in
code/tests: COD default + unauthenticated mutation of `payment_method`; Stripe session not bound to the method (switch
to COD → pay the old session); webhook/return page setting `paid=True` without correcting the method; guard that blocks
re-labelling. Also possible [prod?]: an e-Econt **account-level default** adding COD – to be excluded by inspecting an
affected label (§9). The old code is also consistent with "COD + paid" being **detectable historically**: only Stripe
paths ever set `paid=True`, so `paid AND payment_method='cod'` identifies suspects (reconcile command, local evidence).

---------------------------------------------------------------------------------------------------------------------
## 4. What was implemented

**State model** (`shop/models.py`, migration `0006`): `payment_status` (unpaid/pending/paid/failed) separate from
`shipment_status` (none/pending/in_progress/created/failed/unknown); validated transitions; `public_id` UUID;
`cod_confirmed_at`, `shipping_quoted_at`, `needs_review/review_reason`, `notified_at`, `econt_cod_amount/currency`,
`econt_receiver_pays_delivery` (what we actually asked Econt for); structured receiver address; new tables
`PaymentAttempt`, `StripeEvent` (webhook dedup), `OrderEvent` (audit), `ShipmentAttempt` (one row per Econt call, written
**before** the call). DB constraints: unique non-empty `econt_shipment_num`; `shipment_status='created'` ⇒ number present.

**Stripe** (`shop/payments.py`, `views.stripe_webhook/thank_you`): expected EUR-cent amount computed server-side from the
`OrderItem` snapshot + quoted shipping and stored on a `PaymentAttempt` before redirecting; webhook = POST only, `csrf_exempt`
for that route only, signature verified on the raw body, durable `StripeEvent` row, verification of mode/payment_status/
currency/amount/order association/livemode, idempotent under duplicates and out-of-order events, `500` on infrastructure
errors so Stripe retries, `200` for verified-but-rejected events (flagged on the order); session TTL 1 h; the return page
uses the same verified function as a fallback and never ships inline. Switching payment method or returning to edit
expires the open Stripe session first.

**Econt** (`econt_client.py`, `fulfillment.py`, `econt_service.py`): one payload builder; `validate`+`calculate` before payment;
strict response handling and outcome classes; claim → call → persist with every failure window ending in a visible state
(`unknown` is **never** auto-retried; bounded back-off only when the request provably never left; stale claims →
`unknown`; Econt-accepted-but-local-save-failed → `unknown` + CRITICAL log with the shipment number); post-create
self-check compares Econt's answer (`CD` service, `receiverDueAmount`) with intent and flags/alerts mismatches.

**Operations:** `process_shipments` (sweeper; run every minute), `reconcile_orders` (read-only report),
`resolve_shipment` (dry-run-by-default operator tool: `--adopt NUMBER` / `--retry --confirm-no-label-in-econt`), admin
columns/filters/inlines/actions, admin alert e-mails, system checks.

**Security:** see F1–F2, F9–F19 above. Throttles use the django cache (per-process `LocMemCache` by default; use Redis/
Memcached or accept per-worker limits).

Files: new `shop/{fulfillment,payments,checkout,address}.py`, `shop/management/commands/{process_shipments,
reconcile_orders,resolve_shipment}.py`, `shop/migrations/0006_…, 0007_…`, `shop/tests/*`, `Filip/test_settings.py`; rewritten
`shop/{views,econt_views,econt_client,econt_service,utils,admin,models}.py`, `requirements.txt`; edited `Filip/{settings,urls}.py`,
`shop/{forms,apps}.py`, templates `checkout/{info,summary_readonly,thank_you}.html`, `econt/office.html`,
`emails/order_admin.txt`; removed `econt_probe.py`, `shop/tests.py`, `shop/test/test.py`, tracked `db.sqlite3.backup`.

---------------------------------------------------------------------------------------------------------------------
## 5. Tests (what ran, what is mocked, what is verified for real)

```
python manage.py test shop --settings=Filip.test_settings
  SQLite      : 148 tests OK (3 skipped = PostgreSQL-only concurrency)
  PostgreSQL16: 148 tests OK, 0 skipped      (TEST_DB=postgres ...)
```
* **Automated, mocked providers:** Econt (`requests.Session.post` fake with response shapes copied from the demo server) and Stripe API calls are patched; **webhook signatures are real** (HMAC verified by `stripe.Webhook.construct_event`). Real network is blocked in tests; fake credentials are forced by `Filip/test_settings.py`.
* **Real PostgreSQL 16 (embedded, throw-away):** the three row-lock concurrency tests (8 parallel identical webhooks; 8 parallel shipment attempts; webhook racing the browser return) pass, 12 repeated runs without flakiness. **Mutation-proven:** without the claim lock the test produces 5 duplicate labels; without the payment lock it records the payment 7 times.
* **Real Stripe test mode (API only, no card entered):** real `Session.create` with our parameters (amount 3150 = 2×12.78 + 5.94, metadata, `client_reference_id`, 1 h expiry, success/cancel URLs), double-click reuse, real `expire`, and an unpaid session is not accepted as payment.
* **Real Econt demo server:** `validate/calculate/create/deleteLabels` evidence in §1–§3; the final code path created one prepaid and one COD label (demo), passed the post-create intent check, labels deleted.
* **Real browser run (local server, SQLite, Econt demo, Stripe test):** address checkout with `ул. Витоша 12` (rejected by Econt as typed → accepted as `Витоша`, stored structured), quantity 2 saved, live price on the summary, card payment → redirect to `checkout.stripe.com` (real session), then signed webhook simulation: forged signature → 400, wrong amount → rejected + flagged, genuine event → paid + **prepaid** demo label (no COD, recipient owes 0), duplicate → no-op, return page shows "Платена" without a public label link; COD with office pickup via the real office-picker JS → one COD label (25.00 BGN, recipient pays delivery), double-click produced one request. Demo labels deleted.
* **Upgrade rehearsal on PostgreSQL:** legacy-schema database with 6 orders → `migrate` → unique `public_id` per row, statuses backfilled, nothing flagged or altered; `reconcile_orders` then flagged exactly the two suspect patterns.
* **Dependency audit:** `pip-audit` clean on the new `requirements.txt`; `pip check` clean.
* **Mutation checks:** each original defect and each review fix, when re-introduced, makes a test fail.
* **Still not verified:** a *paid* Stripe Checkout end to end with a real card (card entry on a third-party page was not performed; the paid event was simulated with a correctly signed payload); live Econt behaviour of COD agreement `CD250332` (absent on demo); behaviour behind your actual nginx; Safari/mobile browsers.

---------------------------------------------------------------------------------------------------------------------
## 6. Required settings / provider configuration

| Item | Action |
|---|---|
| `SECRET_KEY`, `DEBUG=False`, `ALLOWED_HOSTS` | must be set in the production environment (the app now refuses to start without `SECRET_KEY`) |
| `SITE_URL=https://filyaka.com` | used for Stripe success/cancel URLs (no longer the request host) |
| Stripe | live keys in `STRIPE_SECRET_LIVE_KEY`/`STRIPE_PUBLIC_LIVE_KEY`; **webhook endpoint** `https://filyaka.com/pay/stripe/webhook/` with events `checkout.session.completed`, `…async_payment_succeeded`, `…async_payment_failed`, `…expired`; its signing secret → `STRIPE_WEBHOOK_SECRET`. Check Dashboard → Webhooks for failed deliveries (HTTP 301/400/500) in the past. |
| Econt | `ECONT_LIVE_BASE_URL=https://ee.econt.com/services` (+ credentials); optional `ECONT_HOLIDAY_DELIVERY_DAY` (`workday`/`Halfday`), `ECONT_COD_CURRENCY`, `ECONT_COD_AGREEMENT_NUMBER`; developers: `ECONT_ENV=demo` |
| Proxy | if nginx terminates TLS: `USE_X_FORWARDED_PROTO=True` (+ `proxy_set_header X-Forwarded-Proto $scheme`), `CSRF_TRUSTED_ORIGINS=https://filyaka.com,https://www.filyaka.com`, `TRUST_X_FORWARDED_FOR=True` only if nginx appends to `X-Forwarded-For`; `SECURE_HSTS_SECONDS` only after verifying HTTPS everywhere |
| **Scheduler** | run `python manage.py process_shipments` **every minute** (cron/systemd timer) or `--loop` under a process manager. Without it only the inline best-effort attempt exists. |
| Cache | use a shared cache (Redis/Memcached) for accurate rate limiting across gunicorn workers |
| Media | confirm `media/` (incl. `econt_labels/`) is **not** publicly served by nginx |

Business decisions left open: COD currency (BGN today vs EUR; Econt now answers in EUR – BGN→EUR rounding can differ by
a cent from the displayed EUR price), Saturday delivery (`Halfday`) vs Monday (`workday`), the stale e-mail text (F22).

---------------------------------------------------------------------------------------------------------------------
## 7. Safe deployment and rollback

1. `pg_dump` the production database (**keep it until the new version has run for a few days**).
2. Pre-flight (read-only SQL): duplicate shipment numbers would stop the migration with a clear message listing them:
   `SELECT econt_shipment_num, array_agg(id) FROM shop_order WHERE econt_shipment_num <> '' GROUP BY 1 HAVING count(*)>1;`
3. Set the environment (§6), `pip install -r requirements.txt` (security upgrades, see F21), `python manage.py migrate` (migrations 0006+0007 only **add** columns/tables and neutral
   backfill: unique `public_id` per row, `payment_status` from `paid`, `shipment_status=created` where a number exists;
   nothing is flagged, no customer/payment/shipment data is modified), `collectstatic`, restart gunicorn.
4. Install the `process_shipments` timer; run `python manage.py check --deploy` (look at `shop.W00x`).
5. Run the read-only report (§8) **before** any repair.
6. Smoke test in Stripe **test mode** first if possible: card order (verify one label, no COD), COD order, switch card→COD,
   cancel → edit → pay.
7. Rollback: deploy the previous code and `migrate shop 0005` (reverses the additions; keep the dump – new audit tables are
   dropped). **Do not roll back lightly**: the old code contains F1–F8. Prefer fixing forward.
8. Dependencies are already upgraded in this change (F21) and audited; keep `pip-audit` in your routine.

---------------------------------------------------------------------------------------------------------------------
## 8. Historical reconciliation (dry-run first, read-only)

```
python manage.py reconcile_orders                       # local consistency only, no network
python manage.py reconcile_orders --econt --stripe --since 400 --csv /tmp/recon.csv
```
Output has order ids, statuses, shipment numbers and codes only (no names/e-mails/phones/addresses). Evidence is labelled
**LOCAL** (our DB) or **VERIFIED** (Econt `getMyAWB` / Stripe `checkout.sessions.list`, read-only). Codes:
`PAID_NO_SHIPMENT`, `SHIPMENT_FAILED`, `SHIPMENT_UNKNOWN`, `STALE_IN_PROGRESS`, `PENDING_OVERDUE`, `PAID_BUT_METHOD_COD`
(legacy suspects for problem 2), `COD_REQUESTED_ON_PAID`, `LABEL_WITHOUT_PAYMENT`, `DUPLICATE_SHIPMENT_NUM`,
`MULTIPLE_LABELS_CREATED`, `FLAGGED`; with `--econt`: `ECONT_COD_ON_PAID_ORDER`, `ECONT_RECEIVER_PAYS_ON_PAID_ORDER`,
`ECONT_NO_COD_ON_COD_ORDER`, `ECONT_NUMBER_NOT_FOUND`; with `--stripe`: `STRIPE_PAID_LOCAL_UNPAID`,
`STRIPE_MULTIPLE_PAYMENTS`, `LOCAL_PAID_NO_STRIPE_SESSION`, `PAID_FLAG_WITHOUT_STRIPE_PAYMENT`.

**Proposed repairs (each needs explicit approval; nothing is automatic):**

| Class | Proposal | How duplicates / extra charges are avoided |
|---|---|---|
| Paid, no label | Look the order up in e-Econt (receiver phone/date). If a label exists: `resolve_shipment ID --adopt NUMBER --yes`. If none: `resolve_shipment ID --retry --confirm-no-label-in-econt --yes` then `process_shipments`. | `--retry` is refused when a number exists; one attempt is queued; the claim/unique constraint make a second create impossible. |
| `UNKNOWN` | Same as above; the report never auto-retries. | Operator confirms absence first. |
| Card-paid with COD label (`ECONT_COD_ON_PAID_ORDER`) | **Do not create another label.** In e-Econt remove/zero the COD amount (or have Econt do it) **before** delivery; check `cdCollectedAmount` via `getShipmentStatuses` first. If COD was already collected: refund the customer in Stripe **or** Econt payout reversal – a business choice. | Customer never pays twice; no new label. |
| Shipped without payment (`LABEL_WITHOUT_PAYMENT`) | Review individually (stop shipment in e-Econt if still possible, contact customer). | — |
| Duplicate/multiple labels | Cancel the surplus in e-Econt manually (`deleteLabels`/UI) after confirming which one is in transit. | — |
| `STRIPE_PAID_LOCAL_UNPAID` | The webhook/return page will not repeat; use `resolve_shipment --retry` after checking the Stripe payment is real. | Verified against Stripe before shipping. |

---------------------------------------------------------------------------------------------------------------------
## 9. Evidence still needed from production (cannot be proven from here)

1. Gunicorn error log: lines `ECONT ◀ 517` / `Econt label after Stripe failed` – tally the Econt messages and weekdays
   (tests F4/F5/F6). Also `Stripe verify on thank_you failed`.
2. `SELECT id, created_at, payment_method, paid, econt_errors FROM shop_order WHERE econt_errors IS NOT NULL OR (paid AND econt_shipment_num IS NULL);`
3. `SELECT id, created_at FROM shop_order WHERE paid AND payment_method='cod' AND econt_shipment_num IS NOT NULL;` (problem-2
   suspects) and the matching labels in e-Econt (COD amount, "платец").
4. One **card-paid** historical label in e-Econt: does it show COD although our payload had none? (excludes an
   account-level default).
5. Stripe Dashboard: webhook endpoint URL/secret/events and delivery history; sessions paid but with the order's
   method = COD.
6. nginx config (HTTPS redirect, `X-Forwarded-*`, `/media/` exposure), production `.env` (DEBUG, SECRET_KEY, ALLOWED_HOSTS).
7. Approval to run a real COD + card test shipment (or Econt confirmation) for the COD agreement `CD250332`.

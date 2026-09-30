# STAX_CLIENT.md — the one door to Stax

**Date:** 2026-09-30 · **Plan:** `docs/qa/PLAN-005-stax-client.md` · **QA:** `docs/qa/QA-005-stax-client.md`
**Code:** `backend/payments/provider.py` (the interface), `backend/payments/stax/` (the only
Stax-specific code), `backend/payments/fake.py` (in-memory provider for tests and local work).

## 1. Why this module exists

Stax blocks the calling IP address for **one hour** after 10 failed requests in one minute, and
counts every request against a per-minute ceiling (200 on one page of its docs, 100 on
another; we design to the stricter figure, TODO-VERIFY-001). A loop with a bug, or a batch of
operators with malformed emails, is enough to take the business offline for an hour. Every
Stax call therefore passes through one transport that limits, breaks, validates and logs.

## 2. The limiter

- Two token buckets, both of which must release a token before a request leaves:
  **2 per second** and **100 per minute**. That is half of Stax's published limit.
- A spent token returns exactly one period after it was spent, so no one-second window ever
  holds more than 2 departures and no minute more than 100. A burst of 50 calls takes about
  25 seconds; the tests prove it with recorded departure times.
- A caller waits at most 30 seconds for a slot, then gets `RateLimitWaitExceeded` and sends
  nothing. Waiting callers are served in order; none can be starved past its allowance.
- The bucket is **process-wide**. Stax's limit is per source IP and every replica leaves
  through the same NAT address, so a bucket per process is only correct if **one process
  calls Stax**. That is enforced by configuration, not convention (§5).

## 3. The circuit breaker

- Opens after **3 consecutive 4xx responses** (429 excepted, it is the limiter's signal), or
  after **5 failures of any kind within 60 seconds**.
- Open means every call raises `CircuitOpen` **without touching the network** for
  **5 minutes**. Then one trial call is allowed. Success closes the breaker; failure re-opens
  it for another 5 minutes.
- Its state is kept in memory and in the `sync_state` table (row `stax_breaker`), so it
  survives a restart and the dashboard (Prompt 11) can show it.
- Each opening fires the alert exactly once (§6).

## 4. Retries, validation and the failure budget

**Retries.** Only on a network error, a 429 or a 5xx, with waits of 1 s, 4 s and 15 s, four
attempts in all. **A 4xx other than 429 is never retried**: it means our request was wrong, and
retrying it would only spend the failure budget. The three creates (customer, invoice,
schedule), refunds and offline payments are retried after a 429 only, because Stax may have
acted on a request whose response was lost; their recovery path is check-before-create.
The HTTP library's own retries are set to 0 and redirects are not followed, so no retry hides
beneath ours.

**Validation before sending.** Email syntax, positive two-decimal amounts, `total = subtotal +
tax`, line items summing to the subtotal, a UUID `idempotency_id`, an RRULE that begins with
`DTSTART=`, date ranges that run forwards and span at most a year, and identifiers made of
letters, digits, hyphen and underscore. A request that fails these never reaches Stax, so its
failure never counts against us.

**Failure budget.** Stax also describes a merchant-level block after 50 failed transactions in
an hour (TODO-VERIFY-014). The budget counts failed HTTP calls **and** failed payment attempts
(a returned ACH, reported by Prompt 8) in sliding windows of one minute and one hour. A debit
is refused when the last minute holds **5** failures or the last hour **25**, half of Stax's
figures. The monthly run (Prompt 7) must ask the budget before each debit and pace itself; at
2 requests per second the day-1 batch of about 166 debits takes under two minutes of calls,
spread as the run decides.

## 5. One bucket for the whole deployment

| Setting | Value | Meaning |
|---|---|---|
| `STAX_CALLER=none` | default; the web app | every provider call raises `ProviderNotPermittedHere` before any network activity |
| `STAX_CALLER=worker` | the single worker process (Step 10) | the only process allowed to call Stax |

The web app on Azure Container Apps may run up to two replicas. Neither may call Stax. When
the Celery worker arrives at Step 10 it runs with `STAX_CALLER=worker`, **one replica**, and
that single-replica rule goes into the Bicep template then. Until Step 10, the only permitted
caller is a one-off command run by the owner with the variable set for that one process (§8).

## 5a. The boot-time identity check (PLAN-006)

Every process start runs `provider_environment_check` after the migrations and before
gunicorn. It sends one `GET /self` on a boot path that bypasses only the `STAX_CALLER` gate,
then refuses to start (gunicorn never runs) when `merchant.is_sandbox` disagrees with
`STAX_ENV` (exit 3), when `STAX_EXPECTED_MERCHANT_ID` is set and differs (exit 1), or when Stax
cannot be reached or read (exit 2). Locally with no key it is skipped. A Stax outage therefore
stops a **new** revision from starting; the running revision keeps serving.

**Interim arrangement until Step 10.** The operator screens create and link Stax customers,
so on staging the web process is the single caller: `STAX_CALLER=worker`, one replica
(`webMaxReplicas = 1`) and one gunicorn process with threads. All three are set in the
template and the image, not by hand. Step 10 moves every call to the worker and reverts them.

## 6. When the breaker opens

What happens automatically:
1. A **CRITICAL** log line: "Stax circuit breaker opened at … calls refused until …".
2. A Sentry event at level **fatal** with fingerprint `stax-breaker-open`. Set a Sentry alert
   rule on that fingerprint to email the owner (owner action; Sentry is not yet configured).
3. The `sync_state` row `stax_breaker` records the opening for the dashboard.
4. Every call fails fast for five minutes. No request leaves.

What a person does:
1. **Do not restart the app to "fix" it.** The state is persisted; a restart changes nothing
   and a restart loop would spend the budget.
2. Read the last log lines: `stax method=… path=… status=… failure=…`. Three consecutive
   4xx means our requests are wrong: a bad payload or an expired key. Five mixed failures means
   Stax or the network is unwell.
3. If the key is wrong or expired: rotate it in Key Vault (owner), then wait for the trial call.
4. If our payload is wrong: fix the data (for example an operator email), then wait.
5. If Stax is down: wait. The trial call every five minutes will close the breaker by itself.
6. Check `https://apiprod.fattlabs.com` status with Stax support if the breaker re-opens three
   times in a row.
7. After it closes, the monthly run resumes from where it stopped; nothing is retried by
   itself.

## 7. Logging

One INFO line per request on the logger `payments.stax.transport`:

```
stax method=GET path=/invoice/{id} status=200 ms=123 object_id=<stax id>
```

and `failure=<ClassName>` on failure. Never a body, header, token, email or amount. The
redacting formatter (`finance.log_redaction`) still runs over every line as a second layer.

## 8. Checking authentication (the only call this step makes)

`provider_ping` runs `GET /self`, which creates nothing, and prints the provider name, the
environment, a masked merchant ID and whether it matches `STAX_EXPECTED_MERCHANT_ID` when that
is set. One attempt, no retry loop. Exit 0 on success, 1 on an identity mismatch, 2 on any
other failure. On staging:

```
az containerapp exec -g finance-sandbox -n finance-staging-web \
  --command "sh -c 'STAX_CALLER=worker python backend/manage.py provider_ping'"
```

The sandbox pytest suites (`uv run pytest -m sandbox`) run only with `STAX_SANDBOX_TESTS=1`
and a key in `STAX_SANDBOX_API_KEY`, and only when `STAX_ENV=sandbox`. The pay-an-invoice
suite additionally needs a sandbox customer and payment-method ID and the feature flag; it is
how the owner will decide TODO-VERIFY-019.

## 9. Asking Stax for IP whitelisting

Send to Stax support, from the account owner:

> We call the Stax API from a fixed egress address, `20.12.221.227`, for an internal rent-ledger
> integration on merchant account `<masked id from provider_ping>`. We expect at most 100
> requests per minute and about 450 ACH debits a month, clustered on the 1st, 2nd, 5th and 15th.
> Please confirm: (1) whether this address can be whitelisted or rate limits raised for it,
> (2) which per-minute figure is enforced, 100 or 200, and whether the 10-failed-requests rule
> counts 5xx as well as 4xx, (3) whether the 50-failed-transactions-per-hour block applies to
> our account and whether ACH returns that arrive days later count towards it, (4) the ACH
> per-transaction limit on the account; our largest single debit is $12,581, and (5) whether
> a one-off invoice can be collected from a stored bank method without the payer acting.

These are TODO-VERIFY-010, 001, 014, 009 and 019. Record the answers in `docs/TODO-VERIFY.md`.

## 10. What is not verified against Stax yet

Every path and field name the adapter uses was read from the specification on 2026-09-30
(`docs/inputs/STAX_ENDPOINTS_2026-09-30.md`). Response shapes were not: rows TODO-VERIFY-020 to
030 list each assumption and where it sits in the code. The first sandbox calls in Prompts 6
and 7 resolve them one by one.

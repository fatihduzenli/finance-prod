# PLAN-006 — Operators module, boot-time identity check, two reserved tables

**Date:** 2026-09-30 · **Planner / QA owner:** Fable · **Implementer:** Opus
**Inputs:** Prompt 6 and the owner's three messages of 2026-09-30 (operators corrections; expected
merchant ID and `is_sandbox` boot check; `documents` and `outbound_messages`);
`docs/inputs/PROMPT_SERIES_AMENDMENTS.md` Prompt 6 section; ADR-0004; `docs/DATA_OWNERSHIP.md`;
`docs/STAX_CLIENT.md`; `docs/inputs/STAX_ENDPOINTS_2026-09-30.md`.

## 1. Restated objective

Staff can list, search, view, create, edit and archive **operators** (Prompt 6's "customers").
Creating or editing an operator with an email saves our record first, then looks the email up
in Stax and links an existing Stax customer or creates one through the rate-limited client,
recording the ID in `external_refs`. A failed Stax call leaves a usable local record marked as
pending, with a single manual retry. Double submission yields exactly one Stax customer.
Operators without an email are complete records; staff link their Stax customer and
payment-method IDs by hand after entering the bank method in the Stax dashboard, which sets
enrolment. Every change writes `audit_log`. Separately: the app refuses to start when the Stax
key's `is_sandbox` flag disagrees with `STAX_ENV`; the staging configuration carries the
expected merchant ID; and two empty tables are reserved for documents and outbound messages.

## 2. Affected packages

Root project: `backend/ledger/` (views, forms, templates, urls, services, migrations 0003 and
0004), `backend/payments/` (boot check command, transport boot path, operator sync service),
`backend/accounts/registry.py` (route rules only), `backend/finance/urls.py` (one include),
`backend/finance/templates/` (a base layout), `Dockerfile` (`CMD`), `infra/` (three parameters
and their staging values), tests, docs. **Out of scope:** invoices, leases, insurance screens
(later prompts), Django admin registration (owner: Prompts 6 and 7 as planned; **this plan
registers the operator model read-only in the admin** so staff can see it, nothing else),
webhooks, Celery, any document upload or email sending.

## 3. Decisions taken in this plan

| # | Decision | Reason |
|---|---|---|
| D1 | **Until Step 10, the web process is the single Stax caller:** staging runs with `STAX_CALLER=worker` and `webMaxReplicas = 1`. Both set in Bicep, not by hand. Step 10 moves the calls to the worker and returns the web app to `none` | Prompt 6's acceptance test needs a Stax call from the screen; the single-bucket rule holds with one replica; owner: enforce in configuration |
| D2 | Boot check is a management command `provider_environment_check` in the image `CMD` after `deploy_migrate`. `GET /self` once through the transport on a **boot path that bypasses the `STAX_CALLER` gate** (one call per process start, never retried by the command itself). Mismatch of `merchant.is_sandbox` with `STAX_ENV` → exit 3; mismatch with `STAX_EXPECTED_MERCHANT_ID` when set → exit 1; Stax unreachable → exit 2. **All three refuse to start.** With `APP_ENV=local` and no key, the check prints that it is skipped and exits 0 | Owner: refuse to start on disagreement; unknown is not agreement. A Stax outage therefore blocks a *new* revision from starting while the old one keeps serving |
| D3 | Check-before-create by normalised email uses `find_customers(email=…)` and then compares **each returned record's email, lower-cased and stripped, for equality** in our code. A match links; several matches link the oldest and flag for review; no match creates | The spec offers an `email` filter but does not say whether it is exact. Local equality makes the strategy safe either way (TODO-VERIFY-031). The prompt's stop condition is answered this way, not by stopping |
| D4 | Operators **without an email make no Stax call**. Their sync status is `awaiting_link`. Staff link the Stax customer ID (and payment-method ID) in a form; the app then reads the customer by ID (`get_customer`) to confirm it exists, stores both IDs in `external_refs`, and sets `enrolment_status = enrolled` | Owner corrections of 2026-09-30 |
| D5 | Operator fields sent to Stax: `firstname` = display name, `lastname` = empty, `company` = legal name or display name, `email`, `phone`, address fields. `reference` = our operator UUID (Rule 5) | Minimal, reversible mapping; TODO-VERIFY-004 already covers `reference` |
| D6 | Double-submit: the create form carries a `creation_key` UUID generated when the form is rendered; the operator row stores it (unique). A second POST with the same key returns the existing operator. The Stax step runs inside a row lock (`select_for_update`) and only when `sync_status = pending` and no `external_refs` row exists | Exactly one Stax customer, whatever the client does |
| D7 | New operator columns (migration 0003): `sync_status` (`not_applicable`, `awaiting_link`, `pending`, `linked`, `failed`), `sync_failure` (class name of the last failure, never a message), `sync_attempted_at`, `creation_key` uuid unique null | Prompt 6 item 3; failure text must be value-free |
| D8 | Migration 0004: `documents` and `outbound_messages` exactly as the owner listed them, no UI, no admin | Owner request; Phase 7 items recorded by Fable |
| D9 | Archive is a soft delete (`archived_at`). No delete view exists at all. The admin registration is read-only (no add, change or delete permissions) | Prompt 6: never hard-delete |
| D10 | Roles: `viewer` GET only; `billing` create, edit, retry, link; `admin` archive as well. Every new route is classified in `accounts/registry.py` | PLAN-003 role model |

## 4. Interface contract

### 4.1 Routes (namespace `ledger`), all behind sign-in and MFA

| Name | Path | Methods | Min role for POST |
|---|---|---|---|
| `operators:list` | `/operators/` | GET | — |
| `operators:create` | `/operators/new/` | GET, POST | billing |
| `operators:detail` | `/operators/<uuid>/` | GET | — |
| `operators:edit` | `/operators/<uuid>/edit/` | GET, POST | billing |
| `operators:archive` | `/operators/<uuid>/archive/` | POST | admin |
| `operators:retry_sync` | `/operators/<uuid>/retry-sync/` | POST | billing |
| `operators:link` | `/operators/<uuid>/link/` | GET, POST | billing |

List: search `q` over display name, legal name, company and email (case-insensitive contains),
`archived=1` to include archived, 25 per page, ordered by display name. Detail: contact block,
sync status with the failure class and time, linked Stax IDs (masked to first 4 and last 4),
enrolment status, leases with store label and status, **outstanding balance** = sum of
`rent_charges.balance` over the operator's leases, invoice history table (empty until Prompt 7),
notes. Forms: Django forms with server validation; the email field lower-cases and strips,
rejects malformed input with Django's validator, allows empty; card-number guard on every text
field (already on the model). Client-side: `type=email`, `required` on display name, and the
submit button disabled on first click by a few lines of inline script (the server rule D6 is
what counts).

### 4.2 Service `backend/payments/operator_sync.py`

```
sync_operator(operator_id, *, provider: PaymentProvider, actor) -> SyncOutcome
link_operator(operator_id, *, customer_external_id, payment_method_external_id, provider, actor) -> SyncOutcome
```

`sync_operator`: lock the row; if not `pending` or already linked, return without a call; if
no email → `awaiting_link`; else `find_customers`, compare (D3), link or `create_customer`,
write `external_refs` (`entity_type = operator`, `provider = stax_pay`, `external_kind =
customer`), set `linked`. On `ProviderError`: set `failed`, store the class name and time, no
retry, return the outcome. Every path writes one `audit_log` row (`operator.created`,
`operator.updated`, `operator.archived`, `operator.sync_linked`, `operator.sync_created`,
`operator.sync_failed`, `operator.link_set`). Views call the service; the service is the only
place that talks to the provider. The provider is obtained from `payments.get_provider()`,
which returns `StaxProvider` unless settings select the fake for local development
(`STAX_PROVIDER=fake`, local only, new env guard value).

### 4.3 Boot check and infrastructure

- `backend/payments/management/commands/provider_environment_check.py` per D2.
- `StaxTransport.send(..., boot_check=True)` bypasses only the `STAX_CALLER` gate; limiter,
  breaker, budget and logging still apply.
- `Dockerfile` `CMD`: `deploy_migrate && provider_environment_check && exec gunicorn …`.
- Bicep: `main.bicep` and `containerAppWeb.bicep` gain `staxCaller` (default `none`) and
  `staxExpectedMerchantId` (default empty) as environment variables `STAX_CALLER`,
  `STAX_EXPECTED_MERCHANT_ID`; `infra/parameters/staging.bicepparam` sets `staxCaller =
  'worker'`, `staxExpectedMerchantId = 'ea824f2b-90b3-4c49-8f69-ba06943458f4'` (the sandbox
  merchant ID read from `GET /self` on 2026-09-30, an identifier, not a secret) and
  `webMaxReplicas = 1`. A comment in the param file says why and until when (Step 10).
- `.env.example`: `STAX_PROVIDER` (`stax` default, `fake` local only).

### 4.4 Reserved tables (migration 0004, no UI)

`documents`: `id`, `operator` FK, `kind` text, `source` (`upload`, `docusign`), `file_reference`
text, `external_id` text null, `uploaded_by` FK user, `created_at`.
`outbound_messages`: `id`, `operator` FK, `template` text, `subject` text (guarded),
`document_refs` jsonb default `[]`, `sent_by` FK user, `sent_at` null, `delivery_status` text.
Both guarded by the card-number guard on their text and JSON columns like every other table.

## 5. Work breakdown

| Batch | Task | Files |
|---|---|---|
| 1 | Boot check, transport boot path, Dockerfile, Bicep, `STAX_PROVIDER`, `get_provider()` | `backend/payments/management/commands/provider_environment_check.py`, `backend/payments/stax/transport.py`, `backend/payments/__init__.py` or `registry.py`, `Dockerfile`, `infra/main.bicep`, `infra/modules/containerAppWeb.bicep`, `infra/parameters/staging.bicepparam`, `backend/finance/env_guard.py`, `settings.py`, `.env.example` |
| 2 | Migrations 0003 (operator sync columns) and 0004 (reserved tables), models | `backend/ledger/models.py`, `backend/ledger/migrations/0003_*.py`, `0004_*.py` |
| 3 | Service, views, forms, templates, urls, registry, admin read-only | `backend/payments/operator_sync.py`, `backend/ledger/views/operators.py`, `forms.py`, `urls.py`, `admin.py`, `templates/ledger/…`, `backend/finance/templates/finance/base.html`, `backend/finance/urls.py`, `backend/accounts/registry.py` |
| 4 | Tests | `backend/tests/test_operator_sync.py`, `test_operator_views.py`, `test_provider_environment_check.py`, `test_reserved_tables.py`, `e2e/test_operators.py` (one browser test: create with a double click against the fake provider) |
| 5 | README rows | `README.md` |

## 6. Test plan

| # | Case | Author |
|---|---|---|
| 1 | Double click: two POSTs with the same `creation_key` (sequential and concurrent on PostgreSQL) → one operator, one provider `create_customer` call, one `external_refs` row | Opus |
| 2 | Provider failure (`ProviderUnavailable`, `ProviderRejected`, `CircuitOpen`): local record exists, `sync_status = failed`, failure class stored, detail page shows the message and the retry button; no second call without a click; retry makes exactly one call | Opus |
| 3 | Existing Stax customer with the same email (any case, surrounding spaces): linked, `create_customer` never called; two matches → oldest linked and `needs_review` | Opus |
| 4 | Malformed email: form error, no local record, **zero provider calls** | Opus |
| 5 | No email: record saved, `awaiting_link`, zero provider calls; link form stores both IDs, `get_customer` called once, enrolment set; a bad ID → `ProviderRejected` → clear message, nothing stored | Opus |
| 6 | Roles: viewer GET 200 everywhere, POST 403 everywhere; billing cannot archive; admin can; anonymous 401 | Opus |
| 7 | Archive sets `archived_at`, list hides it unless `archived=1`, no delete route exists, admin has no delete permission | Opus |
| 8 | Audit rows for every action with actor and before/after | Opus |
| 9 | Boot check: sandbox key with `STAX_ENV=sandbox` → 0; `is_sandbox=False` with sandbox → 3; expected ID mismatch → 1; unreachable → 2; local without key → 0 and skipped; the `STAX_CALLER` gate is bypassed only on this path | Opus |
| 10 | Reserved tables exist, are empty, are guarded, and have no URL or admin | Opus |
| 11 | Adversarial: a POST that replays a used `creation_key` with different field values does not overwrite the existing operator; a forged `creation_key` from another user's form; search input with SQL and template syntax; 5,000-character search; paging past the end; archived operator cannot be edited or retried | Fable |
| 12 | Adversarial: provider returns a customer whose email differs only by Unicode case folding or a trailing dot; provider returns a `raw_summary` with bank fields (must be dropped before persisting) | Fable |
| 13 | Adversarial: concurrency of retry and link on the same operator; boot check under a 500 body containing the token | Fable |
| 14 | Whole suite both engines, lint, format, types, secret scan, image build with the new `CMD` against a fake provider | Fable |

## 7. Risk register

| Risk | Treatment |
|---|---|
| Web replica count drifts back to 2 while it is the caller | Bicep parameter with a comment; QA-006 records it; Step 10 plan reverts |
| A Stax outage stops a new revision from starting | Accepted by design (owner: refuse when in doubt). The running revision is unaffected. Recorded in STAX_CLIENT.md |
| Duplicate Stax customers from concurrent creates | D6 row lock and unique key; test 1 on PostgreSQL |
| Orphan in Stax when the local write fails | Local write happens first, in its own transaction, before any call |
| Bank data through the link form | The form takes two Stax IDs only, validated as identifiers; no other field |
| `find_customers` email filter is fuzzy | D3 local equality; TODO-VERIFY-031 |
| Sandbox account has `allow_ach = false` | Not a Prompt 6 blocker (customers only). Flagged to the owner for Prompt 7 |

## 8. Acceptance criteria

1. Lint, format, strict types exit 0; both pytest commands exit 0 on both engines; no
   existing test edited, removed or skipped; the route registry test passes with the new routes classified.
2. Every row of §6 has a passing test.
3. `makemigrations --check --dry-run` reports no changes; migrations roll back cleanly.
4. On staging after the push: creating an operator with an email creates exactly one sandbox
   Stax customer, also under a double click; the ID appears in `external_refs`; a malformed
   email is refused with no request in the transport log. Verified by the owner (Checkpoint 6)
   and by Fable from the logs.

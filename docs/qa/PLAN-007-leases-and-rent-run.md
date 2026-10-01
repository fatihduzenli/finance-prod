# PLAN-007 — Lease setup, autopay enrolment, monthly rent run

**Date:** 2026-10-01 · **Planner / QA owner:** Fable · **Implementer:** Opus
**Inputs:** Prompt 7 and the owner's amendments (2026-10-01, which win on conflict); `docs/SCOPE.md`
§3, §4, §4a, §5; ADR-0004, ADR-0005; `docs/DATA_OWNERSHIP.md`; `docs/STAX_CLIENT.md`;
`docs/inputs/STAX_ENDPOINTS_2026-09-30.md` (updated 2026-10-01); `docs/TODO-VERIFY.md`.

## 1. Restated objective

Two flows replace the invoice builder. **7a:** staff create and edit leases (store, operator,
rent with history, one or two debit days, channel, start date), choose the operator's Stax
**bank** payment method from a list that shows type and last four only, and enable autopay,
which creates one Stax scheduled invoice per debit day with the rent as the amount, autopaying
from that method. Pause and resume map to the schedule's `active` flag. **7b:** a run screen
lists the leases due today, last month's outcome, and the insurance debits due in the next
seven days; it creates one rent charge per lease per period per debit, idempotently, matches
invoices Stax generated from the schedules to those charges, and records manual payments for
wire, Zelle and other leases with a mandatory note. Nothing writes `provider_status` outside the
sync writer. Adjustments are not built here (they are one-off invoices, a later plan).

## 2. Stop conditions of Prompt 7, answered

| Condition | Answer |
|---|---|
| Invoice payload differs materially from the indicative one | **It does, and it is recorded, not stopped on:** `url` is required (value documented, TODO-VERIFY-027 closed); `memo`, `reference`, `internalMemo` sit under `meta`; `lineItems` use `item`, `details`, `quantity`, `price`; dates are `invoice_date_at` and `due_at`. No direct invoice create is made in this plan; schedules generate the invoices. |
| Per-transaction limit not recorded in `docs/STAX_ANSWERS.md` | **Not recorded.** The owner's amendment says build does not wait. The large-debit warning uses a configurable `LARGE_DEBIT_WARNING_AMOUNT` (default `2000.00`, labelled a placeholder on screen until Stax answers Q25). Go-live of the run stays blocked (owner). |
| Where the human-readable invoice number lives | `meta.reference` on an invoice (spec, 2026-10-01). Schedule-generated invoices carry no number of ours; the app assigns `INV-YYYY-NNNN` to its local invoice row when it matches one (owner's format). |
| A schedule field or behaviour not in the spec | Everything the schedule needs is in the spec: `url`, `total`, `rule`, `customer_id`, `payment_method_id`, `email_notification`, `meta.lineItems`, `active`. **Two points are not and are recorded as TODO-VERIFY, not stopped on:** whether schedule `meta` accepts `memo`/`reference` (035), and how a monthly RRULE behaves on day 29 to 31 (036; no such debit day exists today). |

## 3. Decisions

| # | Decision | Reason |
|---|---|---|
| D1 | `PaymentProvider` gains `list_payment_methods(customer_external_id) -> list[PaymentMethodRecord]` and `get_payment_method(external_id)`. The record holds `external_id`, `method` (`bank` or `card`), `last_four`, `bank_type`, `nickname`, `is_default`, `raw_status`; **nothing else** of the object is kept | Amendment 7a; Rule 1 |
| D2 | The enrolment screen shows only bank methods; a card method is listed as "card, not allowed" and cannot be selected; the service refuses a non-bank method ID even if posted directly | No cards |
| D3 | One `collection_schedules` row and one Stax schedule per debit day: `FREQ=MONTHLY`, `DTSTART` = next occurrence of that day at 12:00:00Z strictly in the future, `total` = rent for monthly leases or rent ÷ 2 for twice-monthly (cents split: the first debit takes the extra cent), `email_notification` true only if the operator has an email, `url` as documented, `meta.lineItems` = one line "Rent, store <label>" | Amendment; SCOPE Q28; spec |
| D4 | Pause = `update_scheduled_invoice(active=False)`; resume = `active=True` with a new future `DTSTART`. **`total` is never sent on an update** (interface has no field) | Spec; owner |
| D5 | Rent change: a new `lease_rent_history` row (closing the open one the day before) and `needs_review` on any active schedule, with the message "Schedule amount differs from rent; pause and re-enable autopay". The schedule is never edited for an amount | Owner: schedules are never edited for adjustments |
| D6 | Run idempotency: `rent_charges` unique `(lease, period_month, seq)` plus `get_or_create` inside a transaction; a second run creates nothing and reports "already created" | Amendment 7b |
| D7 | Matching: for each active schedule, `list_invoices(schedule_external_id)`; an invoice whose `invoice_date_at` falls in the charge's period and whose schedule matches links to that charge: a local `invoices` row with `creation_key`, `invoice_number` (next `INV-YYYY-NNNN`), `rent_charge`, totals from Stax, `provider_status` through `provider_write()`, external ref `invoice`. Total mismatch with `amount_due` → `needs_review`, never overwritten | Prompt 7 item 2; DATA_OWNERSHIP §3 |
| D8 | Manual payment: `payments` row with `method` zelle/wire/other, amount, date, mandatory note; `amount_paid` recomputed; charge status `paid` or `partial`; no Stax call in this plan (mark-paid in Stax is Prompt 9) | Amendment; owner |
| D9 | Insurance charges are generated by a management command `generate_insurance_charges` (the owner's algorithm, ADR-0005) and listed on the run screen when due within 7 days. **Insurance autopay schedules are a later plan** (7c) | Scope control; the amendment asks for the listing |
| D10 | Roles: lease create/edit and enable/disable autopay, pause, resume → `admin`; run, create charges, match, manual payment → `billing`; viewer reads | SCOPE Round 2 Q14 |
| D11 | All Stax calls run in the request, paced by the client (interim single caller, PLAN-006 D1). The run creates charges locally first and then matches; it never creates Stax invoices itself | Amendment 7b |
| D12 | Sandbox ACH is reported disabled. Any ACH refusal is logged with field names, recorded as "blocked on Stax ACH", and never worked around with a card | Owner |

## 4. Interface contract

### 4.1 Routes (namespace `ledger`)

| Name | Path | Methods | POST role |
|---|---|---|---|
| `leases:list` | `/leases/` | GET | — |
| `leases:create` | `/leases/new/` | GET, POST | admin |
| `leases:detail` | `/leases/<id>/` | GET | — |
| `leases:edit` | `/leases/<id>/edit/` | GET, POST | admin |
| `leases:enrol` | `/leases/<id>/enrol/` | GET, POST | admin (choose bank method and enable autopay) |
| `leases:pause` / `leases:resume` | `/leases/<id>/pause/`, `/resume/` | POST | admin |
| `run:today` | `/run/` | GET | — |
| `run:create_charges` | `/run/create-charges/` | POST | billing |
| `run:match` | `/run/match/` | POST | billing |
| `charges:detail` | `/charges/<id>/` | GET | — |
| `charges:record_payment` | `/charges/<id>/payments/new/` | GET, POST | billing |

Lease form: store, operator, rent (Decimal, > 0), debit day, second debit day (optional),
channel, start date, status. Rent edits create history (D5). Lease detail: current rent and
history, schedules with state and next run, enrolment blockers ("operator has no Stax customer",
"no bank method linked", "no email: operator will not receive receipts" as information only),
charges list with status and balance, notes. Enrol page: the operator's payment methods from
Stax (bank only selectable), the computed `DTSTART`, the amount per debit, the large-debit
warning when amount exceeds `LARGE_DEBIT_WARNING_AMOUNT`, and "Enable autopay". Run page: today's
leases by debit day with last month's charge status, buttons to create charges and to match,
insurance charges due in 7 days, a banner when the breaker is open or the budget exhausted.

### 4.2 Services (`backend/ledger/services/`)

`leases.py`: `create_lease`, `update_lease` (history on rent change), `enable_autopay(lease, payment_method_external_id, actor)`,
`pause_autopay`, `resume_autopay`. `rent_run.py`: `leases_due(date)`, `create_charges(date, actor)`,
`match_schedule_invoices(actor)`, `record_manual_payment(charge, method, amount, paid_on, note, actor)`.
`insurance.py`: `generate_insurance_charges(today)`. Every provider call goes through
`payments.get_provider()`; services are the only callers. Every change writes `audit_log`.

### 4.3 Configuration

`LARGE_DEBIT_WARNING_AMOUNT` (decimal string, default `2000.00`), in `env_guard.py`, `.env.example`.

## 5. Work breakdown

| Batch | Task | Files |
|---|---|---|
| A1 | Provider interface and adapter: payment methods (D1), `fake.py` support, verified paths | `backend/payments/provider.py`, `stax/adapter.py`, `stax/mapping.py`, `fake.py` |
| A2 | Lease services, forms, views, templates, urls, registry | `backend/ledger/services/leases.py`, `forms.py`, `views/leases.py`, templates, `urls.py`, `accounts/registry.py` |
| A3 | Tests for A | `backend/tests/test_lease_services.py`, `test_lease_views.py`, `test_stax_payment_methods.py` |
| B1 | Rent run and insurance services, views, templates, command | `backend/ledger/services/rent_run.py`, `insurance.py`, `views/run.py`, `views/charges.py`, templates, `management/commands/generate_insurance_charges.py` |
| B2 | Invoice numbering helper (`INV-YYYY-NNNN`, gap-free per year, row-locked) | `backend/ledger/services/numbering.py` |
| B3 | Tests for B | `backend/tests/test_rent_run.py`, `test_insurance_charges.py`, `test_run_views.py`, `test_invoice_numbering.py` |
| C | README, `.env.example`, settings/env guard | as named |

## 6. Test plan

| # | Case | Author |
|---|---|---|
| 1 | Lease create/edit; rent change writes history and closes the previous row; no silent overwrite; twice-monthly split 50/50 with the odd cent on the first debit | Opus |
| 2 | Payment methods: cards are listed as not allowed and refused on POST; only `external_id`, method, last four, bank type, nickname survive (no account or routing data in any record, log or template) | Opus |
| 3 | Enable autopay creates exactly one schedule per debit day with the verified fields; `DTSTART` is in the future; `total` equals rent or half; a second click creates nothing (`external_refs` check first); provider failure leaves the lease unchanged and surfaces the field names | Opus |
| 4 | Pause and resume send only `active` (and a future `DTSTART` on resume); never `total` | Opus |
| 5 | Run: leases due by day; charges created idempotently; **double run creates no duplicate**; paused and unknown-channel leases excluded; twice-monthly leases get `seq` 1 and 2 on their days | Opus |
| 6 | Matching: Stax invoice in the period links to the charge; `provider_status` written only via `provider_write()`; total mismatch flags, never overwrites; invoice number assigned once | Opus |
| 7 | Manual payment: note mandatory; partial and full; `amount_paid` and status recomputed; audit row | Opus |
| 8 | Insurance generation per the owner's algorithm edge cases (Feb 29, future open date, closed before anniversary, missing premium, missing open date) and the 7-day listing | Opus |
| 9 | Large-debit warning at the threshold; breaker-open banner; budget check before any provider call on the run | Opus |
| 10 | Roles and CSRF on every new route; registry test green | Opus |
| 11 | Adversarial: concurrent double run on PostgreSQL; concurrent enable-autopay clicks; a payment method ID that belongs to another customer; schedule `total` tampering through the form; rent change while autopay active; a Stax invoice whose period is ambiguous (month boundary); manual payment above balance; negative and zero amounts; note with a card number | Fable |
| 12 | Whole suite both engines, lint, format, types, migrations, secret scan | Fable |

## 7. Risk register

| Risk | Treatment |
|---|---|
| Sandbox ACH disabled | D12; the schedule create test on staging is expected to fail with field names until Stax enables ACH |
| Two schedules per twice-monthly lease drift | One `collection_schedules` row per schedule; the lease page shows both states |
| Schedule amount diverges from rent after a change | D5 flag; staff pause and re-enable |
| Matching picks the wrong period | D7 uses the invoice date inside the charge period; ambiguous cases flag for review |
| Writing `provider_status` outside the sync writer | Database trigger from PLAN-004 rejects it; test 6 |
| Floating point in totals | `Decimal` only; the no-float test covers the new modules |

## 8. Acceptance

Lint, format, strict types exit 0; both pytest commands green on both engines; migrations clean
and reversible; no existing test removed or skipped; every row of §6 has a passing test. On
staging: enrolment reaches the schedule create and either succeeds (one schedule, visible in
the app) or is recorded as "blocked on Stax ACH" with the field names.

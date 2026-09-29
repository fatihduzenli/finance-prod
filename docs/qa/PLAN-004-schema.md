# PLAN-004 — Database schema and migrations

**Date:** 2026-09-29 · **Planner / QA owner:** Fable · **Implementer:** Opus
**Inputs:** Prompt 4 (owner, 2026-09-29); `docs/inputs/PROMPT_SERIES_AMENDMENTS.md`;
`docs/inputs/OWNER_ANSWERS_ROUND2.md`; `docs/SCOPE.md` §4a, §5, §9 Round 3; ADR-0004, ADR-0005;
`docs/CLAUDE.md` Rules 1, 2 and 8.

## 1. Restated objective

Create the database schema for the Rents ledger. Our database owns business data: stores,
operators, leases, rent and insurance charges, notes, adjustments. Stax owns money state:
invoice status and payment outcome are copied from Stax and never decided here. Every Stax
identifier lives in one mapping table so that a second provider needs no migration. Money is
exact decimal everywhere. Card numbers are rejected from free text by the application and by
the database. Nothing in this step calls Stax, imports the sheet or adds a screen.

## 2. Affected packages

Root project only. New Django app `ledger` (business tables) and models in the existing
`payments` package (provider-side tables). `backend/finance/settings.py` gains the two apps in
`INSTALLED_APPS` and nothing else. **Out of scope:** Django admin registration, views, URLs,
forms, the Stax client, the sheet import, Celery, `infra/`, `.github/`, `Dockerfile`,
dependencies (none may be added), `backend/accounts/`.

## 3. Decisions taken in this plan (owner may overrule before the push)

Prompt 4 says to stop when the mapping needs a field with no home, and to propose rather than
perform a split or merge. The owner's amendments file already instructs the additions in D1.
D2 to D9 are proposals. **They are built locally and presented to the owner with the Publish
Request; nothing reaches staging before the owner has seen them.**

| # | Decision | Reason |
|---|---|---|
| D1 | Add `stores`, `leases`, `lease_rent_history`, `rent_charges`, `charge_notes`, `charge_adjustments`, `insurance_premiums`, `insurance_charges`, `store_balances`, `notes`, `import_audit`, `provider_snapshots` | Amendments file; owner statements of 2026-09-29; Rule 8 |
| D2 | Prompt 4's `customers` is built as **`operators`**, with every `customers` field | Amendments: "where a prompt says customer, read operator" |
| D3 | **Money is `numeric(12,2)`**, Python `Decimal`. `docs/SCOPE.md` §5 `*_cents` names are replaced | Prompt 4 and the amendments both specify `numeric(12,2)`; one convention only |
| D4 | **New table `collection_schedules`** (one row per Stax schedule: lease, kind rent or insurance, seq). Replaces the amendments' single column `leases.stax_schedule_state` | A lease has up to two rent schedules plus one insurance schedule; `external_refs` needs one entity per schedule |
| D5 | Debit days are two columns `debit_day`, `second_debit_day` | Amendments; works on sqlite and PostgreSQL; replaces SCOPE's list |
| D6 | `invoices.allow_card` exists as Prompt 4 lists it, with a CHECK that it is `false` | Owner: no cards at all. One constraint to lift if that ever changes |
| D7 | `payments.method` is `ach`, `zelle`, `wire`, `other`. Prompt 4's `card`, `cash`, `check` are left out | Owner statements of 2026-09-29 |
| D8 | Status column names follow Prompt 4: `internal_status`, `provider_status`. `docs/CLAUDE.md` Rule 2 (`workflow_status`, `stax_status`) is updated to match | Prompt 4: no `stax_*` columns outside `external_refs` |
| D9 | `external_refs` follows Rule 8: unique on `(provider, external_kind, external_id)`, plus `superseded_at`. Prompt 4 says unique on `(provider, external_id)` | Rule 8 is the more detailed, already accepted definition; a Stax customer ID and a payment-method ID are different kinds |
| D10 | `rent_charges` has no `kind`. A catch-up or any other difference is a `charge_adjustments` row | Owner: adjustments are separate one-off invoices |
| D11 | Insurance status is **not stored**; it is derived from payments (owner's algorithm). Imported years carry `legacy_status` | One source of truth |
| D12 | `deposits.provider_deposit_id` and `webhook_events.provider_object_id` stay as Prompt 4 lists them | They are provider-side records, not business entities |

## 4. Interface contract

Conventions for every table: `id` UUID primary key (`uuid4` default) unless stated; table names
exactly as below (`db_table`); `created_at`/`updated_at` where listed; every enumerated column
is a `TextChoices` field **and** a database CHECK constraint; every money column is
`DecimalField(max_digits=12, decimal_places=2)`; foreign keys use `PROTECT` unless stated.
"Free text" marks a column covered by the card-number guard (§4.3).

### 4.1 App `ledger`

| Table | Columns | Constraints |
|---|---|---|
| `operators` | `display_name`; `legal_name`; `company`; `email` null; `phone`; `address_line1`; `address_line2`; `city`; `state`; `postal_code`; `country` default `US`; `notes` free text; `enrolment_status` (`not_enrolled`, `enrolled`) default `not_enrolled`; `created_at`; `updated_at`; `archived_at` null | `display_name` not blank. `email` validated, stored lower-case and stripped; CHECK `email = lower(email)`; unique where not null. Empty email is stored as NULL, never `''` |
| `stores` | `external_label`; `store_number` int null; `city`; `state`; `open_date` null; `open_date_note` free text; `needs_open_date` bool; `closed_at` date null; `status` (`prospective`, `active`, `closed`, `terminated`); `needs_review` bool; `created_at`; `updated_at` | `external_label` and `store_number` indexed, **not unique**. CHECK `needs_open_date = (open_date IS NULL)` |
| `leases` | `store` FK; `operator` FK null; `debit_day` smallint null; `second_debit_day` smallint null; `collection_status` (`active`, `paused`, `prospective`, `ended`); `review_reason` (`wire`, `dormant`, `closed`, `unknown`, empty); `payment_channel` (`stax`, `wire`, `zelle`, `unknown`, `other`); `started_on` null; `ended_on` null; `end_reason` free text; `needs_review` bool; `created_at`; `updated_at` | Days 1 to 31. `second_debit_day` requires `debit_day` and differs from it. **CHECK: not (`collection_status = active` and `payment_channel = unknown`)**. CHECK: `active` requires `debit_day` and `operator`. `ended_on >= started_on` |
| `lease_rent_history` | `lease` FK cascade; `amount`; `effective_from`; `effective_to` null; `source` (`sheet_current`, `sheet_inferred`, `staff`); `created_at` | `amount > 0`; `effective_to > effective_from`; unique `(lease, effective_from)`; at most one open row (`effective_to` null) per lease |
| `collection_schedules` | `lease` FK; `kind` (`rent`, `insurance`); `seq` smallint; `debit_day` smallint null; `amount`; `state` (`none`, `active`, `paused`, `failed`, `ended`); `created_at`; `updated_at` | `seq` in (1, 2); `amount > 0`; unique `(lease, kind, seq)` where `state <> ended` |
| `rent_charges` | `lease` FK; `period_month` date; `seq` smallint default 1; `due_on`; `amount_due`; `amount_paid` default 0; `balance` **generated, stored** = `amount_due - amount_paid`; `status` (`due`, `pending`, `paid`, `partial`, `unpaid`, `waived`); `legacy` bool default false; `needs_review` bool; `local_ref` uuid unique; `created_at`; `updated_at` | **unique `(lease, period_month, seq)`**; `period_month` is the first of a month; `seq` in (1, 2); `amount_due >= 0`; `amount_paid >= 0` |
| `charge_notes` | `rent_charge` FK null cascade; `insurance_charge` FK null cascade; `body` free text; `author` FK user null; `author_label`; `created_at` | exactly one of the two charge FKs is set; `body` not blank |
| `charge_adjustments` | `from_charge` FK `rent_charges` null; `to_charge` FK `rent_charges`; `amount`; `reason` (`cross_store_balance`, `credit`, `discount`, `carry_forward`); `note` free text; `created_by` FK user; `created_at` | `amount > 0`; **`note` not blank**; `from_charge <> to_charge`; `cross_store_balance` requires `from_charge`. Model `clean()` requires both charges to belong to the same operator for `cross_store_balance` |
| `insurance_premiums` | `policy_year` int unique; `amount`; `updated_at` | year 2000 to 2100; `amount > 0` |
| `insurance_charges` | `lease` FK; `policy_year` int; `due_on`; `amount` null; `needs_premium` bool; `needs_review` bool; `legacy` bool; `legacy_status` (`paid`, `unpaid`, empty); `created_at`; `updated_at` | unique `(lease, policy_year)`; CHECK `needs_premium = (amount IS NULL)`; `amount > 0` when set |
| `store_balances` | `store` FK cascade; `kind` (`insurance_opening`, `sign`, `deposit_or_access`); `amount` (signed); `needs_review` bool; `created_at` | unique `(store, kind)` |
| `notes` | `entity_type` (`store`, `operator`, `lease`); `entity_id` uuid; `body` free text; `author` FK user null; `author_label`; `created_at` | `body` not blank; index `(entity_type, entity_id)` |
| `invoices` | `invoice_number`; `operator` FK; `rent_charge` FK null; `insurance_charge` FK null; `charge_adjustment` FK null; `issue_date`; `due_date`; `memo` free text; `subtotal`; `tax`; `total`; `amount_paid` default 0; `internal_status` (`draft`, `ready`, `sent`, `closed`, `void`); `provider_status` text, blank allowed; `sent_at`; `viewed_at`; `paid_at`; `allow_card` bool default false; `allow_ach` bool default true; `creation_key` uuid unique; `legacy` bool default false; `created_at`; `updated_at` | `invoice_number` unique, format `^INV-\d{4}-\d{4,}$`; at most one source FK set; `total = subtotal + tax`; amounts `>= 0`; `due_date >= issue_date`; **`allow_card = false`**. `provider_status`: see §4.4 |
| `invoice_line_items` | `invoice` FK cascade; `position` int; `description` free text; `quantity` numeric(12,4); `unit_price`; `line_total` | unique `(invoice, position)`; `position >= 1`; `quantity > 0` |
| `payments` | `invoice` FK null; `rent_charge` FK null; `insurance_charge` FK null; `charge_adjustment` FK null; `kind` (`rent`, `insurance`, `adjustment`); `policy_year` int null; `method` (`ach`, `zelle`, `wire`, `other`); `amount`; `status` (`pending`, `settled`, `returned`, `voided`); `is_refund` bool; `parent_payment` FK self null; `initiated_on` null; `settled_on` null; `returned_at` null; `return_code`; `return_reason` free text; `provider_created_at` null; `reference` free text; `note` free text; `recorded_by` FK user null; `raw_summary` jsonb default `{}`; `created_at`; `updated_at` | `amount > 0`; `is_refund = (parent_payment IS NOT NULL)`; `policy_year` set exactly when `kind = insurance`; **methods `zelle`, `wire`, `other` require a non-blank `note`** |
| `audit_log` | `id` bigserial; `actor` FK user null; `action`; `entity_type`; `entity_id` text; `before` jsonb null; `after` jsonb null; `at` | **Append-only.** PostgreSQL trigger rejects UPDATE and DELETE |
| `legacy_records` | `source_tab`; `row_number` int; `raw` jsonb; `imported_at` | unique `(source_tab, row_number)` |
| `import_audit` | `sheet_row` int; `sheet_col` text; `raw_value` free text; `target_table`; `target_id` text; `rule_applied`; `needs_review` bool; `created_at` | index `(target_table, target_id)` |

### 4.2 Package `payments` (provider-side tables)

| Table | Columns | Constraints |
|---|---|---|
| `external_refs` | `entity_type`; `entity_id` uuid; `provider`; `external_kind`; `external_id`; `created_at`; `superseded_at` null | unique `(provider, external_kind, external_id)`; unique `(entity_type, entity_id, provider, external_kind)` where `superseded_at IS NULL`. **The only place a provider ID of a business entity is stored** |
| `provider_snapshots` | `external_ref` FK cascade; `payload` jsonb; `fetched_at` | index `(external_ref, fetched_at)` |
| `deposits` | `provider`; `provider_deposit_id`; `deposit_date`; `gross`; `fees`; `net`; `status`; `created_at` | unique `(provider, provider_deposit_id)`; `net = gross - fees` |
| `webhook_events` | `received_at`; `event_type`; `provider_object_id`; `dedupe_hash`; `processed_at` null; `outcome`; `error` | `dedupe_hash` unique |
| `sync_state` | `job_name` unique; `last_success_at` null; `cursor` | |

No column named `stax_*` anywhere. No column that could hold a card number, CVV, expiry, bank
account or routing number.

### 4.3 Card-number guard (Rule 1, Prompt 4 requirement 2)

**Application:** `backend/ledger/validators.py`

- `contains_card_number(text: str) -> bool`: true when the text holds a run of **13 to 19
  digits** that passes the Luhn check. Inside a run, digits may be separated by single spaces
  or single hyphens. A run is bounded by a character that is neither a digit nor such a
  separator, or by the ends of the text. A run of 20 or more digits is not a card number.
- `contains_bank_routing_pattern(text: str) -> bool`: Rule 1's second pattern: a 9-digit
  number with a valid ABA checksum next to (within 20 characters of) a digit run of 6 or more.
- `validate_no_payment_data(value: str) -> None`: raises `ValidationError` with the exact,
  value-free message `This text may not contain card or account numbers.` Attached to every
  free-text field above. The value is never included in the error, a log line or an exception.
- JSON columns (`raw_summary`, `payload`, `raw`, `before`, `after`) are validated on their
  serialised text by `validate_json_has_no_payment_data`.

**Database (PostgreSQL only, created by a migration, reversible):** function
`finance_contains_card_number(text) returns boolean` with the same Luhn rule, and one
`BEFORE INSERT OR UPDATE` trigger per table that raises an exception with the same value-free
message when any guarded column matches. Guarded columns: every free-text column in §4.1, and
the text form of every jsonb column in §4.1 and §4.2. The ABA pattern is application-only.
On sqlite (local development and the default test run) the trigger is not created; the
application validator still applies.

### 4.4 `provider_status` is written only by the sync layer (Prompt 4 requirement 3)

- Model field `editable=False`, with a help text and a **database column comment** stating:
  copied from the payment provider, written only by the sync layer, never by staff or views.
- No CHECK on its values: Rule 2 says unknown provider strings are stored as they are.
- PostgreSQL trigger on `invoices`: an INSERT with a non-empty `provider_status`, or an UPDATE
  that changes it, is rejected unless the transaction has set
  `finance.sync_writer = 'on'`. `backend/payments/sync_writer.py` provides the only way to set
  it: a context manager `provider_write()` that opens a transaction and runs
  `SELECT set_config('finance.sync_writer', 'on', true)` (transaction-local).
- A test asserts that no module outside `backend/payments/` imports `sync_writer`.

### 4.5 Seed command (Prompt 4 requirement 4)

`python backend/manage.py seed_demo` in app `ledger`. Refuses with exit status 2 unless
`APP_ENV=local`. Idempotent: a second run changes nothing. Deterministic (fixed random seed).
Produces: 10 operators (2 without email), 14 stores, 14 leases including one twice-monthly,
one paused and one wire lease, rent history, insurance premiums 2025 = 595.00 and
2026 = 695.00 with insurance charges, **25 invoices covering every `internal_status` and every
listed `provider_status`**, rent charges in every status, **partial payments**, one returned
ACH payment, one Zelle payment with a note, one cross-store adjustment, and **one refund**.
All names and emails are invented and use `example.com`. No external ID resembles a real one.

### 4.6 Amendment after QA cycle 1 (2026-09-29) — the guard, tightened

Found by the QA owner's adversarial tests. This section overrides §4.3 where they differ.

1. **Coverage.** The guard covers **every** `CharField` and `TextField` of both apps that has
   no `choices` and can hold 13 characters (`max_length` 13 or more, or unlimited), and every
   JSON column. **Excluded, by name:** `external_refs.external_id`,
   `deposits.provider_deposit_id`, `webhook_events.provider_object_id`,
   `webhook_events.dedupe_hash`, `audit_log.entity_id`, `import_audit.target_id`. These hold
   identifiers, not free text.
2. **Separators.** Between two digits of a run there may be **one to three** separator
   characters. Separators are: space, tab, no-break space (U+00A0), hyphen-minus, the dashes
   U+2010 to U+2015, and a line break (`\n` or `\r\n`, counted as one). A dot, a slash, a
   colon and a letter end a run.
3. **Invisible characters.** U+200B, U+200C, U+200D, U+2060 and U+FEFF are removed from the
   text before matching.
4. **Identifiers are not card numbers.** Before matching, every UUID-shaped token
   (`8-4-4-4-12` hexadecimal groups, either case, bounded by non-hexadecimal characters or
   the ends of the text) is replaced by a single letter, in the application and in the
   database. Without this, about one provider identifier in 20,000 is rejected and the sync
   fails on it.
5. **Same rule in both layers.** The database function applies points 2 to 4 exactly as the
   application does, for ASCII, full-width, Arabic-Indic and extended Arabic-Indic digits.
   Digits of other scripts are recognised by the application only; this is recorded as a
   known difference.
6. All matching stays linear in the length of the text.

## 5. Work breakdown

| Batch | Task | Files |
|---|---|---|
| A | A1 validators | `backend/ledger/__init__.py`, `apps.py`, `validators.py` |
| A | A2 `ledger` models and migrations | `backend/ledger/models.py` (or a `models/` package), `backend/ledger/migrations/0001_initial.py` and following |
| A | A3 `payments` models and migrations | `backend/payments/apps.py`, `models.py`, `sync_writer.py`, `migrations/` |
| A | A4 PostgreSQL functions and triggers as reversible migrations | `backend/ledger/migrations/`, `backend/payments/migrations/` |
| A | A5 settings | `backend/finance/settings.py` (`INSTALLED_APPS` only) |
| A | A6 tests | `backend/tests/test_ledger_models.py`, `test_payments_models.py`, `test_card_number_guard.py`, `test_migrations_roundtrip.py` |
| B | B1 seed command and its tests | `backend/ledger/management/commands/seed_demo.py`, `backend/tests/test_seed_demo.py` |
| B | B2 README commands | `README.md` |

Batch B starts after batch A. `docs/DATA_OWNERSHIP.md` and the entity-relationship summary are
written by Fable.

## 6. Test plan

| # | Case | Author |
|---|---|---|
| 1 | Fresh database: `migrate` applies everything; `migrate ledger zero` and `migrate payments zero` roll back cleanly; `migrate` again succeeds (PostgreSQL and sqlite) | Opus |
| 2 | `makemigrations --check --dry-run` reports no changes | Opus |
| 3 | Every CHECK and unique constraint in §4 has one accepting and one rejecting test at database level (`IntegrityError`) | Opus |
| 4 | Money round trip: `Decimal("12581.00")`, `Decimal("0.01")`, `Decimal("9999999999.99")` stored and read back equal; a `float` is never accepted silently; `balance` is generated correctly | Opus |
| 5 | Guard rejects `4111 1111 1111 1111`, `4111-1111-1111-1111`, `4111111111111111`, a 13-digit and a 19-digit Luhn-valid number, inside longer text and across a line break | Opus |
| 6 | Guard accepts: an invoice number `INV-2026-0001`, a 16-digit number that fails Luhn, a US phone number, a 12-digit and a 20-digit Luhn-valid run, a rent amount, a date, a UUID | Opus |
| 7 | The database trigger rejects test 5 values on every guarded table when the application validator is bypassed (`QuerySet.update`, `bulk_create`, raw SQL) | Opus (PostgreSQL) |
| 8 | `provider_status`: plain save and raw UPDATE rejected; inside `provider_write()` accepted; setting does not leak to the next transaction | Opus (PostgreSQL) |
| 9 | `audit_log` UPDATE and DELETE rejected | Opus (PostgreSQL) |
| 10 | Seed: runs after migrate; counts match §4.5; second run adds nothing; refuses when `APP_ENV` is not `local`; every status covered; no seeded text trips the guard | Opus |
| 11 | No column named `stax_*`; no column name containing `card_number`, `cvv`, `routing`, `account_number`; provider IDs only in `external_refs`, `deposits`, `webhook_events` (introspection test) | Fable |
| 12 | Guard evasion: full-width digits, Arabic-Indic digits, zero-width characters, tabs and double spaces between groups, digits split over JSON keys, card number inside jsonb `raw_summary`, 5,000-character inputs, timing on hostile input | Fable |
| 13 | False positives at scale: 10,000 random phone numbers, invoice numbers, amounts and dates; rejection rate reported | Fable |
| 14 | Application and database Luhn implementations agree on 5,000 random digit strings | Fable (PostgreSQL) |
| 15 | No `float` in the new application code (static check); no `FloatField` in any model | Fable |
| 16 | Lease rule: a lease with unknown channel cannot become active by any write path | Fable |
| 17 | Concurrency: two transactions inserting the same `(lease, period_month, seq)`; exactly one succeeds | Fable (PostgreSQL) |
| 18 | Whole suite, lint, format, strict types, secret scan; image build with migrations on an empty database | Fable |

## 7. Risk register

| Risk | Treatment |
|---|---|
| Schema encodes an owner decision wrongly | §3 lists every decision; nothing is pushed before the owner has read them |
| Guard too weak: a card number reaches a column | Two layers; tests 5, 7, 12 |
| Guard too aggressive: staff cannot save a legitimate note | Tests 6, 13; about 10% of random 13 to 19 digit numbers pass Luhn by chance, which is inherent and documented |
| Triggers exist only on PostgreSQL | Staging, production and CI are PostgreSQL; sqlite is local only; stated in `docs/DATA_OWNERSHIP.md` |
| Migrations applied on staging are hard to reshape | First applied only after owner approval of §3; staging holds no business data |
| Generated column or partial index unsupported on sqlite | Django 5.2 supports both on sqlite 3.31+; test 1 runs on both |
| Seed data mistaken for real data or run on staging | Refuses outside `APP_ENV=local`; `example.com` only |
| `entity_id` in `external_refs` and `notes` has no foreign key | Accepted cost of a generic reference; integrity checked in the reconciliation job (Prompt 10) |
| Time zones | All timestamps are timezone-aware UTC; dates (`due_on`, `period_month`) are plain dates |

## 8. Acceptance criteria

1. `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy` exit 0.
2. `uv run pytest -m "not e2e" -q` exits 0 on PostgreSQL 16 with no skipped test, and on
   sqlite with only PostgreSQL-only tests skipped. `uv run pytest -m e2e -q` exits 0.
3. No existing test edited, removed or skipped.
4. Every row of §6 has a passing test.
5. Migrations apply on an empty database, roll back to zero, and apply again, on both engines.
6. `seed_demo` runs after migration and produces the data in §4.5.
7. The guard rejects `4111 1111 1111 1111` and accepts a long invoice number and a phone number.
8. `pyproject.toml` and `uv.lock` unchanged.
9. `docs/DATA_OWNERSHIP.md` exists and names an owner for every column of every table.

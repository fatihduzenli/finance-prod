## QA Report — PLAN-004: database schema and migrations

**Date:** 2026-09-29 · **QA owner:** Fable · **Implementer:** Opus · **Plan:** `docs/qa/PLAN-004-schema.md`

**Scope:** root project. New app `backend/ledger/` (models, validators, two migrations,
`seed_demo`), `backend/payments/` (models, `sync_writer.py`, `demo.py`, two migrations),
`backend/finance/settings.py` (`INSTALLED_APPS`, three lines), `README.md`.
Tests (Opus): `test_ledger_models.py`, `test_payments_models.py`, `test_card_number_guard.py`,
`test_migrations_roundtrip.py`, `test_seed_demo.py`. Tests (Fable): `test_schema_adversarial.py`.
Docs (Fable): `docs/DATA_OWNERSHIP.md`, this report, updates to `docs/SCOPE.md` §5 and `docs/CLAUDE.md`.
Unchanged, verified: `pyproject.toml`, `uv.lock`, `infra/`, `.github/`, `Dockerfile`, `backend/accounts/`.

**Commands run by Fable after rework cycle 1** (repository root, placeholder environment):

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .` | n/a | 0 | clean |
| `uv run ruff format --check .` | n/a | 0 | 110 files |
| `uv run mypy` (strict) | n/a | 0 | 81 source files |
| `uv run python backend/manage.py makemigrations --check --dry-run` | both | 0 | no changes |
| `uv run pytest -m "not e2e" -q` | PostgreSQL 16 | 0 | 999 passed, 0 skipped |
| `uv run pytest -m "not e2e" -q` | sqlite | 0 | 687 passed, 312 skipped (PostgreSQL-only) |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 2 passed |
| guard, adversarial and round-trip files, two consecutive runs | PostgreSQL 16 | 0 | 458 passed each time |
| `bash scripts/check-no-secrets.sh` | n/a | 0 | clean |
| `migrate`, `migrate` again, `seed_demo` twice, `migrate ledger zero`, `migrate payments zero`, `migrate` | PostgreSQL 16, fresh database | 0 each | see hand checks |

**Hand checks on a fresh PostgreSQL database**

| Check | Result |
|---|---|
| Seed | 10 operators, 14 stores, 14 leases, 25 invoices, 32 payments, 1 refund; second run identical |
| Invoice statuses | every `internal_status` and every listed `provider_status` present, plus `PARTIALLY PAID` |
| `UPDATE invoices SET memo = '4111 1111 1111 1111'` | refused: "This text may not contain card or account numbers." |
| `UPDATE invoices SET provider_status = ...` outside the sync layer | refused |
| `UPDATE` and `DELETE` on `audit_log` | refused by trigger (covered by tests; the seed writes no audit rows) |
| Column comment on `provider_status` | present |
| Roll back to zero, migrate again | clean |

**Tests:** 308 before, 1,001 after. No existing test edited, removed or skipped. Every row of
plan §6 has a passing test. Coverage tooling is not configured; constraint coverage is
enforced by a test that fails when a constraint has no accepting and rejecting case.

**Findings**

```
[MAJOR] backend/ledger/models.py:177 — open text columns were outside the card-number guard.
Failure scenario: a card number saved in display_name, company, an address line and others was stored.
Required change: every open text column of 13 characters or more is guarded in both layers.
Status: RESOLVED in rework cycle 1. 17 tables carry the trigger; six identifier columns are excluded by name.

[MAJOR] backend/ledger/validators.py:37 — common separators defeated the guard.
Failure scenario: two spaces, tabs, no-break spaces, en dashes, " - " and zero-width characters between digit groups were accepted.
Required change: plan §4.6 points 2 and 3.
Status: RESOLVED in rework cycle 1.

[MAJOR] backend/ledger/validators.py:37 — provider identifiers could be rejected as card numbers.
Failure scenario: a UUID whose last groups are all digits formed a 13 to 19 digit run; about one such identifier in 20,000 would have stopped the sync.
Required change: UUID-shaped tokens are excluded before matching, in both layers.
Status: RESOLVED in rework cycle 1. 6,000 generated identifiers: none rejected.

[MINOR] backend/ledger/validators.py — digits of scripts other than ASCII, full-width and the two Arabic-Indic scripts are recognised by the application only.
Decision: accepted and recorded in plan §4.6 point 5 and docs/DATA_OWNERSHIP.md.

[MINOR] backend/ledger/migrations/0002 — the bank routing pattern and JSON keys are checked by the application only, not by the trigger.
Decision: accepted; Prompt 4 requires the Luhn rule in the database. Recorded in docs/DATA_OWNERSHIP.md.

[MINOR] backend/payments/sync_writer.py:38 — a provider_write() block nested inside another switches the flag off when the inner block ends.
Failure scenario: the outer block's later writes are refused. Fails closed.
Decision: deferred to the Prompt 8 plan, where the sync layer is written.
```

Counts: BLOCKER 0, MAJOR 3 resolved, MINOR 3 (2 accepted, 1 deferred). Rework cycles: 1.
The full gate was re-run after the rework.

**Two mistakes of mine, corrected.** A plan defect: §4.3 listed too few guarded columns; it is
amended by §4.6. A test defect: a lint fix of mine broke a line in my own test file, which
Opus reported and did not touch; I corrected it and one sqlite-only parameter type.

**Deviation accepted:** `backend/payments/demo.py`, not in the plan's file list. It is the
only path by which the seed writes `provider_status`, and it refuses outside `APP_ENV=local`.

**Gate 3:** models, validators and trigger SQL read in full. One money field type, one guard
helper per column kind, no dead code, no admin, URL or view added.
**Gate 4:** trigger SQL is constant text; no value is ever placed in an error message; matching
is linear (200,000-character hostile inputs in under two seconds in total); the unique
constraint on `(lease, period_month, seq)` holds under two concurrent inserts; a lease with an
unknown channel cannot become active through five write paths. Secret scan of the diff: clean.
No secret store was read by either model. No new dependency.

**Residual risk**
- **Decisions D2 to D12 of the plan are proposals until the owner accepts them.** Nothing has
  been pushed. The migrations have been applied only to throwaway local databases.
- The triggers exist on PostgreSQL only. A local sqlite database relies on the validators.
- The provider-status guard stops accidents, not a person with direct database access.
- About one in ten random 13 to 19 digit numbers passes Luhn; such a reference number in a
  note is refused. Inherent to the rule Prompt 4 asks for.
- Overlapping rent periods for one lease are not prevented by the database; the application
  must check them when rent history is edited (Prompt 7).
- The three MINOR findings of QA-003a remain deferred.

**Verdict: PASS.** The owner accepted decisions D2 to D12 in writing on 2026-09-30; the condition is met.

### Entity-relationship summary

| Table | Belongs to | Has many | Key rule |
|---|---|---|---|
| `operators` | — | `leases`, `invoices` | email unique when present |
| `stores` | — | `leases`, `store_balances` | label is not unique |
| `leases` | `stores`, `operators` | `lease_rent_history`, `collection_schedules`, `rent_charges`, `insurance_charges` | unknown channel cannot be active |
| `lease_rent_history` | `leases` | — | one open period per lease |
| `collection_schedules` | `leases` | — | one live schedule per lease, kind and seq |
| `rent_charges` | `leases` | `charge_notes`, `payments`, `invoices`, `charge_adjustments` | unique lease, month, seq |
| `insurance_premiums` | — | — | one amount per policy year |
| `insurance_charges` | `leases` | `charge_notes`, `payments`, `invoices` | unique lease, policy year |
| `charge_notes` | one rent or insurance charge | — | exactly one parent |
| `charge_adjustments` | `rent_charges` (from, to), user | `payments`, `invoices` | note mandatory |
| `store_balances` | `stores` | — | one per store and kind |
| `notes` | store, operator or lease by reference | — | |
| `invoices` | `operators`; at most one charge or adjustment | `invoice_line_items`, `payments` | number unique; no cards |
| `invoice_line_items` | `invoices` | — | unique position |
| `payments` | invoice, charge or adjustment; parent payment | refunds | manual methods need a note |
| `audit_log` | user | — | append-only |
| `legacy_records` | — | — | unique tab and row |
| `import_audit` | — | — | one row per imported cell |
| `external_refs` | any business record by reference | `provider_snapshots` | the only home of provider IDs |
| `provider_snapshots` | `external_refs` | — | |
| `deposits` | — | — | net equals gross minus fees |
| `webhook_events` | — | — | unique dedupe hash |
| `sync_state` | — | — | unique job name |

### Commands

```
uv run python backend/manage.py migrate
uv run python backend/manage.py seed_demo        # APP_ENV=local only; safe to repeat
uv run python backend/manage.py migrate ledger zero && uv run python backend/manage.py migrate payments zero   # roll back
```

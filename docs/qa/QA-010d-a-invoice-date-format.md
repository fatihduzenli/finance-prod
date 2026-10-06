## QA Report — PLAN-010d-a: invoice body dates in Stax's date-time shape

**Date:** 2026-10-06 · **QA owner:** Fable · **Implementer:** Opus
**Origin:** owner click 1 of Prompt 10d on staging: the worker's `invoice_create` for
`INV-2026-0003` failed with `ProviderRejected (fields: due_at, invoice_date_at)`; no invoice at
Stax. We sent both fields as `YYYY-MM-DD` (TODO-VERIFY-026's assumption).

**Evidence of the accepted shape.** The spec page create-an-invoice types `due_at` as
`date-time` with the response example `"due_at": "2025-12-19 23:59:59"`; the invoice objects Stax
sent to staging by webhook carry `"invoice_date_at": "2026-10-02 20:50:23"` (read from the stored
redacted bodies, date keys only). Shape: `Y-m-d H:i:s`, a space, no timezone.

**Fix.** `payments/stax/mapping.py`: new `stax_datetime(date, end_of_day=…)`; `invoice_body` sends
`invoice_date_at` as `YYYY-MM-DD 00:00:00` and `due_at` as `YYYY-MM-DD 23:59:59`;
`invoice_update_body` likewise for `due_at`. `iso_date` remains for the list calls' query
parameters, which staging accepts. Every other date-built field was reviewed: the RRULE `DTSTART`
and the list queries are unchanged and accepted on staging; the schedule body carries no other
date. The `FakeProvider` needed no change.

**Regression test through the real transport** (`test_stax_adapter.py`): a Stax-like mock that
answers 422 with the Laravel-style message naming each field given as a bare date; the old shape
is proven refused with `ProviderRejected` and `fields == ("due_at", "invoice_date_at")` in one
request; `create_invoice` with `invoice_date=2026-10-06`, `due_on=2026-10-16` sends
`2026-10-06 00:00:00` and `2026-10-16 23:59:59` and succeeds; `update_invoice` pins `due_at`.

**Also fixed (tests only):** `test_lease_views.py::test_enable_through_the_screen_then_a_second_click_creates_nothing`
asserted literal dates that stopped being "the next debit days" on 2026-10-06; it now asserts the
dates `next_debit_at` computes from the clock.

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .`, `ruff format --check .`, `uv run mypy` | n/a | 0 | clean, 332 files; 267 source files |
| `makemigrations --check --dry-run` | both | 0 | no changes |
| `uv run pytest -m "not e2e" -q -o faulthandler_timeout=300` | PostgreSQL 16 | 0 | 3,944 passed, 3 skipped |
| same | sqlite | 0 | 3,463 passed, 484 skipped |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 5 passed |
| `bash scripts/check-no-secrets.sh`; pattern scan of the diff | n/a | 0 | clean |

**Findings.** BLOCKER 1, resolved (the date shape). MINOR 1, resolved (the calendar time bomb in
a test). **Residual risk:** whether Stax accepts `00:00:00` for `invoice_date_at` on create is shown
by the mock only; the owner's retry of `INV-2026-0003` is the proof. TODO-VERIFY-026 updated; the
query-date assumption stays open.

**Verdict: PASS** (pending the staging retry).

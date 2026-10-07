## QA Report — PLAN-012c-a: every builder line linked to its invoice; paid line by line

**Date:** 2026-10-07 · **QA owner:** Fable · **Implementer:** Opus
**Origin:** owner's findings 3 and 4 of Prompt 12 on staging (INV-2026-0006).
**Plan:** `docs/qa/PLAN-012c-a-builder-lines-linked-and-paid-line-by-line.md` · ships with 012a,
012b and 012c in one push.

**Scope.** `backend/ledger/services/one_off_charges.py` (`invoices_of`, `invoice_covered`,
`invoice_billed`, `invoice_charges`, `is_builder_invoice`, `lock_lines`, the mark decision,
`allocated_overflow` capped by a line's own payments, `DELETE_INVOICE_SHARED`),
`backend/ledger/services/rent_run.py` (`charge_invoices`, `provider_invoice`, the sibling flags
cleared after a mark), `backend/ledger/views/one_off_charges.py`, the new read-only command
`invoice_line_links`, tests. No migration, template structure, route, environment or provider
change (the mark call is unchanged; `OfflinePayment.amount` is local only).

**Commands run (final code).** Env as in QA-012; PostgreSQL 16 in a throwaway container.

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .`, `uv run ruff format --check .` | n/a | 0 | clean |
| `uv run mypy` (strict) | n/a | 0 | no issues in 352 source files |
| `uv lock --check` | n/a | 0 | unchanged |
| `manage.py makemigrations --check --dry-run` | sqlite, PostgreSQL 16 | 0 | No changes detected |
| `uv run pytest -m "not e2e" -q -o faulthandler_timeout=300` | sqlite | 0 | 4,612 passed, 576 skipped (4,591 before) |
| same | PostgreSQL 16 | 0 | 5,185 passed, 3 skipped (5,164 before) |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 7 passed |
| `bash scripts/check-no-secrets.sh`; pattern scan of the diff and new files | n/a | 0 | clean |
| Bicep build | n/a | — | not applicable: `infra/` untouched |

Baseline before PLAN-012c-a (QA-012c): PostgreSQL 5,164 passed; sqlite 4,591 passed.

**Tests (Gate 2).** Opus: 14 new functions in `test_invoice_builder.py` (the owner's scenario
end to end through the pages and the worker: the memo, the check number, both flags cleared,
the sibling audit, provider status PAID, open balances empty; late fee first; three lines in
three payments; one $350 payment on line 1; $299.99 + $50 not covered then $0.01 marks; a first
line overpaid by a cent; a later line capped at its balance; a direct line payment skipped by
the first line's overflow; a waived later line lowers the target; a total differing from the
lines gives `TOTAL_DIFFERS`; Waive refused on a later line in the service, the page and the
POST, and no Link invoice on either line; the delete remedy refused on a shared invoice; a failed
mark flags only the paying line and its retry clears both; the command's three exits), one in
`test_one_off_charges.py` (a plain charge paid in two parts is still flagged, never marked), two
outdated tests updated. Opus found one defect in Fable's adversarial helper (a stale draft
object) and reported it instead of editing the file; Fable fixed it.

Fable's adversarial file (`test_plan012c_a_adversarial.py`, 6 cases): a later line cannot be
paid from another operator's charge page by forged fields, and that operator's invoice is never
marked; the provider is told "paid" exactly once when both lines are paid before the worker runs,
in either order, with one mark job, both lines at zero, no Record a payment left and Waive
refused; the partial flag sits only on the paying line while the other line still shows the
invoice, Record a payment, no Link invoice and the Waive refusal; `invoice_line_links --check`
reads only (audit count, line items and invoices identical before and after) and names the
orphaned invoice; a rent charge's provider invoice resolves exactly as before.

No test weakened, skipped or deleted. Coverage is not measured in this repository.

**Findings (Gates 3 and 4).** None blocking. Design choices accepted as built: a payment on a
later line larger than its balance is refused (`PAY_OVER_BALANCE`), nothing recorded; coverage
counts settled payments less settled refunds on the non-waived, non-voided lines; when covered,
the mark is queued if `invoice.total` equals what the lines bill, else `TOTAL_DIFFERS`; the memo
format is unchanged ("Paid by <method> on <date>. <note>", the reference as the check number;
the plan's "names the paying charge" was not added, so every charge's memo stays the same);
`DELETE_INVOICE_SHARED` keeps the waived-invoice remedy from deleting an invoice that still bills
unwaived lines; the mark path takes the lines' locks in payment order.

Gate 4: the payment route is per charge and ignores foreign fields; the lines are locked first
line first so two quick payments serialise and the mark job is queued once; pending payments
never count; the command writes nothing; the provider request is unchanged; no new secret.

**Confidentiality gate (§5.4).** Secret scan clean; no secret-store read; no new file type; no
new secret name.

**Residual risk.**
- Real two-session concurrency on PostgreSQL is not exercised (the serialisation rests on the
  row locks taken in a fixed order).
- The payments-ledger and P&L split of a first-line payment is unchanged: in the mixed case (a
  later line paid on its own, then overflow from line 1 on an invoice of three or more lines)
  the split can show line 2 instead of line 3; charge states and open balances are right.
- A waived **first** line with live unwaived later lines can use neither the remedy nor Cancel
  (an admin cancels the lines or the invoice from the provider's side); recorded for Step 12d.
- Refunds on a later line are not covered by tests.
- The staging repair is verified by the owner's click and by `invoice_line_links --check` on
  staging (a read-only command in the web container, to be run with the owner's authorization).

**Verdict: PASS** (pending the owner's clicks on INV-2026-0006).

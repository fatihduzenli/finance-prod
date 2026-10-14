## QA Report — PLAN-012e (Step 12b): lease end, operator transfer, installment plans, proration

**Date:** 2026-10-13 · **QA owner:** Fable · **Implementer:** Opus
**Plan:** `docs/qa/PLAN-012e-lease-end-transfer-installments.md` · **Decision:** ADR-0012 (proposed)

### Batches A and B (committed `02c9765`)

**Scope.** `backend/ledger` (new services `proration`, `lease_end`, `statements`, `transfer`,
`messages`; `leases` with `stop_autopay`; `rent_run`, `deposits`, `one_off_charges`,
`receivables`, `reports`, `pnl`, `attention`, `documents`, `collections`, `merge`; views
`lease_ends`, `transfers`, `leases`, `stores`; forms; templates; migrations ledger 0034–0037),
`backend/payments` (jobs `lease_end_documents`, `autopay_stop`; migrations 0016–0017),
`backend/mail` (attachments), `pyproject.toml` and `uv.lock` (`reportlab==5.0.1`, BSD; transitive
`pillow==12.3.0`, MIT-CMU), tests. No infra, workflow or environment change; every provider call
is one the adapter already made.

**Commands run (final A+B code).** Env as in QA-012; PostgreSQL 16 in a throwaway container.

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .`, `uv run ruff format --check .` | n/a | 0 | clean |
| `uv run mypy` (strict) | n/a | 0 | no issues in 371 source files |
| `uv lock --check` | n/a | 0 | consistent (reportlab pinned) |
| `manage.py makemigrations --check --dry-run` | sqlite, PostgreSQL 16 | 0 | No changes detected |
| `uv run pytest -m "not e2e" -q -o faulthandler_timeout=300` | sqlite | 0 | 4,854 passed, 607 skipped (4,642 before) |
| same | PostgreSQL 16 | 0 | 5,458 passed, 3 skipped (5,216 before); 18:40 wall |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 7 passed |
| `bash scripts/check-no-secrets.sh`; pattern scan of the diff and new files | n/a | 0 | clean |
| Bicep build | n/a | — | not applicable: `infra/` untouched |

**Tests (Gate 2).** Opus: `test_proration.py` (23), `test_lease_end.py` (57 + the rework's
stop-autopay, after-end and live-invoice cases), `test_transfer.py` (32), attachment cases in
`test_mail.py`, seeded round trips of 0034–0037. Fable (`test_plan012e_adversarial.py`, 16
cases): the wizard cannot end another store's lease by forged fields; the refund equals the
deposit less deductions whatever the form posts, in three settings; deductions beyond the deposit
owe and refund nothing; a card number or routing-and-account pair in a deduction, reference or
note is refused and reaches no audit row; the stored PDF holds no long digit run and passes the
guard; the statement e-mail goes to the catch-all with the attachment; viewer and billing get 403
on every step; confirm is atomic under a forced failure; a transfer cannot create the incoming
lease without ending the outgoing one (forced failure rolls everything back); a transfer cannot be
pointed at another store's lease or at an archived or the outgoing operator by forged fields;
nothing reaches the provider before the move-in draft is sent.

**Findings (A and B).**

| # | Severity | Where | Finding | Outcome |
|---|---|---|---|---|
| 1 | MAJOR | `lease_end.blockers` | A rent or insurance charge with a live Stax invoice did not block, so a lease could end with an invoice the operator can still pay open at the provider | Resolved before Batch B: such charges block like one-off charges, linked to where the invoice is cancelled; tests for live, paid and DELETED |
| 2 | decision | plan D5 | A paused schedule blocked with no action to clear it | Resolved: **Stop autopay** (admin, confirmation page, worker job, one provider delete per schedule, 404 = gone) |
| 3 | decision | plan D3 | Rent charges after a back-dated end date were left open | Resolved: unpaid → waived at confirm with an audit row; paid → credit |

Design choices accepted: the deposit offset counted once (`deposit_resolutions.offset_amount`);
coverage order (offset first, then other deductions); the overpaid last month refunded as a
settled refund payment by the settlement method; "check" recorded as method `other` with a note;
the lease-end balance charge manual with no invoice; paid and voided charges never block; the
final-statement message bound to the `lease_end` entity; dates written as words in the PDF; a
transfer's balance blockers judged as the end will leave them; debt allowed after a transfer
(**for the owner**: a "Lease-end balance" charge may remain on the outgoing operator when
deductions exceed the deposit, as in a plain lease end; the owner's rule says every balance is
settled first, so the owner decides whether that must block); the insurance blocker links to
the lease page (insurance charges have no page).

### Batch C (installments)

**Scope.** `backend/ledger` (new `services/installments.py`, `views/installments.py`,
templates `installments/*`; `models.py` (`InstallmentPlan`, `OneOffCategory.INSTALLMENT`,
`OneOffCharge.plan/installment_no`); `one_off_charges`, `rent_run`, `receivables`, `attention`,
`pnl`, `grid`, `payments_ledger`, `csv_export`, `returns`, `nsf`, `lease_end` (the live-plan
blocker); `forms.py`, `urls.py`, charge, invoice and payment templates; migration ledger 0038),
`backend/payments` (`one_off_invoices.move_schedule/move_invoice`, `jobs/runners.py` reschedule
dispatch), `accounts/registry.py`, tests. No new dependency, environment variable, infra or
workflow change; every provider call is one the adapter already made (`update_invoice` is now
called for the first time: TODO-VERIFY-029 stands).

**Commands run (final Batch C code, after the rework).** Env as above; PostgreSQL 16 in a
throwaway container (`plan012e-c-gate-pg`, port 55432).

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .`, `uv run ruff format --check .` | n/a | 0 | clean, 463 files formatted |
| `uv run mypy` (strict) | n/a | 0 | no issues in 376 source files |
| `uv lock --check` | n/a | 0 | consistent |
| `manage.py makemigrations --check --dry-run` | sqlite, PostgreSQL 16 | 0 | No changes detected |
| `uv run pytest -m "not e2e" -q -o faulthandler_timeout=300` | sqlite | 0 | 4,973 passed, 613 skipped (4,854 after A+B); 10:59 wall |
| same | PostgreSQL 16 | 0 | 5,583 passed, 3 skipped (5,458 after A+B); 20:39 wall |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 7 passed |
| `bash scripts/check-no-secrets.sh`; pattern scan of the diff since `02c9765` and every new file | n/a | 0 | clean |

**Tests (Gate 2).** Opus: `test_installments.py` (69: the four rounding cases, cent refusals,
counts 1 and 13, the dates of each cadence incl. the clamped 31st and leap February, past first
date, edits that break the sum or the order, each refusal reason, the provider objects per
collection and none for manual, the parent left out of open balances, aging, arrears and
attention, a payment on installment 2 only, parent payment refused, completion without a payment
row, a returned installment through the webhook with the $100 fee and the attention item cleared
by reschedule or by a note, reschedule of unpaid ones only with the provider updates, cancel
through the worker with the remainder back on the parent and a re-split allowed, P&L and ledger
equal to the unsplit charge with the payments settled in a later month, grid progress, pages and
role gates, the oversized-amount refusal); `test_lease_end.py` (+6: the plan blocker for a rent
and a one-off parent, attention still blocks, completed and cancelled do not, confirm refuses);
`test_transfer.py` (+1); `test_reports.py` property test with split, pay-installment and
cancel-plan operations (and a pre-existing oracle date fixed); seeded round trip of 0038; query
counts P&L and revenue 16 → 17. Fable (`test_plan012e_c_adversarial.py`, 20 cases + 1
PostgreSQL-only): the plan binds to the charge's own operator and lease whatever the form posts;
lines adding up to another charge's balance are refused; hostile amounts (NaN, negative,
Infinity, three decimals, `1E+400`, a short sum, a formula) and dates (past, out of order,
invalid, datetime) are refused and write nothing, the page comes back without echoing the raw
value; a second confirm finds the first plan and the partial unique index refuses a forced
second; two confirms at the same moment leave one plan (threads, PostgreSQL); five seeds of
random parents, splits, installment and parent payments and cancels never double count in open
balances, the payments ledger or the P&L (billed once, collected once, a completed parent has no
payment row); an installment payment counts in the month it settled like any other; the lease
cannot end while a plan is live on one of its charges (blockers, confirm refused with nothing
written, cleared by cancel); a cancel note with a card number is refused and reaches no row;
anonymous gets 401, a viewer 403 and no Split, Reschedule or Cancel link, billing 403 on Cancel.

**Findings (C).**

| # | Severity | Where | Finding | Outcome |
|---|---|---|---|---|
| 4 | MAJOR | `installments.lines_refusal` | An amount whose `quantize` exceeds the Decimal precision (`1E+400`) raised `InvalidOperation` out of the view: a 500 from user input | Resolved: compared against `AMOUNT_MAX` before quantizing, `InvalidOperation` caught; refused with the line-amount sentence; test added |
| 5 | MAJOR | `pnl._add_installments` | An installment payment was counted in the parent's month, so a payment in a later month rewrote a closed month's P&L; every other collected figure is by `settled_on` month (plan D12 said "the parent's month": Fable's wording, amended) | Resolved: grouped by `settled_on` month under the parent's category; Opus's equality test now settles in a later month |
| 6 | MAJOR | `lease_end.blockers` | A live installment plan on a charge of the lease did not block the end: the last-month proration would cut a parent whose installments still bill the original split, and the after-end waive would waive a parent whose installments stay owed | Resolved: blocker kind `installment_plan` per live plan with the plan link; confirm and the transfer inherit it; tests added |
| 7 | MINOR | `grid` | A twice-monthly cell where one debit is split shows only the plan's progress, not the other debit's figure | **Deferred** (written decision: the second debit keeps its own charge page and open balance; the cell text is a summary; fix with the next grid change) |

Design choices accepted: reschedule reuses `schedule_create` and `invoice_create` with a
`reschedule` parameter (no new job kind, no payments migration); `0.05` split in two gives
`0.02` and `0.03` (D9's rounding; §6's "refused" line was wrong and is amended); the collection
mapping (`charge_now` → e-mail, setup's `none` → manual, rent on a provider-ACH lease →
autopay, else manual); cancel refused while an installment is part paid, pending, re-debiting or
waiting on a provider request; the plan status stored and re-derived on every installment
recompute and return resolution; deposits, re-debits, NSF fees and installments never split.
**For the owner (scope):** by plan D9 a parent with a live Stax invoice or schedule is not
splittable, and cancelling that invoice voids the charge, so in practice only charges collected
by hand (an operator without a Stax customer, rent on a non-ACH lease) can be split today; the
remedy is a split that deletes the parent's provider object without voiding it (a new worker
path), proposed as a follow-up for the owner's decision at the checkpoint.


### D14 (debt after a transfer; owner's decision 3 of 2026-10-14)

**Scope.** `backend/ledger` (`models.py` four `lease_transfers.debt_override_*` columns with an
all-or-nothing CHECK; migration `0039_transfer_debt_override` with the guard trigger recreated
over the new text column; `services/transfer.py` `debt_of`, `override_debt`, the confirm's two
refusals and audit; `services/attention.py` kind `lease_end_balance`; `views/transfers.py`,
`forms.py`, `urls.py`, templates `transfers/confirm.html`, `confirmed.html`, new
`_debt_allowed.html`), `accounts/registry.py`, tests. No new dependency or environment variable.

**Commands run (final code, D14 included; the whole tree re-gated).**

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .`, `uv run ruff format --check .` | n/a | 0 | clean, 465 files formatted |
| `uv run mypy` (strict) | n/a | 0 | no issues in 378 source files |
| `uv lock --check` | n/a | 0 | consistent |
| `manage.py makemigrations --check --dry-run` | sqlite, PostgreSQL 16 | 0 | No changes detected |
| `uv run pytest -m "not e2e" -q -o faulthandler_timeout=300` | sqlite | 0 | 5,011 passed, 618 skipped; 11:16 wall |
| same | PostgreSQL 16 | 0 | 5,626 passed, 3 skipped; 21:37 wall |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 7 passed |
| `bash scripts/check-no-secrets.sh`; pattern scan of the diff since `b3ad928` and every new file | n/a | 0 | clean |

**Tests (Gate 2).** Opus: `test_transfer.py` (+17: refused with the sentence and nothing
written; override with a reason → fields, audit, confirm, the charge on the outgoing operator;
reason required; billing refused by service and route; a card number refused; debt grown refused;
zero debt refused; a confirmed transfer refused; a balance that dropped still confirms at the lower
amount; the confirm and confirmed pages), `test_attention.py` (+7: the item marked for a transfer,
unmarked for a plain end, gone when paid, waived or voided; dashboard), seeded PostgreSQL round
trip of 0039 over an existing transfer row. Fable (`test_plan012e_d14_adversarial.py`, 6 cases):
an override cannot be forged onto another transfer by posted ids, nor by viewer, billing or
anonymous (403/401), and the route has no GET page (405); an override never bypasses another
blocker (a live installment plan added after the override still refuses the confirm, nothing
written, the override untouched); an override without debt is refused by service and route and
writes nothing, and the confirm then needs none; the debt cannot grow behind an override; the
reason never reaches the operator's statement PDF or the e-mail's subject, text, html or
attachment while the audit keeps it; the lease-end balance stays on the attention list (viewer's
dashboard too) through a part payment and leaves it when paid in full.

**Findings (D14; one rework cycle, test files only).**

| # | Severity | Where | Finding | Outcome |
|---|---|---|---|---|
| 8 | MAJOR | `test_ledger_models` | The new CHECK `lease_transfers_debt_override_complete` had no constraint case (the file's every-constraint rule failed on both engines) | Resolved: two cases and one extra rejection in `test_ledger_models` (empty accepted; reason without the moment, moment with a blank reason, and a complete override without the amount refused) |
| 9 | MAJOR | `0039` / `test_card_number_guard` | On PostgreSQL the guard-trigger test writes a reason alone by a raw update, which the all-or-nothing CHECK refused (`CheckViolation`) | Resolved without touching the schema: the guard test's existing companion-value convention (as for `contact_entries.delete_reason`) now seeds a complete override, so the trigger is what the test exercises; the strict CHECK stands |

Design choices accepted: the override form's errors
as flash messages (the session holds one form at a time); allowing again replaces the earlier
allowance with the old one in the audit's `before`; a balance that dropped below the allowance
confirms at the lower amount; the user shown by username; the GET of the override route is 405
after the role check (a viewer gets 403 first).

**Confidentiality gate (§5.4).** Secret scans clean on every gate; no secret-store read by either
model; no new credential-capable file type; no new secret name; the PDF is generated from our
own rows and checked by the 10c guard before storage.

**Residual risk.**
- The proration rounding rule (ADR-0012 §2) is the owner's to confirm; the alternative is one
  line and one test.
- `reportlab` and `pillow` are new in the image; the PDF is text only (no images), so pillow is
  unused at runtime but present.
- No browser test drives the wizards or the stop-autopay page; the owner's staging clicks are
  the browser check.
- The PostgreSQL suite takes 21:37 on this machine (CI ran the 18:40 suite in 12:47) of the 25-minute CI step; BACKLOG §4 stands.
- Real Stax behaviour of `update_invoice` (due date) and of `update_scheduled_invoice` with a
  new one-occurrence rule is exercised only on staging (TODO-VERIFY-029).
- Two confirms at the same moment are tested with threads on PostgreSQL only; the row lock and
  the partial unique index are the guard.
- No browser test drives the split, plan, reschedule or cancel pages; the owner's staging clicks
  are the browser check.
- Real Gmail with an attachment and real Blob storage are exercised only on staging.

**Verdict:** PASS (Batches A, B, C and D14; three MAJOR in A/B and three in C resolved; one MINOR in C deferred by written decision; residual risk as listed).

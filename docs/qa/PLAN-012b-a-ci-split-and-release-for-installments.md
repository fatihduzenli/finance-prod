# PLAN-012b-a — CI: the migration round trips as their own step; splitting a charge that is live at Stax

**Date:** 2026-10-14 · **Origin:** the owner's decision 4 of 2026-10-14 (*"splitting a charge with a
live Stax invoice or schedule deletes the parent's Stax object without voiding the charge and
recreates collection per installment. Do it right after this push, before Step 12d."*) and the
owner's order of 2026-10-14: *"(1) split the seeded migration round trips into their own CI step
with its own limit, keep the unit step under 15 minutes; (2) splitting a charge with a live Stax
invoice or schedule. Gate each part separately and report; nothing is pushed until I confirm the
clicks."*
**Lead/QA:** Fable · **Implementer:** Opus (part 2 only; part 1 is verification-only CI config,
Fable's) · **Ships:** one push after both gates and the owner's confirmation of the 12b clicks.

## 1. Restated objective

Two independent parts. **Part 1:** the CI unit step ran 22:11 of its 25-minute limit on the 12b
push; the seeded PostgreSQL migration round trips (`test_migrations_roundtrip.py`) and whatever
else the measurement shows to be slow move to their own step (or job) with their own limit, so
the remaining unit step runs under 15 minutes. **Part 2:** today a one-off charge whose own Stax
invoice or schedule is live cannot be split into installments, because cancelling that invoice
voids the charge; the split now **releases** the parent's Stax object (deletes it at Stax, marks
the local invoice no longer live, keeps the charge owed at its price) and then creates the
installments' own invoices or schedules, each collected as the parent was. Rent charges are not
in part 2: a rent invoice is cancelled without voiding the rent charge already, after which the
split is offered.

## 2. Affected packages

Part 1: `.github/workflows/ci.yml` only (Fable; a pytest marker or `-k`/file selection, no
production change; `docs/BACKLOG.md` §4 closed). Part 2: `backend/ledger/services/installments.py`
(refusals, confirm), `services/one_off_charges.py` (the released invoice), `services/attention.py`
(one new kind), `payments/one_off_invoices.py` (the release of an invoice or a schedule),
`payments/jobs/runners.py` (the `release` parameter on the delete jobs), templates of the split
page, the plan page and the one-off charge page, tests. Out of scope: rent charges' invoices, the
multi-line builder invoice (refused as today, with a clearer sentence), new job kinds or
migrations (the delete jobs take a parameter as the reschedule did), Stax payment-method changes.

## 3. Decisions

### Part 1 (Fable)

- **D1 — measurement first.** A PostgreSQL run with `--durations=80` and a timed run of
  `test_migrations_roundtrip.py` decide what moves. Goal on CI: the unit step under 15 minutes.
- **D1a — the step split.** `test_migrations_roundtrip.py` (and, if the measurement says so, the
  other seeded migration tests named in BACKLOG §4) run as their own step **"Migration round
  trips"** with `timeout-minutes` set from the measurement plus margin; the unit step deselects
  them (`--deselect`/`--ignore` of the file, or a marker `migration_roundtrip` registered in
  `pyproject.toml` only if Opus adds it, since `pyproject.toml` is not Fable's; Fable uses
  `--ignore=` on the file, which needs no marker). If the remainder is still above 15 minutes on
  CI, the unit tests become **two parallel jobs** (each with its own PostgreSQL service) split by
  test-file halves listed in the workflow, each under 15 minutes; the deploy job `needs` both.
- **D1b — nothing else changes**: the same command options, the same database, the same `-m
  "not e2e"`; e2e and the secret scan steps stay.

### Part 2 (Opus)

- **D2 — what may be released.** In `installments.split_refusal`, a **one-off** parent whose own
  live provider invoice is **single-charge** (the parent is the only charge the invoice bills:
  `one_off_charges.billed_with(parent)` is the parent itself and no other line) no longer refuses
  with `LIVE_INVOICE`; a one-off parent with its own provider schedule (`autopay_due`, one
  occurrence, no invoice generated yet) no longer refuses with `LIVE_SCHEDULE`. The preview and
  the confirm page then say, in `id="release-notice"`: *"Invoice INV-… at the payment provider
  will be deleted (the charge stays open here) and each installment is collected on its own."*
  (or *"The payment provider schedule of this charge will be deleted …"*). **Still refused:** a
  parent that is a line of a multi-line invoice while that invoice is live (new sentence
  `LIVE_INVOICE_SHARED = "Invoice {number} has other lines; it cannot be released for one of
  them. Collect it or cancel it first."`); a parent whose schedule has generated its invoice
  (`PENDING`-like: *"The schedule already generated its invoice; collect or cancel it first."*);
  rent parents with a live invoice (unchanged sentence, unchanged behaviour); the other refusals
  as today.
- **D3 — the release job.** `create_plan` on such a parent creates the plan and the installments
  as today but **queues no installment provider job yet**; instead it queues the parent's
  existing delete job with a parameter: `invoice_delete` or `schedule_delete` with
  `params.release = True` (`installments.RELEASE_PARAM`), audited on the parent as
  `one_off_charge.release_requested` (plan id, invoice number or schedule ref masked as the
  adapter masks). The runners dispatch on the parameter to new
  `one_off_invoices.release_invoice(charge_id, actor)` / `release_schedule(charge_id, actor)`:
  the provider delete exactly as the cancel path does it (404 counts as gone; the listing check
  as `delete_schedule` does), then **locally**: the invoice's `internal_status = void`,
  `provider_status = DELETED`, audited `invoice.released_for_installments`; the schedule's
  external ref superseded as the cancel path does; the charge **stays** `owed` at its price and
  `amount_paid`; then, in the same transaction, if the parent's plan is still live and the
  parent's balance still equals the plan's total (no payment landed meanwhile), one
  `invoice_create` or `schedule_create` job per installment is queued (as `create_plan` does for
  a parent with nothing live); the plan is audited `installment_plan.released`. If the plan is
  no longer live (cancelled meanwhile), nothing more is queued. If the parent's balance is below
  the plan's total (a payment settled on the old invoice before the delete), the plan becomes
  `attention` (audited, reason `parent_paid`), no installment job is queued, and the attention
  list shows the new kind **D4**. A provider error leaves the job failed with the existing
  failure surface on the one-off charge page (the "Needs review" text and **Retry**, which
  re-runs the latest job with its parameters); the plan page shows *"Waiting for the payment
  provider to release invoice INV-…"* (`id="release-pending"`) while the release job is active or
  failed. The release job is **idempotent**: run again after success it finds the object gone,
  changes nothing, and queues the installment jobs only if none exists for the plan yet.
- **D4 — attention.** New kind `SPLIT_CHARGE_PAID = "split_charge_paid"` (HIGH): one item per
  live plan in `attention` whose parent's balance is below the plan's total, text *"Payment on a
  split charge: <store>, <parent>, <amount> paid before its invoice was released; cancel or
  reschedule the plan"*, link to the plan page; cleared when the plan is cancelled or completed.
  The plan status `attention` therefore has two causes (a returned installment, a payment on the
  parent); `plan_status` returns `attention` for either and `active` once neither holds.
- **D5 — the charge page and the invoice page.** The one-off charge page shows, for a released
  parent, *"Invoice INV-… was released at the payment provider for the installment plan"* in the
  invoice block (the invoice row is void with that note); the invoice page's Split hint
  (PLAN-012e D10) changes to *"Split into installments from the charge page; the invoice is
  released at the payment provider when the plan is confirmed."* for a single-charge live
  invoice, and keeps *"split each line from its charge page"* for multi-line ones (whose lines
  then refuse with `LIVE_INVOICE_SHARED`).

## 4. Interface contract

`installments.RELEASE_PARAM = "release"`; `installments.split_refusal` as D2;
`installments.release_notice(parent) -> str` ("" when nothing is live);
`one_off_invoices.release_invoice(charge_id, actor) -> Result`,
`one_off_invoices.release_schedule(charge_id, actor) -> Result`; runners dispatch on
`params.release`; `attention.SPLIT_CHARGE_PAID`; `one_off_charges.plan_status` with the second
cause; audit actions `one_off_charge.release_requested`, `invoice.released_for_installments`,
`installment_plan.released`, `installment_plan.status_changed` (existing) with `reason`. No
migration, no new job kind, no new environment variable, no provider call the adapter does not
already make.

## 5. Work breakdown

| Part | Owner | Files | Done when |
|---|---|---|---|
| 1 | Fable | `.github/workflows/ci.yml`, `docs/BACKLOG.md` | the round trips run as their own step with their own limit; the unit step runs under 15 minutes on CI (verified on the next push's run; locally by the measurement) |
| 2a | Opus | `installments.py`, templates `installments/split.html`, `plan.html`, `one_off_charges/detail.html`, `invoices/draft.html` | D2, D5 refusals and notices |
| 2b | Opus | `payments/one_off_invoices.py`, `payments/jobs/runners.py`, `one_off_charges.py` | D3 release jobs, idempotent, audited, the installment jobs queued after |
| 2c | Opus | `attention.py`, `one_off_charges.plan_status` | D4 |
| 2d | Opus | `tests/test_installments.py` (+ `test_attention.py`) | §6 |

## 6. Test plan (before implementation)

**Part 1:** the workflow lints (`actionlint` if installed, else a YAML parse); the step selection
runs locally: `pytest tests/test_migrations_roundtrip.py` alone and `pytest -m "not e2e"
--ignore=tests/test_migrations_roundtrip.py` together cover every test the single step covered
(counts add up to the previous total); the limit of each step is the measured time plus at
least 50 % margin.
**Part 2 (Opus):** an e-mailed one-off parent with a live single-charge invoice: the split is
offered with the notice; confirm creates the plan and installments, queues one release job and
no installment job; the worker's release calls the provider delete once with the invoice's
reference, marks the local invoice void/DELETED, leaves the charge owed at its price, queues one
`invoice_create` per installment, which then create one provider invoice each (fake); the same
for an `autopay_due` parent with a schedule (schedule delete, then one schedule per
installment); a multi-line live invoice refuses with `LIVE_INVOICE_SHARED`; a schedule that
generated its invoice refuses; a rent parent with a live invoice refuses as before; a provider
error on the release leaves the job failed, nothing created for the installments, the plan page
"Waiting…", Retry re-runs it and succeeds; a payment settled on the parent between the split and
the release → plan `attention`, the item, no installment job, cancel returns the remainder; the
release run twice changes nothing and queues no second set; a plan cancelled before the release
runs → the release deletes the provider object and queues nothing; the lease-end blockers see no
live invoice after a release; `invoice_line_links --check` passes with a released single-line
builder invoice; the one-off charge page and the invoice page texts.
**Adversarial (Fable):** a release job cannot be pointed at another charge's invoice by its
parameters; a replayed release never queues the installments twice; the parent cannot be split
again while its release is pending; a viewer sees no release notice or button; a provider
payment that arrives on the parent after the release (a late webhook) is recorded on the parent
and puts the plan in `attention`, never on an installment; totals never double count with
released parents in the random property test.

## 7. Risk register

- Money: a payment on the old invoice between the split and the delete; handled by D3's
  balance check and D4's item, and by the owner's staff cancelling the plan.
- Provider: a delete that fails leaves the parent's invoice live and the plan without
  installment objects; the failure is visible and retryable; nothing is voided.
- CI: a split step changes no test; a parallel job doubles the PostgreSQL service.

## 8. Acceptance criteria

Part 1: the next CI run shows the unit step under 15 minutes and the round-trip step within its
limit. Part 2: gate green on both engines; the §6 cases present; on staging a split of an
e-mailed charge with a live Stax invoice deletes that invoice at Stax (visible in the Stax
dashboard) and creates one invoice per installment; the charge is never voided.

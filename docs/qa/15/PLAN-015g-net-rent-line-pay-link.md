# PLAN-015g — the rent line net to the processor, no hosted pay link in the invoice e-mail, the address in the Django admin, the phone meta line (owner, 2026-11-09)

Plan by Fable. Approved by the owner on 2026-11-09 ("Confirmed, act on the pasted list, in your planned order"): items 2, 3 and 6 of that list and the Django admin address fields, as one Opus plan. Gate class: **full** (the processor's invoice line is a money path; a data migration).

## 1. Objective

Four changes. (A) A rent line with a free-rent concession goes to the processor at its **net** amount, with the concession named in the line's details, and no `discountRate` is sent: the sandbox kept the rate and the net total but its hosted page showed the line gross with no discount (TODO-VERIFY-075, closed). Our own invoice page and PDF keep the rent line and the concession line unchanged. (B) The processor's hosted pay link leaves the app's invoice e-mail: its page offered card payment although the invoice is ACH-only (TODO-VERIFY-076; Rule 1). The portal pay link stays. (C) The Django admin's Company settings form shows and saves the company address fields. (D) On a phone the store link in a Notes & follow-ups entry's meta line no longer stretches the wrapped line's height, and keeps a 44 px touch target.

## 2. Packages

`backend/` only: `payments/one_off_invoices.py`, `payments/provider.py`, `payments/stax/mapping.py`, `payments/stax/validation.py` (only if the removed field is read there), `payments/__init__.py`, `ledger/services/invoice_messages.py`, `ledger/services/message_templates.py`, `ledger/merge.py`, one new ledger migration (0078), `ledger/admin.py`, `finance/static/finance/app.css`, tests, visual baselines. Out of scope: the invoice builder's own rows (`invoice_builder.py` — the CHARGE and DISCOUNT rows stay exactly as they are), the invoice page and PDF, the schedule (autopay) body, the portal, the settings page's own Company card, every other e-mail template, `docs/` (Fable wrote the register, ADR-0020 and CLAUDE.md §5 amendments in this branch already).

## 3. Interface contract

### A. The rent line to the processor

- `payments/one_off_invoices._provider_line(item)`: for a line whose `rent_charge.concession > 0` the `LineItem` is quantity 1 at unit price `line_total − concession` (the net), with `details` = the line's own details (when any) followed by the concession sentence, joined with `"; "`:
  `"Rent $2,000.00 less $666.67: Free rent per lease agreement (Dec 10 – Jan 8)"` — built by one small function next to `_provider_line` from `item.line_total`, `rent.concession` and `rent.concession_text` with the money formatter the module already uses; when `concession_text` is empty the sentence ends at the amount (`"Rent $2,000.00 less $666.67"`). The line's `item` text is what the existing not-exact branch already builds for a quantity-1 line (`text or item.description`). A line without a concession is built exactly as today.
- `payments/provider.LineItem` loses `discount` and `gross`; `total` is `quantity × unit_price` again; the discount bounds check goes. `payments/stax/mapping`: `discount_rate`, `_RATE` and the `discountRate` key go (no dead code; the TODO-VERIFY-075 comment goes with them). `_line_amounts` is unchanged in meaning: the sum of the lines' totals is the net, equal to the invoice's `total` and `subtotal`.
- A rent-alone invoice that is free in full is refused as today with nothing sent (`NOTHING_TO_BILL`). **Revised after review (Fable, 2026-11-09):** a Rent line free in full beside a billed line is left out of the lines sent to the processor, on create and update alike (the processor refuses a line at price zero, and the first build let the whole invoice fail); the other lines still sum to our invoice total, and our rows, page and PDF keep the rent line and its concession line.
- The update path (`invoice_update_body`) uses the same line builder and so sends the same net line.
- Nothing changes in `invoice_builder.py`, in the DISCOUNT rows, in `invoices/_invoice.html`, `draft.html` or `pdf/invoice.py`.

### B. The hosted pay link leaves the invoice e-mail

- `merge.INVOICE_FIELDS` loses `provider_pay_link`; `invoice_messages` loses `PROVIDER_PAY_LINK`, `provider_pay_link()` and the context key; `message_templates` loses `SAMPLE_PROVIDER_PAY_LINK` and the sample key; `payments.hosted_invoice_url` goes if nothing else calls it (it has one caller today). `without_lines_naming` and the `_queue` step that used it go only if nothing else uses them — check before removing. `DEFAULT_PAYMENT_PAGE_URL` and `payment_page_url` stay if `mapping.invoice_body` still takes it.
- Migration `ledger/0078_invoice_email_portal_link_only` (data only): on the `invoice_sent` template, remove **every line of the body that names `{provider_pay_link}`** (the seeded line `Or pay on our payment page: {provider_pay_link}` or an admin's own wording), since the placeholder stops being a merge field and a body still naming it would no longer validate. Write one `audit_log` row `message_template.provider_pay_line_removed` with the before and after bodies when the body changed (no row when it did not). A subject naming the placeholder (not seeded so; possible by an edit) has the placeholder text removed the same way. Reverse: when the body has the line `Pay by bank debit (ACH) here: {pay_link}` and no line naming the placeholder, re-insert the seeded line directly after it; otherwise leave the body (the reverse is best-effort for an edited body and says so in its docstring). No schema change. Add 0078 to the explicit migration lists of `tests/test_migrations_roundtrip.py` and `tests/test_card_number_guard.py`.
- The seeded text for a fresh database: 0071 stays as written (history is not edited); after 0078 the body reads

  ```
  Hello {operator_name},

  Your invoice {invoice_number} for store #{store_number} {store_name} is attached: {amount}, due {due_date}.

  Pay by bank debit (ACH) here: {pay_link}
  Paying by wire or Zelle? Put {invoice_number} in the memo.

  Questions? Write to {support_email}.

  {company_name}
  ```

  Tests that re-seed from `0071.TEMPLATES` (`tests/test_plan015c_email.py::seeded`) apply 0078's transformation too (import the migration's pure function; do not copy the text).
- The settings page's help text and the template editor's field list must not name the hosted link after this (check `settings/index.html` and the editor's placeholder list, which read `merge.fields_for`).

### C. The address in the Django admin

- `CompanySettingsAdmin.fields` gains `address_line1`, `address_line2`, `city`, `state`, `postal_code` after `support_email`; `audited_fields` follows `fields` as today, so a change is audited `company_settings.updated` with the address before and after. `invoice_sender` is **not** added (the owner asked for the address; the sender switch stays on Admin › Settings).
- Validation is the model's (max lengths 200/200/100/50/20; the payment-data guard of `guarded_char`). No new form class unless the model's validators are not run by the admin form — then the smallest fix that runs them.
- The existing admin tests post six keys; with the address in `fields` an omitted address would be blanked, so their form helpers (`tests/test_design_shell.py::_admin_form`, `tests/test_plan015c_settings.py::admin_form`) post the address keys too (re-pin; name it in the report).

### D. The phone meta line

- In `app.css`, in the same `@media (max-width: 1023.98px), (pointer: coarse)` block as the touch-target rule (do **not** edit the pinned selector list `td a, li a, .pagination a, …`), add after it: `.contact-meta a` is `display: inline; min-height: 0; position: relative;` and its `::after` is an absolutely positioned, transparent box centred on the link, as wide as the link and `var(--tap)` high, so the tap area stays 44 px while the line box is text height. The link's first line box is the reference when the label wraps; that is accepted.
- No template change. Applies wherever `_log_entry.html` renders (store page, operator page, both All-entries pages).

## 4. Work breakdown (one Opus session; one commit per task)

1. **A** — `_provider_line`, `LineItem`, `mapping`; re-pin `tests/test_plan015d_free_rent.py` (L275: the processor line is now `(price, total) == (600, 600.00)` with the details sentence and no `discountRate`; L340 and L362 lose their subject — replace them with tests of the new rule, do not leave empty tests; L350 stays true), `tests/test_stax_adapter.py` exact-body pins stay green unchanged. New tests: prorated rent line (quantity ≠ 1) with a concession; a line with its own details and a concession; empty `concession_text`; a mixed invoice (one-off + rent with a concession) — the one-off line untouched and the amounts sum to the net; the update body; fully free refused.
2. **B** — the removals and migration 0078 with its tests: forward on the seeded body; forward on an edited body naming the placeholder in other words; forward on a body not naming it (unchanged, no audit row); reverse; the roundtrip and guard lists; `merge_field_errors()` empty for the seeded template after 0078; the e-mail body test (`test_plan015c_email.py` L182) re-pinned to the portal link alone and asserting no `staxpayments`/`#/bill/` text; the preview test.
3. **C** — the admin fields and tests: an admin saves the address in the Django admin (audited with before/after); too long refused; a card-number-like value refused with the guard's sentence and nothing saved; below admin cannot.
4. **D** — the CSS and an e2e test at 390 in `tests/e2e/test_menus_015f.py` (or a new small file): a two-line meta paragraph's height is at most 2 × its computed line-height + 2 px; the link's `::after` computed height is 44 px; the existing phone assertions still hold. Regenerate the 390 baselines that show an entry (`store-contacts`, `store-log-contact`, `operator`, `operator-log-contact`, `store-charges`, `operator-form` if it shows one; both themes) with `docs/qa/tools/visual.sh "store or operator"`; list every changed PNG; no 1280 baseline may change.
5. **Self-review and checks** (§6).

## 5. Test plan (Fable verifies)

Happy paths as in §4. Boundaries: concession of one cent; concession = amount − 1 cent; rent line with quantity 0.5 (proration) and a concession; `concession_text` with unicode and 200 characters (the details field's limit at the processor is unknown — the existing details length rule applies; say what it is). Error paths: fully free refused, nothing sent, the job's failure sentence unchanged; an edited template body made empty by the removal (every line named the placeholder) must not be saved empty — if the removal would leave an empty body the migration leaves the row untouched and writes the audit row `message_template.provider_pay_line_kept` with `{"reason": "would_be_empty"}`; Opus then reports it, since such a body would fail validation at send. Adversarial: a hand-posted template body naming `{provider_pay_link}` is refused by the editor as an unknown field after this change; no test or fixture leaves the hosted URL in an e-mail body; the Django admin refuses a 16-digit number in `city`. Rule 8: no `stax` literal outside `backend/payments/` (the new migration and the ledger tests name the processor only through existing helpers). Query budgets unchanged.

## 6. Risks

- Money: the net line must equal our invoice total to the cent — `_line_amounts` sums the lines, the validation requires the sum to equal the subtotal; a test asserts it for a prorated line.
- The processor's reconciliation and webhook handlers match on the invoice id and total, not on line items (Opus confirms by grep that nothing reads `discountRate` or a line's discount from a fetched invoice).
- Invoices already sent with a `discountRate` exist only on the sandbox; no data repair.
- Migration 0078 edits staff-editable text: it removes only lines naming the placeholder and audits the change.
- The shared touch-target rule is pinned by tests; add, do not edit.
- No new dependency; no route change (FROZEN_ROUTES untouched); no new choice value.

## 7. Acceptance criteria

1. For a rent line with a concession the processor body's line has `price` = `total` = the net, `quantity` 1, the details sentence of §3A, and no `discountRate` key anywhere in the body; `total` and `meta.subtotal` equal our invoice total.
2. `git grep -n "discountRate\|discount_rate" backend/` returns nothing; `git grep -n "provider_pay_link\|hosted_invoice_url" backend/` returns only migration 0071, 0078 and their tests.
3. After 0078 the `invoice_sent` body has no line naming `{provider_pay_link}`; `merge_field_errors()` is empty; a queued invoice e-mail's body contains the portal link and no processor URL.
4. Our invoice page and PDF for the same invoice are byte-for-byte what they were (the existing PDF and page tests pass unchanged).
5. The Django admin's Company settings form shows the five address fields, saves them and audits them; the guard refuses payment data.
6. At 390 the entry's meta line wraps at text line height and the store link's tap box is 44 px high; no 1280 baseline changed.
7. Full gate green locally and in CI; no new ruff or mypy warning; secret scan clean.

## 8. Assumptions (the work proceeds under them)

- The details sentence wording of §3A. Say if another wording is wanted on the processor's invoice.
- A Rent line free in full is left out of the processor's invoice beside billed lines (told to the owner 2026-11-09). Say if such an invoice should be refused at the builder instead.
- `invoice_sender` is not added to the Django admin form.

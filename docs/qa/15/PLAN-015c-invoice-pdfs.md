# PLAN-015c — one invoice PDF, receipt PDFs, downloads on the portal and the staff pages

**Owner's request (2026-11-07) and decisions (2026-11-07), queued after PLAN-015b under the same
approval; gate class full (a migration, money documents, a new e-mail path).** References:
`docs/design/invoice-template-unpaid.png`, `docs/design/invoice-template-paid.png`. Plan by Fable;
implementation by Opus; gate and sign-off by Fable. The record is `docs/qa/15/QA-015.md`.

## 1. Objective

One invoice PDF, rendered on request from stored data, is what every invoice download and e-mail
carries: a header band in the sidebar gradient with the company logo, name and address, "INVOICE"
and the number, a status stamp (UNPAID red, PARTLY PAID orange, PAID green, VOID grey); an info panel
(Bill to, Store, Issued, Due); the line items; the totals with each payment applied and a dark
"Balance due" box; a "How to pay" panel; a footer with the company, the support e-mail and "Page x of
y". A matching receipt PDF exists for every settled payment. The operator portal and the staff pages
get "Download PDF" and "Receipt PDF" links, the portal only for the operator's own stores. A new
setting decides who e-mails invoices: the processor (today, the default) or the company — then the
app's e-mail carries the PDF, the portal pay link and the processor's hosted pay link. The app e-mails
its receipt PDF only for payments recorded outside the processor. No PDF is stored.

## 2. Scope

**In:** `backend/ledger/` — a new `services/pdf/` package (`frame.py`: the shared page frame, fonts,
colours, gradient, stamp, footer with page numbers; `invoice.py`; `receipt.py`), `models.py` +
one migration (`CompanySettings`: `address_line1`, `address_line2`, `city`, `state`, `postal_code`,
`invoice_sender` with choices `processor`/`company` default `processor`, `logo_png` bytes — the copy
the renderer and the portal read; `MessageRule`/`ChargeEntityType` values for `invoice` and
`receipt`; seeded `MessageTemplate`s `invoice_sent` and `receipt`), `services/messaging.py` (the two
kinds, the PDF attached at delivery), `services/invoice_builder.py` (the preview's company name from
`CompanySettings`; the send path's choice of sender), `payments/one_off_invoices.py` (no provider
e-mail when the company sends; the hosted pay link), `views/invoices.py`, `views/payments.py`,
`views/settings.py` + the settings template (the address, the sender choice), the store tabs and
the invoice and payment pages (links), `urls.py`, `accounts/registry.py`; `backend/portal/`
(two download routes, rows with ids, "Partly paid"); `backend/finance/static/finance/fonts/`
(Inter Regular and Bold TTF, OFL) and `brand/monvera_logo_dark_transparent.png` (the fallback logo);
`docs/DECISIONS.md` ADR-0020 and `docs/CLAUDE.md` §5 (Fable writes both); the tests.

**Out:** storing PDFs; the lease-end statement's renderer (untouched); the Stax hosted page itself;
the mail backend; portal ACH (its link is what the portal Pay page already is); refunds; the
`COMPANY_NAME` environment variable's other readers (only the invoice preview changes, decision 4).

## 3. Interface contract

**The frame** (`ledger/services/pdf/frame.py`): `Frame(company: CompanyFacts, title: str,
number: str, stamp: Stamp | None)` draws Letter pages with reportlab's `Canvas` (`invariant=1`,
`pageCompression=0` as `statements.render_pdf`): the band (a vertical gradient from `#0B2545` to
`#0E4A5C`, 150 pt tall; the logo at the left from `CompanySettings.logo_png`, else the Monvera PNG;
the company name and address; "INVOICE"/"RECEIPT" small caps and the number at the right; the stamp
below the number as a rounded outline in its colour), the body area between 72 pt margins, and on
every page the footer "<company> · <support e-mail>" left and "Page x of y" right (a second pass
over the pages for "of y"). Fonts: Inter (TTF registered once) with Helvetica as the fallback when
the TTF is missing. Colours print in black and white (the band dark, text black on white; the
stamp's word carries its meaning). `Stamp` has `UNPAID` (red `#D14343`), `PARTLY_PAID` (orange
`#C8741A`), `PAID` (green `#1E8E5A`), `VOID` (grey `#6B7280`). Every text drawn goes through
`statements.long_date`-style dates and `validate_no_payment_data` on the full text in tests. The
bytes for the same data are the same bytes.

**The invoice** (`ledger/services/pdf/invoice.py`): `render_invoice(invoice: Invoice, *, today:
date) -> bytes` from `Invoice`, its `InvoiceLineItem`s, its settled `Payment`s (`invoice=` FK,
newest last) and the store through the lines' charges (`workspace.store_invoices`'s rule):
- the stamp: VOID when `internal_status` is void; else PAID when `amount_paid >= total`; else
  PARTLY PAID when `0 < amount_paid < total`; else UNPAID;
- Bill to: the lease billing's company name (`LeaseBilling.company_name`, else the operator's
  display name), then the operator's full name; Store: "#<number> <label>"; Issued; Due;
- lines: description + details (muted), qty, price, amount; the header row repeats on every page;
- totals: Subtotal; one row per payment "Paid <Mon D> · <method label>" with the amount negative;
  "Balance due" = total − amount_paid in the dark box;
- How to pay: "Online: <portal pay URL> — bank debit (ACH)" (`portal.links` for the store's Pay
  page), "Wire or Zelle: put <number> in the memo" (`wording.pay_memo`), "Questions: <support
  e-mail>" (omitted when blank);
- `pdf_filename(invoice)` = `<number>.pdf`.

**The receipt** (`ledger/services/pdf/receipt.py`): `render_receipt(payment: Payment, *, today:
date) -> bytes` for a settled, non-refund payment: title RECEIPT, number `RCPT-<payment id's first
8>` (no stored counter), stamp PAID; a facts panel: Date (`settled_on` else `initiated_on`), Amount,
Method (`PaymentMethod` label; for a bank debit "Bank debit ····<last4>" from the lease billing's
cached last four only), Reference (the provider transaction reference through `mask_identifier`;
never a routing or account number; blank when none); "Applied to": the invoice number and each
charge's words ("Rent — November 2026", a one-off's description); "Remaining balance on the store:
$X" from `receivables.open_balances` for the store; `receipt_filename(payment)` =
`receipt-<RCPT number>.pdf`. A returned, refunded or voided payment has no receipt (404).

**Company settings**: the address fields (optional; the band shows the lines that are set, joined
", "); `invoice_sender` (`processor` default / `company`), shown on Admin › Settings as "Invoice
e-mails sent by: <provider name> / <company short name>" (the labels from settings, Rule 8);
`logo_png`: a PNG copy (≤ 1 MB, re-encoded with Pillow when the upload is JPEG) written when the logo
is saved, cleared when it is removed; readable without a staff actor. The settings form gains the
address and the sender choice; the existing settings page tests re-pinned.

**E-mail**:
- `MessageRule.INVOICE` ("invoice_sent", kind ladder? no: a new `MessageKind.INVOICE`) and
  `MessageRule.RECEIPT` with seeded templates: subject "Invoice {invoice_number} — {amount} due
  {due_date}" / body with `{amount}`, `{due_date}`, `{pay_link}` (the portal Pay page),
  `{provider_pay_link}` (the processor's hosted invoice page: the URL the provider returns for the
  invoice when it has one, else the merchant's payment page URL the adapter already uses;
  omitted when neither exists), `{support_email}`; the receipt: subject "Receipt for {amount}",
  body with `{amount}`, `{paid_on}`, `{invoice_number}` or the charge's words; placeholders added
  to `ledger/merge.py`'s allow-lists.
- `OutboundMessage` gains `invoice` and `payment` references (`ChargeEntityType` values) so
  `_deliver` renders the PDF at delivery (`render_invoice` / `render_receipt`) and attaches it (the
  mail `Attachment`, PDF only); no `Document` row.
- The send path: `invoice_builder.send` → when `invoice_sender == company`, the worker's
  `send_invoice_email` is not called and `messaging.queue` queues the invoice message to the
  billing e-mail (held as any message when mail is not live); when `processor`, today's behaviour,
  no app e-mail. The rent run's, NSF's and installments' invoices follow the same switch where
  they ask the provider to e-mail today.
- Receipts: when a payment is recorded with a method outside the processor (zelle, wire, check,
  cash) and settles, a receipt message is queued to the billing e-mail; processor payments get no
  app receipt (the processor's stays).

**Downloads** (FROZEN_ROUTES 323 → 327):
- staff: `ledger:invoices:pdf` `invoices/<pk>.pdf` (the invoice's pk, not the draft's) and
  `ledger:payments:receipt` `payments/<pk>/receipt.pdf`; `READ_ONLY_ROUTE_NAMES`; viewer and above
  (as `invoices:print`); `NOT_PAGES` in `test_plan012f_d_pages`; headers as `documents.open_for`
  (attachment, nosniff, `Cache-Control: private, no-store`); audit `invoice.pdf_downloaded` /
  `payment.receipt_downloaded` with the actor;
- portal: `portal:invoice_pdf` `invoices/<pk>.pdf` and `portal:receipt_pdf`
  `payments/<pk>/receipt.pdf`, before the catch-all; the invoice's operator and the payment's
  billing operator must be the signed-in operator and the store one of `store_of`'s, else 404;
  `PORTAL_ROUTE_NAMES` updated; audited with the portal user;
- links: the store Invoices tab (every row, drafts or not) and Payments tab, the invoice draft
  and preview pages, the payment detail page, the portal Invoices and Payments pages ("Download
  PDF", "Receipt PDF"); portal rows carry ids.
- the portal status label gains "Partly paid" (`0 < amount_paid < total`).

**Decision 4**: `invoice_builder.preview()` reads the company name from `CompanySettings`
(`current().name`), not `settings.COMPANY_NAME`.

## 4. Work breakdown (Opus; branch `design-v2/invoice-pdfs` from main; a commit per task)

1. Migration + settings: the fields, `logo_png` written on logo save (Pillow), the settings form
   and page, the seeded templates and the new enum values; tests (settings page, logo copy, the
   migration reversible).
2. The frame + the invoice renderer + `pdf_filename`; tests: each stamp state, the figures against
   the ledger (subtotal, payments, balance), a 40-line invoice over two pages with the header row
   repeated and "Page 1 of 2"/"Page 2 of 2", the fallback logo, the address lines, no payment data
   in the text, deterministic bytes.
3. The receipt renderer; tests: ACH with last four, Zelle with a masked reference, applied-to
   lines, the remaining balance, no receipt for a returned payment, deterministic bytes.
4. Staff downloads + links + registry + classification + audit; tests: roles, headers, the PDF
   bytes start `%PDF`, audit rows, rent-run invoices (no draft) downloadable.
5. Portal downloads + rows' ids + "Partly paid"; the isolation tests extended for both URLs (the
   other operator's invoice/payment 404, own 200, unknown and malformed ids 404); every portal URL
   still in the isolation walk.
6. E-mail: the kinds, the templates, the switch in the send paths, the receipt queueing, delivery
   with the rendered attachment; tests: with `company` the provider's e-mail call is absent and an
   invoice message with a PDF attachment is delivered through the memory mail; with `processor`
   unchanged (the provider call present, no app message); a Zelle payment queues a receipt, an ACH
   payment does not; held while mail is off.
7. Decision 4 + self-review + report.

## 5. Test plan (must pass before acceptance)

§4's, plus: an invoice with no payments and one with three partial payments (stamp, rows, balance);
a voided invoice (VOID, balance $0.00, payments listed); a portal user downloading another
operator's invoice by id (404, no audit of a download); a staff viewer downloading (200); Rule 8:
no "stax" literal in the new modules (`test_stax_isolation`); the e-mail body's links point at
`APP_BASE_URL`; the settings page shows the provider's name in the sender choice; the Outbox lists
the invoice and receipt messages with the right kind; the lease-end statement's tests untouched.

## 6. Risk register

- **A new e-mail path for money documents**: held by the Outbox until mail is live; the default
  keeps the processor sending, so nothing changes on staging until the owner flips the setting.
- **Hosted pay link**: the provider's invoice URL may not be returned by the API; the fallback is
  the merchant's payment page URL; if neither exists the placeholder is omitted — Opus reports which.
- **Logo bytes in the database**: ≤ 1 MB PNG; the Monvera fallback ships as a static PNG.
- **Fonts**: Inter TTF embedded; deterministic bytes checked by test; Helvetica fallback.
- **Portal isolation**: ids are UUIDs; the operator check is in the view and tested per URL.
- **FROZEN_ROUTES** moves by four; the registry and classification follow.

## 7. Gate class and acceptance criteria

**Full gate.** Accepted when every §5 case exists and is green on both databases; the PR's CI is
green; the migration applies and reverses; FROZEN_ROUTES 327; the ADR and the §5 amendment are in
the PR; the QA-015 entry names the counts and the verdict; a rendered invoice in each state and a
receipt are attached to the QA entry as images (Fable renders them in the gate).

## 8. Owner's decisions (2026-11-07) applied above

1. The app sends invoice e-mails when the setting says so (default: the processor); the e-mail
   carries the portal pay link and the processor's hosted link; ADR-0020; `docs/CLAUDE.md` §5.
2. Receipts: the processor's e-mail stays for its payments; the app e-mails its receipt PDF for
   payments recorded outside it; receipt PDFs are downloadable everywhere.
3. A company address (migration + settings page); the Inter font shipped.
4. A PNG copy of the logo readable by the renderer and the portal; the invoice preview's company
   name from company settings.
5. The portal shows "Partly paid" beside Void, Paid and Open.

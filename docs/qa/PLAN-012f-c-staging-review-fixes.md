# PLAN-012f-c — the owner's review of 12f-b on staging: four batches of fixes

Fable's Phase A plan for the owner's prompt of 2026-10-27 ("Step 12f-c — staging review of
12f-b: fixes"). Staging runs 12f-b at `a8b2195`. Same rules as 12f-b (plain words, the money
filter, dates as "Nov 1, 2026", no GET that writes, no secrets); one pull request per batch; the
QA record is `docs/qa/12f-c/QA-012f-c.md` with laptop and phone shots of every changed page;
only changed baselines are regenerated. Merges follow the owner's standing rule: push, PR and
merge in order, green CI before, a verified staging deploy after, stop on any red.

## 1. Objective

Four batches. **A** gives the payment page the actions it lacks: Void for a payment recorded
outside the processor (billing, with a reason, never a deletion), and Void and Refund for a
processor payment in the same dialog frame. **B** is six copy and small-UI fixes (the greyed
bank-debit card's two reasons, the Add payment badge, phone numbers formatted on screen, the
Reference help line, staff names on "Recorded by", the partial-payment notice as an info flash).
**C** rebuilds the Split into installments page in the Add payment family with a live preview
and a sticky summary, and adds its section to the design spec. **D** wires the USPS Addresses
client to its OpenAPI file and the two Key Vault secrets to the web app in Bicep, egress to
`apis.usps.com`; the check stays off until the owner stores the secrets.

## 2. Packages and scope

One Django project, `backend/`. Touched: `ledger` (services `refunds`, `one_off_charges`,
`rent_run`, `installments`, `attention`; views `payments`, `charges`, `one_off_charges`,
`installments`; forms; templates; static; one migration in A), `finance` (templatetags, help
topics, `usps.py` only for a spec-conformance test), `infra` (Bicep modules and parameters, D
only), docs, tests. Out of scope: the provider adapter, any new provider call, the autopay and
settlement debit paths (TODO-VERIFY on sandbox), version bumps, Step 13 (the migration job's
split, the double workflow run, C2-3).

**Batch A, the owner's history question — answered from the record.** Refund and Void have
existed since PLAN-009 (commit `c78ed21`, the payments ledger) for payments **made through the
processor** (ACH, with a provider transaction reference), admin only, behind confirmation pages;
the 12f-a redesign kept them in the header as "Refund" and "More ▾ › Void" and lost nothing.
They are hidden on a payment recorded outside the processor because `refunds._common_refusal`
refuses any payment whose method is not ACH or that has no provider transaction. A void for a
Zelle, wire, check or cash record has **never existed**: it is new in this batch.

## 3. Decisions

- **D1. Branches and PRs.** `step-12f-c/batch-a` … `batch-d` from `main`, stacked in order (each
  PR's base is the previous branch, retargeted to main before its merge). Each branch carries
  its QA section.
- **D2. Void of a manual payment (A).** `refunds.void_manual_payment(payment_id, *, reason,
  actor, creation_key)` beside `void_payment`: billing and above; the payment must be a settled,
  non-refund, non-deposit-refund payment recorded outside the processor (method not ACH); a
  voided one is refused ("This payment is already voided."); the reason is required (free text,
  the card and account guard, at most 200 characters) and stored on the payment
  (`Payment.void_reason`, migration **0057**, guarded short text, `GUARDED_COLUMNS` names it;
  `void_note` exists on one-off charges — the payment gets its own column, nothing is reused).
  One transaction under the payment's and the charge's row locks: status `voided`, `voided_at`,
  `voided_by`, the reason; the charge recomputed (`rent_run.recompute_charge` /
  `one_off_charges.recompute`, which already fold `voided` payments out of the paid amount), so
  Paid becomes Partly paid or Unpaid by the figures; one audit row `payment.voided_manual`
  (actor, when, reason, the charge's before/after). The replay key is the dialog's
  `creation_key`. A viewer, a pending or returned payment, a refund, a processor payment: refused
  with nothing written.
- **D3. The marked-paid invoice (A) — departure with the reason.** The owner asked that a Stax
  invoice marked paid because of this payment be "reopened the same way the existing reversal
  path does". **No such path exists**: the app marks an invoice paid in cash/check at the
  processor (`payment_mark`), and the processor's invoice statuses are system-managed (Rule 2) —
  there is no unmark call and nothing in the code reverses a marking. So the void writes locally
  and, when a marking job for this payment succeeded, (a) the dialog says so beforehand ("The
  Stax invoice {number} was marked paid by this payment; after the void, unmark it in the Stax
  dashboard."), (b) the void creates the attention item `invoice_marked_paid_void` (HIGH, on the
  charge, "Invoice {number} is marked paid in Stax for a voided payment — unmark it in the Stax
  dashboard"), cleared by the existing refresh when the processor shows the invoice open again,
  and (c) the audit row carries `provider_marking: "to unmark"`. The processor's name comes from
  the company settings. §9 asks the owner to confirm.
- **D4. The installment-plan sentence (A).** When an installment plan on the charge was created
  after the payment's `paid_on` (any plan status), the dialog adds the owner's sentence with the
  amount: "The installment plan was built on the remaining balance; voiding adds {amount} back to
  the charge, not to the plan." The plan is untouched (its lines keep their amounts); the charge's
  balance rises; `rent_run.own_paid` / `one_off_charges` recompute as today.
- **D5. The dialog frame (A).** One template `ledger/payments/void_manual.html` on the States
  spec's confirmation dialog over the payment page (`{% dialog size="sm" open=True
  return_url=… %}`, the one sentence of consequence "Voiding returns {amount} to {charge}; the
  record stays, marked void.", the Reason field, "Keep the payment" and the red "Void payment").
  The processor void (`ledger:payments:void`) and Refund (`ledger:payments:refund`) move into the
  same frame (they are plain pages today): their forms and POST handlers are unchanged. Route
  `ledger:payments:void_manual` (`<pk>/void-manual/`, billing, POST; GET renders over the page,
  writes nothing); the registry; the route digest re-pinned (274).
- **D6. The header actions (A).** For a manual settled payment: "More ▾" with the red item
  "Void payment" (billing). For a processor payment: Refund (admin, settled) and "More ▾ › Void"
  (admin, pending or settled, not refunded) as today, now opening the dialog frame. A voided
  payment: no actions, the status badge reads Voided, the reason and who/when under DETAILS. The
  charge pages' payment rows show Voided with the amount struck through (the existing `voided`
  rendering for processor voids, extended to manual voids if it keys on the status alone).
- **D7. The greyed card's reasons (B.a).** `BankDebitOptions.reason` becomes the first line and a
  new `reason_more` the second: when the operator has no bank method **and** the flag is off, the
  first line is the owner's sentence "No bank account on file — send {operator} the {provider}
  link to add one" and the second "Bank debits are also off at {provider}."; with one reason only,
  the one line as today. **Flag for the owner (§9):** "send the Stax link" conflicts with the
  2026-09-29 decision that operators never see a processor page and staff enter bank methods in
  the dashboard; the sentence is built as written because the owner stated it twice, and the
  app sends nothing — it is copy that tells staff what to do.
- **D8. The Add payment badge (B.b).** The header badge reads "● Due · $2,100.00" through the
  existing `status_badge` with the dot (no trailing "due"); the muted line keeps "due {date}".
- **D9. Phone numbers (B.c).** A template filter `phone` in `finance/templatetags/phones.py`:
  ten digits, with or without a leading 1, render "(123) 456-7822"; eleven-plus digits with a
  country code other than 1 render "+CC …" as stored; anything else renders as stored. Applied
  wherever an operator's `phone` or `sms_phone` is printed (operator page and side card, the
  store workspace's phone line, the Add payment and builder summaries, the calendar's day panel,
  the wizard steps). Inputs are untouched; the stored value is unchanged.
- **D10. Reference help and the Payments topic (B.d).** The Reference field's help reads "Never
  an account number."; the sentence about Zelle confirmations, wire references and check numbers
  moves to a new help topic `payments` ("Payments", in `finance/help.py` TOPICS after "How an
  invoice is collected") that also receives the void rules of this step and the partial-payment
  rule. The Add payment page's "Learn more" link, if it has one, points there.
- **D11. Recorded by (B.g).** A helper `accounts.display.staff_name(user)` returns the full name
  when first or last name is set, else the username, never the e-mail; the e-mail is the
  `title` attribute of the rendered name. Applied on the payment page, the charge pages' payment
  tables and any other "Recorded by" / "by {user}" cell (Opus greps `recorded_by` and the audit
  "by" cells and names each).
- **D12. The partial-payment notice (B.h).** The Charge card's bold line "Not marked paid in
  {provider}: part of the amount." becomes the info flash style of the States spec (the
  `.toast`-style info box inline, not a dismissible message): "Partial payment — the {provider}
  invoice stays open until the rest is paid." The other `MARK_FAILED` texts (a real failure)
  keep the strong style.
- **D13. Split into installments (C).** The page moves to the Add payment family's frame:
  breadcrumb Stores › #no Name › charge › Split into installments; title "Split {charge} into
  installments" with the charge's status badge; the muted line "{charge} · #no Name · operator ·
  {balance} left to pay". Left card "Plan": Number of installments as a − / + stepper (2–12, a
  number input underneath for keyboard and no-script use), First installment date (a date input
  whose label shows the chosen date as "Oct 26, 2026"), Cadence as the Monthly / Weekly / Yearly
  segmented control, the info line "Collected by: {collection}". The live preview replaces the
  Preview button: a read-only JSON route `ledger:plans:split_preview`
  (`plans/split-preview.json?parent=<kind:pk>&count=&first=&cadence=`, billing, registry
  read-only, every parameter validated, refusals as `{"ok": false, "message"}`) answers the
  lines; a few lines of script render number · date · amount with the rounding cent marked on
  the last row; with scripts off the server renders the same table from the query string as
  today. The editable dates and amounts of today's preview stay (the engine's rules are
  unchanged). Right sticky Summary: Balance to split, "{n} × {share}", last payment date, one
  line on the returned-payment fee rule (the NSF fee sentence from the help), primary "Create
  {n} installments", Cancel. Phone: one column, the Summary above the button. The three-line
  SPLIT_HELP goes to the help topic "Autopay and the daily charges" (or "Payments", whichever
  holds installments today). DESIGN-SPEC.md gains §4 "Split into installments" describing this.
- **D14. USPS (D).** The client already implements the OpenAPI file's `GET /addresses/v3/address`
  with `streetAddress`, `city`, `state`, `ZIPCode` and the `/oauth2/v3/token` client-credentials
  token with scope `addresses`, cached until expiry; Opus adds a conformance test that reads
  `docs/integrations/usps/addresses-v3r2_0.yaml` (the file's actual name; the owner's
  `addresses-v3.yaml` does not exist) and asserts the client's path, parameter names and
  response fields are in it, and fixes any mismatch found. Bicep: `usps` secrets on the web app
  only (the only caller), on the Sentry optional pattern — `uspsClientIdSecretName` and
  `uspsClientSecretSecretName` parameters (empty = not wired), the Key Vault references
  `secrets/usps-client-id` and `secrets/usps-client-secret`, env `USPS_CLIENT_ID` /
  `USPS_CLIENT_SECRET` from `secretRef`, `USPS_API_BASE_URL` = `https://apis.usps.com` (the
  production host, read-only lookups). `infra/parameters/staging.bicepparam` sets both names;
  `infra/README.md`'s egress note is updated; **the expected Key Vault secret names are
  `usps-client-id` and `usps-client-secret` in `finance-sandbox-kv`** (lower-case with hyphens, as
  the other secrets; Key Vault names are case-insensitive, so `USPS-CLIENT-ID` would resolve too,
  but the parameters name the lower-case form). Until the secrets exist the deployment must not
  fail: the parameters ship with the names **commented out** (not wired) and the owner uncomments
  them after storing the secrets — a deployment with a reference to a missing secret fails to
  start the revision. The form keeps saving with "Address not verified". The Bicep change
  deploys only when the owner authorises the infra apply (Section 6); this PR carries the files.

## 4. Interface contract

- `refunds.void_manual_payment(payment_id, *, reason: str, actor, creation_key=None) -> Outcome`.
- `Payment.void_reason: CharField(200, blank)`; migration 0057 (`GUARDED_COLUMNS = {"payments":
  (..., "void_reason")}`, both guard lists).
- `attention` kind `invoice_marked_paid_void` (HIGH).
- Route `ledger:payments:void_manual` (POST billing); JSON route `ledger:plans:split_preview` (GET
  billing, read-only). Digest 272 → 274.
- `BankDebitOptions.reason_more: str`.
- Filter `phone`; helper `accounts.display.staff_name`.
- Help topic `payments`.
- Bicep parameters `uspsClientIdSecretName`, `uspsClientSecretSecretName` on `main.bicep` and the
  web module; env `USPS_API_BASE_URL` on the web app.

## 5. Work breakdown

- **A-1** model, migration, service, attention kind, tests. **A-2** the dialog template, the
  three actions in the frame, the view, route, registry, header actions, DETAILS, the charge
  rows, tests and re-pins. (Opus, one brief.)
- **B-1** a, b, d, h (Add payment and the payment page copy). **B-2** c, g (the filter and the
  staff-name helper across templates). (Opus, one brief, two commits.)
- **C-1** the preview route and the page; **C-2** DESIGN-SPEC §4 section (Fable writes the spec
  text from Opus's built page). (Opus, one brief.)
- **D-1** the conformance test and any client fix; **D-2** Bicep and parameters, README. (Opus,
  one brief.)

## 6. Test plan

- A: void restores the balance and the status (Paid → Partly paid → Unpaid with two payments);
  a voided payment cannot be voided twice; billing can, viewer cannot (403, nothing written);
  the dialog's Keep writes nothing; GET writes nothing; the reason is required and guarded; a
  processor payment is refused on this route; a refund and a deposit refund refused; the
  attention item appears only when a marking succeeded and clears when the invoice refreshes
  open; the installment sentence appears only for a plan created after the payment; the audit
  row; the replay key; the pages tests' pins (one primary, the frame). Adversarial (Fable): two
  voids racing (PostgreSQL); a void while a refund of the same charge's other payment is pending;
  a reason with a card number; hostile return_url; the charge page's figures after a void on a
  multi-line invoice.
- B: the two-line reason in every combination; the badge text; the phone filter's cases (10
  digits, +1, 11 digits, non-US, letters, empty); the Reference help; staff name vs username vs
  e-mail title; the notice style and text; the help topic renders.
- C: the preview route (validation, refusals, 403, 401, nothing written); the stepper bounds;
  the rounding cent on the last row; the server-rendered table with scripts off; the summary
  figures; the phone layout; re-pins.
- D: the conformance test; Bicep `az bicep build` clean; `--what-if` not run (publish gate);
  the parameters file compiles with the names commented out.

## 7. Risks

- A's void changes money figures: the recompute paths are reused, not rewritten; a void never
  touches the processor. The marked-paid invoice cannot be reopened by the app (D3).
- B.c touches many templates; a wrong filter could hide a number — the filter falls back to the
  stored value.
- C replaces a working page; the engine and the confirm POST are unchanged.
- D: a Key Vault reference to a missing secret breaks the revision; hence the commented-out
  names.

## 8. Acceptance

Every test in §6 passes on sqlite and PostgreSQL; the migration round trip holds; no GET writes;
the secret scans are clean; the visual suite passes with only the named baselines regenerated;
the QA file holds each batch's section with laptop and phone shots.

## 9. Questions for the owner (answered at the checkpoint; built as assumed)

1. D3: no reversal of a processor marking exists; the void flags the invoice for a hand unmark
   in the processor's dashboard. Confirm, or name the reversal you meant.
2. D7: "send {operator} the Stax link" is built as written though no link is sent by the app
   and the 2026-09-29 decision keeps operators off processor pages. Confirm the wording.
3. D14: the Key Vault secret names `usps-client-id` / `usps-client-secret`, shipped commented
   out in the parameters until the secrets exist. Confirm.

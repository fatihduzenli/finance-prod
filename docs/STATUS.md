# Status note — 2026-09-29

Written at the end of the Azure and DNS work, before Prompt 4 starts.
Prompts 0 to 3 are complete. Prompt 4 has not been started.

## What is live

| Item | State |
|---|---|
| Staging address | `https://staging.monvera.io` |
| Certificate | Azure managed certificate, valid to 2027-03-29, renewed by Azure |
| Region | Central US for the app, network and database; East US for the registry, identity and staging vault |
| Subscription | Pay-As-You-Go, spending limit off |
| Image | `finance-web` built from commit `02ff92b` |
| Git | local `main` equals `origin/main` at `02ff92b`; this file is new and not committed |
| DNS zone | `monvera.io` delegated to Azure DNS; records: root SPF `v=spf1 -all`, `staging` CNAME, `asuid.staging` TXT |
| CI variable | `APP_BASE_URL` is `https://staging.monvera.io` |
| Static egress IP | `20.12.221.227`, available for Stax whitelisting (TODO-VERIFY-010) |

## Verified from outside on 2026-09-29

| Check | Result |
|---|---|
| `GET /healthz` | 200, `status ok`, `app_env staging`, `stax_env sandbox` |
| `GET /admin/` without a session | 401 |
| Plain HTTP | 301 to HTTPS |
| SANDBOX banner and `[SANDBOX]` title | present |
| `GET /account/login/` | 200 since commit `89d0066` (was 500, defect 1) |
| Default Azure address | 400 by design; the app accepts only `staging.monvera.io` |

## Defects found, not fixed

**Update 2026-09-29, later:** defects 1 and 2 are fixed in code under PLAN-003a, QA verdict
PASS (`docs/qa/QA-003a-staging-defects.md`). **Deployed to staging on 2026-09-29** at commit `89d0066` after the owner's push approval:
login page 200, migrations applied at start, errors with traceback in the container log. Defects 3 and 4 are unchanged.

Each needs a production-code or infrastructure change. Under the root
`CLAUDE.md` that means an Opus assignment, a QA gate and an approved push.

1. **Migrations never run on deploy.** All 27 migrations are unapplied on
   staging, so the login page returns 500. Nobody can sign in until this is
   fixed. The image starts gunicorn only.
2. **No error logging to the container log.** Django has no `LOGGING`
   configuration and no Sentry DSN is set, so a 500 leaves no trace.
3. **The database password rotates on every infrastructure deployment.** The
   template generates a new password each run. The app must be restarted
   afterwards. It was restarted after the hostname deployment. This design is
   not acceptable for production.
4. **The local Bicep compiler crashes** on this machine. The hostname
   deployments ran from a compiled JSON copy of the same template.

## Owner actions outstanding

- Overwrite the **placeholder** in the staging vault under
  `stax-api-key-sandbox` with a real Stax sandbox key. No Stax call may be made
  until then. The key in `finance-kv` is the live key and stays untouched.
- ~~Confirm in writing, per question: Q1, Q2, Q3, Q5, Q6.~~ **Done 2026-09-29**,
  together with Q13, Q21, Q22, Q24, Q26 and Q28. See `docs/SCOPE.md` §9 Round 3.
- Export the sheet as a spreadsheet file with cell notes into
  `docs/inputs/private/`.
- ~~Re-confirm explicitly: Q21, Q22, Q24, Q26, Q28.~~ Done, see above. They are recorded from the
  owner's single reply "yes for the first part" on 2026-09-27, not from a
  per-question answer. Numbers are the session's numbering in
  `docs/COLUMN_MAPPING.md`; `ANSWERS_ALL_25.md` numbers the same questions
  17, 18, 20, 21 and 23.
- Go-live blockers: payment channel for 453 leases, Stax ACH per-transaction
  limit above $12,581 and the daily cap, sheet version history check,
  TODO-VERIFY-019, and the ACH authorisation point in ADR-0005. Operator emails
  are no longer a blocker; they are a receipts gap.
- Checkpoint 3 on staging (own MFA enrolment, viewer destructive attempt, admin
  list) is no longer blocked by defect 1, but **no user exists on staging yet**.
  Creating the first admin is an owner-approved action still to be planned.

## Follow-ups for later prompts

- Redis replacement: Azure Cache for Redis is retired for new instances.
  Decision due at Step 10. `deployRedis` is off.
- Document the ID-based GitHub OIDC subject in `infra/README.md`.
- Pin CI actions to commit SHAs.
- `README.md` status section is stale: it says nothing is deployed.

## Before Prompt 4

The amendments file requires one change ahead of Prompt 4: scheduled invoices
are MVP scope, not backlog. That is recorded in ADR-0004. Prompt 4 must add
`stores`, `operators`, `leases`, `lease_rent_history` and `rent_charges`, plus
the `pending` and `returned` payment states. Since the owner statements of
2026-09-29 it must also add `charge_notes`, `charge_adjustments`,
`insurance_premiums` and `insurance_charges`. The sign convention is confirmed.

**Owner, in writing, 2026-09-29:** "confirmed: defects 1 and 2 are fixed on staging. I opened
the login page myself." The condition for starting Prompt 4 is met.

## Update, 2026-09-29 evening

- **Stax sandbox key:** the owner states a real sandbox key was stored in the staging vault.
  Metadata shows a second version of `stax-api-key-sandbox` created 2026-09-29 19:00 UTC.
  The value was not read. The app picks it up with the next revision.
- **Refused-host logging:** fixed under PLAN-003b, QA verdict PASS
  (`docs/qa/QA-003b-refused-host-logging.md`). Not on staging until the next push.
- **First staging admin:** proposal in `docs/qa/PLAN-003c-first-staging-admin.md`, awaiting
  owner approval.

## Update, 2026-09-29 night

- **Deployed at commit `2d5c1e6`:** new revision healthy, health endpoint reports staging and
  sandbox, login 200. A request to the default Azure hostname now writes one WARNING line.
  The revision started after the new Stax sandbox key version was stored, so it holds the
  current version. No code calls Stax yet; the key itself is first exercised in Prompt 5.
- **First admin:** plan 003c approved by the owner, implemented, QA verdict PASS
  (`docs/qa/QA-003c-first-staging-admin.md`). Needs a push, then the owner runs the command
  in `infra/README.md` §12.

## Update, 2026-09-29, owner confirmation

- **Deployed at commit `411e412`**, which contains `bootstrap_admin`. Revision healthy.
- **Owner, in writing:** "First staging admin created and MFA enrolled. Checkpoint 3 on
  staging: I signed in with password + authenticator myself. Confirmed."
- Checkpoint 3 items on staging:

| Item | State |
|---|---|
| Owner's own MFA enrolment and sign-in | Confirmed by the owner |
| A viewer attempts a destructive action and is refused | Confirmed by the owner in writing, 2026-09-29 ("both confirmed") |
| Owner has reviewed the list of admin users | Confirmed by the owner in writing, 2026-09-29 ("both confirmed") |

- `bootstrap_admin` now refuses on staging with exit status 2, because an admin exists.

- **Observed:** the "First admin created" warning is not in the container log. A command run
  through an exec session writes to that session, not to the container's own output. The
  creation is therefore evidenced by the owner's statement and the user row, not by a log
  line. A durable audit record for such events belongs with the audit log in a later prompt.
- **Checkpoint 3 on staging is complete**: all three items confirmed by the owner.

## Update, 2026-09-29, Prompt 4

- PLAN-004 implemented and QA verdict PASS (`docs/qa/QA-004-schema.md`), **not pushed**.
  The verdict is conditional on the owner accepting decisions D2 to D12 in
  `docs/qa/PLAN-004-schema.md` §3.
- `docs/DATA_OWNERSHIP.md` is written for the owner's Checkpoint 4 review.
- **Owner, in writing, 2026-09-29:** "accept D2 and D12". D2 (`customers` built as
  `operators`) and D12 (deposit and webhook tables keep their provider ID columns) are
  accepted. D3 to D11 are open. No push approval has been given for PLAN-004.

## Update, 2026-09-30, Prompt 4 on staging

- **Owner typed "push" on 2026-09-29.** `main` pushed to commit `5e66026`. The first deploy
  attempt failed while pulling the Python base image (a registry 502, outside our code); the
  failed job was re-run for the same commit and succeeded.
- Revision `finance-staging-web--0000006` is healthy on that image. Health endpoint 200,
  login 200, admin without a session 401. Gunicorn starts only after `deploy_migrate` exits 0,
  so the 23 new tables and the PostgreSQL triggers are applied on staging. All tables are
  empty; no demo data is loaded there.
- **D3 to D11 remain proposals** awaiting the owner's word. The schema is reversible while
  the tables are empty (`migrate ledger zero`, `migrate payments zero`).
- Checkpoint 4 items for the owner: read `docs/DATA_OWNERSHIP.md`; decide the invoice number
  format; confirm the two behaviours in its §6; test the card guard by hand.

## Update, 2026-09-30, Checkpoint 4 complete

- **Owner, in writing:** Checkpoint 4 complete. `docs/DATA_OWNERSHIP.md` read line by line.
  Invoice number format `INV-YYYY-NNNN` with old numbers carried forward; collection stops
  on a Stax total mismatch until staff resolve it; an unknown Stax-method payment is flagged,
  never deleted. Card guard: **corrected**, no browser test was possible because the new tables are not in
  the admin panel yet; Fable ran both attempts inside the staging container at the owner's
  request, both refused, messages recorded in `docs/DATA_OWNERSHIP.md` §6. Admin panel
  exposure of the new tables stays in Prompts 6 and 7.
- **Owner notes for the record:** receipts go to the email stored in Stax, so an email
  mismatch flag tells staff to fix it in Stax; a database-level guard for bank routing and
  account patterns is added to the **Prompt 14** security scope.
- Open from this: where the old invoice numbers live (the Rents tab has none); needed before
  Prompt 13. D3 to D11 still await the owner's word.

## Update, 2026-09-30, Checkpoint 4 closed

- **Owner, in writing:** "guard test confirmed, accept D2 TO D12 and push". Checkpoint 4 is
  closed. PLAN-004 decisions D2 to D12 are all accepted; the schema on staging is final for
  Prompt 5 to build on.
- Still open before Prompt 13: where the old invoice numbers live.

## Update, 2026-09-30, Prompt 5

- PLAN-005 implemented, QA verdict PASS (`docs/qa/QA-005-stax-client.md`), **not pushed**.
  No call to Stax has been made. Owner steps after the push: run `provider_ping` on staging,
  create the Sentry project and alert rule, send the whitelisting questions in
  `docs/STAX_CLIENT.md` §9 to Stax.

## Update, 2026-09-30, Prompt 5 on staging and the sandbox key verified

- **Owner typed "push"**; `main` at `9b68d63` deployed, revision healthy, login 200.
- **Owner instructed "run ping".** `provider_ping` on staging: `GET /self` returned 200 in one
  attempt; provider `stax`, environment `sandbox`, merchant `ea82…58f4`, expected merchant not
  set. No retry was needed.
- **Written evidence that the key belongs to the sandbox account** (read-only probe of the
  same response; the key was never printed):

| Field in `GET /self` | Value |
|---|---|
| `merchant.company_name` | `Global Retail Group` |
| `merchant.is_sandbox` | `True` |
| `merchant.status` | `ACTIVE` |
| `merchant.is_payfac` | `True` |
| `user.is_api_key` | `True` |
| `user.team_role` | `admin` |

  The manager's statement: the sandbox account is "Global retail group" and the live company
  account is "Global Retail Group LLP". The response names the former and carries
  `is_sandbox = True`, so the key in the staging vault belongs to the sandbox.
- Recommended owner action: set `STAX_EXPECTED_MERCHANT_ID` on staging to the merchant ID
  shown in full in the Stax dashboard, so every future ping also checks identity.
- TODO-VERIFY-005 and 024 closed by this observation.


## Update, 2026-09-30, Prompt 6

- PLAN-006 implemented, QA verdict PASS (`docs/qa/QA-006-operators.md`), **not pushed**.
- **Observed on the sandbox account (`GET /self`, 2026-09-30): `merchant.allow_ach = False`.**
  The sandbox account may not be able to run ACH debits at all. Not a Prompt 6 blocker
  (customers only), but Prompt 7's monthly run and the one-off-invoice test (TODO-VERIFY-019)
  cannot be exercised in the sandbox until Stax enables ACH there. Owner to raise with Stax.
- Interim: the staging web app becomes the single Stax caller (one replica, one process)
  until Step 10; set in the template and the image.
- Owner requests recorded: Phase 7 items (a) document upload, (b) templated outbound email,
  (c) DocuSign API; tables `documents` and `outbound_messages` reserved.

## Update, 2026-09-30, Prompt 6 on staging

- Owner typed "push" and "deploy". `main` at `1b5093a` deployed; then the staging template
  deployed (`staging-plan006-caller`, succeeded, no failed operations) and the revision was
  restarted for the rotated database secret (defect 3, unchanged).
- Revision `finance-staging-web--0000011`: `STAX_CALLER=worker`, `maxReplicas=1`,
  `STAX_EXPECTED_MERCHANT_ID` set to the sandbox merchant. Start-up log: migrations up to
  date, `GET /self` 200, **provider environment check: ok**, one gthread worker.
- Health 200, login 200, `/operators/` without a session 401.
- **Checkpoint 6 is now the owner's:** create an operator with an email, check the sandbox
  dashboard for exactly one customer, double-click save, try a malformed email. Fable reads
  the transport log afterwards.

## Update, 2026-10-01, Checkpoint 6 and PLAN-006a

- **Checkpoint 6, owner in writing:** test 2 (double click: one record, linked) and test 3
  (malformed email refused, no record) passed. Fable confirmed from the transport log that no
  request left for the malformed email, and by a read-only API lookup that each of the three
  created operators has exactly one Stax customer, with matching IDs. Test 1's first attempt
  returned 422 from `POST /customer` (email at the reserved `example.com` domain; hypothesis,
  body not logged); five further attempts within a minute opened the breaker at 20:34:01 UTC,
  which closed itself at 20:39:01. Dashboard duplicate check deferred by the owner to
  2026-10-01 (no sandbox dashboard access yet).
- **No-email rule, owner in writing:** "confirm. Allowed, complete record, not enrolled until
  staff link the IDs."
- **PLAN-006a** (owner's option A) implemented, QA PASS (`docs/qa/QA-006a-sync-diagnostics.md`),
  **not pushed**: 4xx field names in the log, 60-second retry lock, post-redirect-get on every
  operator POST, created ID in the create log line.
- Owner will edit the failed operator's email to a non-reserved domain and retry once.

## Update, 2026-10-01, cause of the 422 found by the owner

- **Owner, in writing:** the failed operator is now linked. Cause: an **incomplete phone
  number**. A non-reserved email alone still gave 422; adding the company name alone still
  gave 422; completing the phone number succeeded first time. The `example.com` hypothesis was
  wrong; the six failures that opened the breaker at 20:34 UTC most likely had the same cause.
  Three more failed requests were spent on the retries. Checkpoint 6 items 2 and 3 confirmed;
  item 1 (dashboard) is for 2026-10-01.
- The specification (`/reference/create-customer.md`, read 2026-10-01) states the phone rule
  `/[0-9]{10,15}/`, 255-character caps on names and email, and the error shape
  `{"<field>": ["<message>"]}`.
- **PLAN-006b** implemented, QA PASS (`docs/qa/QA-006b-customer-field-rules.md`), **not pushed**:
  the phone rule and the caps are enforced in the form and again before any request leaves;
  empty optional fields are no longer sent; stored phones normalised by migration 0006, bad
  ones flagged for review.


## Update, 2026-10-01, PLAN-006a and 006b on staging

- Owner typed "push". `main` at `518dfb0` deployed; pipeline green. Revision
  `finance-staging-web--0000012` healthy: migrations 0005 and 0006 applied at start,
  `GET /self` ok, environment check ok, one gthread worker. Health 200, login 200.
- Checkpoint 6 remaining: item 1, the sandbox dashboard duplicate check, when the owner has
  dashboard access.

## Update, 2026-10-01, Prompt 7

- PLAN-007 (7a lease setup and autopay enrolment, 7b monthly rent run, insurance charge
  generation) implemented, QA PASS (`docs/qa/QA-007-leases-and-rent-run.md`), **not pushed**.
- Spec facts settled on 2026-10-01: hosted-page `url` value; `meta.memo` / `meta.reference` /
  `meta.internalMemo` on invoices; payment-method list fields; schedule pause and resume.
  New open items TODO-VERIFY-035 to 038.
- One test failed once in twelve full runs and was not identified; carried as an open MINOR.
- **Sandbox ACH is still reported disabled.** The first real enrolment on staging is expected
  to stop at the schedule create with field names until Stax enables ACH. What to ask Stax:
  enable ACH (bank payment methods and ACH scheduled invoices) on sandbox merchant
  `ea824f2b-90b3-4c49-8f69-ba06943458f4` ("Global Retail Group"), and confirm the ACH
  per-transaction limit on the live account exceeds $12,581 (Q25).


## Update, 2026-10-01, Prompt 7 on staging

- Owner typed "push". `main` at `f96887a` deployed; pipeline green (the unidentified flake
  did not recur in CI). Revision `finance-staging-web--0000013` healthy: migration 0007
  applied, environment check ok, one gthread worker. Health 200, login 200, `/leases/` and
  `/run/` refuse anonymous requests with 401.
- Checkpoint 7 on staging waits on Stax enabling ACH in the sandbox. Steps once it is on:
  link a bank method in the Stax dashboard, enable autopay on one lease in the app, confirm
  one schedule, run "Match invoices" after the first debit day.

## Update, 2026-10-01, owner's three questions and the "flake" found

- **Re-enable after a rent change** now deletes the old Stax schedule (DELETE, 404 = gone)
  before any create; any other refusal creates nothing. Tested. **Enrolment page** shows the
  operator's receipts email; live operators need a real address before enrolment
  (`docs/STAX_CLIENT.md` §5b). **DTSTART at 12:00 UTC:** the spec is silent on run time; ACH
  batches at 8 pm Eastern; left as is.
- The one failure in twelve runs was **not a flake**: the routing-number check did not strip
  UUIDs, so about one in 6,400 writes carrying a UUID was refused. Fixed in
  `ledger/validators.py` with regression tests; closed in QA-007.


## Update, 2026-10-01, store page (PLAN-007b)

- Owner: nothing in Prompt 7 could be exercised on staging because no store existed and no
  store page did. PLAN-007b adds the store page (list, create, edit; open date required on the
  form), registers Store, Operator and Lease read-only in the admin with view permission for
  the admin group, and confirms the second debit day renders empty. QA PASS
  (`docs/qa/QA-007b-stores.md`), **not pushed**.

## Update, 2026-10-01, store page on staging

- Owner typed "push". `main` at `a9e40d4` deployed; revision `finance-staging-web--0000015`
  healthy; migration 0008 applied; environment check ok. `/stores/`, `/leases/`, `/run/`
  refuse anonymous requests with 401; login 200.
- Owner's staging walk-through can start: store, lease, rent change, run page, then "Enable
  autopay" expecting the ACH refusal with field names.

## Update, 2026-10-01, Prompt 7 walk-through and PLAN-007c

- **Owner's staging walk-through (in writing):** store created; lease created (day 1, ACH,
  $2,000); "Second debit day" was empty on a fresh form, the earlier "1" was a browser hint;
  rent changed to $2,100 with history shown; run page listed the lease under day 1 with the
  "Large debit" tag and the 2026 insurance charge with "Premium missing"; "Create today's
  charges" twice: 1 created, then 1 already created; "Match invoices": 0 schedules; "Enable
  autopay" refused by the large-debit hard block at $2,100; at $1,500 the page asked for a
  bank method; **the Stax sandbox dashboard cannot add a bank account: "Could not tokenize
  payment method: no tokenization gateway found".** Enrolment stays blocked on Stax; the
  request to Stax now includes this and the merchant ID.
- **PLAN-007c** (owner change request): the warning threshold no longer blocks; an admin
  confirms explicitly and the confirmation is audited; `STAX_MAX_TXN_AMOUNT` (new, per
  environment, empty until Stax answers Q25) is the confirmed limit above which enabling is
  hard-blocked. QA PASS (`docs/qa/QA-007c-large-debit-confirmation.md`), **not pushed**.


## Update, 2026-10-01, PLAN-007c on staging

- Owner typed "push". `main` at `04a51a1` deployed; revision healthy; environment check ok.
  The large-debit confirmation is live; `STAX_MAX_TXN_AMOUNT` is unset (unknown) on staging.
- Prompt 8 can be built and tested on the sandbox without ACH (customer, invoice and schedule
  webhook events); transaction, settlement and deposit events wait on Stax enabling ACH.

## Update, 2026-10-01, Checkpoint 6 closed

- **Owner, in writing:** "Checkpoint 6 closed: Stax sandbox dashboard shows exactly four active
  customers, one per operator, emails matching ours. No duplicates." This completes item 1
  (dashboard duplicate check); items 2 and 3 were confirmed on 2026-09-30, and the no-email
  rule was confirmed in writing. Checkpoint 6 is complete.

## Update, 2026-10-01, Prompt 8

- PLAN-008 (webhook receiver, processor, state machine, returns queue, registration) implemented,
  QA PASS (`docs/qa/QA-008-webhooks.md`), **not pushed**. One MAJOR (token in Sentry breadcrumbs)
  found by Opus and fixed before the gate. `docs/RUNBOOK.md` written.
- After the push: `register_webhooks` runs inside the staging container with the owner's word;
  then the owner's four clicks (operator edit → `update_customer` event; dashboard invoice →
  invoice events; replay; wrong token 404).


## Update, 2026-10-02, Prompt 8 on staging

- Owner typed "push". `main` at `46fcf80` deployed; revision `finance-staging-web--0000017`
  healthy; migrations `payments.0003` and `ledger.0009` applied; environment check ok.
- Receiver probed from outside: POST with a wrong token → 404, empty body; GET → 404; the
  path with no token → 404. The log shows the refusal lines with `[token]` in place of the
  path segment and no token value. Nothing stored (no row can exist before registration).
- Waiting for the owner's "register" before `register_webhooks` runs in the container.

## Update, 2026-10-02, webhooks registered on staging

- Owner typed "register". `register_webhooks` ran inside revision `finance-staging-web--0000017`:
  `GET /webhook` then fifteen `POST /webhook`, all 200, paced by the limiter. Registrations: 15,
  created: 15; IDs stored in `external_refs` (`webhook_registration`). Event list as in
  `docs/RUNBOOK.md` §2. The owner's four click paths are next.

## Update, 2026-10-02, Sentry wired on staging

- Owner created the Sentry project `finance-staging` (alert: level fatal emails the owner) and
  stored `sentry-dsn` in `finance-sandbox-kv`. Owner typed "push" and "deploy". Template
  deployment `staging-sentry-dsn` succeeded; the live revision `finance-staging-web--0000019`
  has `SENTRY_DSN` as a vault reference. The breaker and dead-letter alerts now reach Sentry.
- A one-off test event (level fatal) from inside the container awaits the owner's word.
- **Owner, in writing (2026-10-02):** the test event's alert email arrived; the Sentry event has
  no Request section, only tags, runtime and `sys.argv`, and no token or key anywhere.
  **Sentry is proven on staging.** Breaker and dead-letter alerts now page the owner.

## Update, 2026-10-02, Prompt 9

- PLAN-009 (payments ledger, Stax marking of manual payments, carried balance, refunds and voids)
  implemented, QA PASS (`docs/qa/QA-009-payments-ledger.md`), **not pushed**.
- Owner decision needed: a **void** marks the original payment voided rather than adding a row
  (amendment 4 said new rows for refunds and voids; a void is not a new transaction at Stax).
- TODO-VERIFY-016 closed; 028 (`apply_balance` unit) keeps partial payments local-only.

- **Owner, in writing (2026-10-02): "voids as built."** A void marks the original payment voided;
  refunds are new rows linked to their parent. Amendment 4 is read that way from here on.

## Update, 2026-10-02, Prompt 9 on staging

- Owner typed "push". `main` at `0725f8f`. The pipeline's deploy step reported an Azure
  InternalServerError from `az containerapp update`, yet Azure created and activated revision
  `finance-staging-web--0000021` on the new image: healthy, 100 % traffic, migration 0010 applied,
  environment check ok. The CI job is marked failed although the deployment took effect; no
  re-run was needed. Health 200, login 200, `/payments/` without a session 401.

## Carried into the next plan (owner request, 2026-10-02)

- **CI verification step after `az containerapp update`:** poll until the active revision's
  image tag equals the commit SHA and its health is Healthy and Running; pass the job on that
  evidence even when the update command itself returned a transient Azure error; fail the job
  when the revision is not live within the wait. Not urgent; goes into the next plan that
  touches `.github/workflows/ci.yml`.

## Update, 2026-10-02, Checkpoint 8 click 1 and PLAN-006c

- **Owner, in writing:** editing operator fatih's phone did not reach Stax; the Webhooks page
  stayed empty. Fable's checks: (a) the edit path made no provider call (a Prompt 6 deviation
  Fable had accepted in QA-006, wrongly); (b) no Stax call in the transport log at 16:54 UTC;
  (c) the operator showed `linked`, no failure, phone changed locally only.
- **PLAN-006c** fixes it: an edit of a linked operator updates the Stax customer through the
  same field rules as create, shows the outcome, and offers the single manual retry on failure.
  One MAJOR (email validated as required in the adapter) found and fixed. QA PASS
  (`docs/qa/QA-006c-operator-update-sync.md`), **not pushed**.


## Update, 2026-10-02, PLAN-006c on staging

- Owner typed "push". `main` at `ab8d075` deployed; the live revision runs that image tag,
  healthy; environment check ok. Click 1 of Checkpoint 8 can be repeated.

## Update, 2026-10-02, PLAN-008a (owner findings from the clicks)

- `send_invoice` events were `ignored: no object id`: the body is an email envelope with the
  invoice under `mergeVars.invoice`. The receiver now reads nested IDs and records the source;
  the handler re-fetches and derives `sent_at` from the event time (the invoice object has no
  timestamp keys). A dashboard invoice can now be linked to a charge: automatically on
  `create_invoice` when exactly one open charge of the operator matches total and period,
  otherwise by hand from the charge page ("Link invoice", billing or admin, confirmation and
  note when the total differs). QA PASS (`docs/qa/QA-008a-send-invoice-and-manual-link.md`),
  **not pushed**. TODO-VERIFY-015 and 042 (invoices) closed from the stored bodies.

- **Owner, in writing (2026-10-02): Checkpoint 8 click 1 passed** on revision 0000022: phone
  edit saved, "Provider customer updated" shown, new phone in the Stax dashboard, an
  `update_customer` row on the Webhooks page with the event name from the header, and the
  email still present in Stax after the update (the update carried the email; TODO-VERIFY-056,
  about omitting it, stays open).

## Update, 2026-10-02, PLAN-008a on staging

- Owner typed "push". `main` at `8fc2521` deployed; the live revision runs that image tag,
  healthy, 100 % traffic; migrations `payments.0004` and `ledger.0011` applied; environment
  check ok.

## Update, 2026-10-02, owner clicks on 0000023 and PLAN-009a

- **Owner, in writing:** Link invoice worked without confirmation. The replayed `send_invoice`
  still read "ignored: no object id". The wire payment was recorded locally but the Stax mark
  failed: `ProviderRejected (fields: email_receipt)`. The owner then marked that invoice paid by
  hand in the Stax dashboard and will run the memo test on a fresh charge.
- **PLAN-009a:** booleans in query strings are now `0`/`1` (Stax rejected `false`); a replay
  re-reads the object ID from the stored body. QA PASS (`docs/qa/QA-009a-query-booleans-and-replay.md`),
  **not pushed**. After the deploy: Retry on the first charge should find the invoice already
  paid in Stax and send nothing (mismatch recorded); the memo test runs on a fresh charge.


## Update, 2026-10-02, PLAN-009a on staging

- Owner typed "push". `main` at `938608b` deployed; the live revision runs that
  image tag, healthy; environment check ok. Owner's next clicks: Retry on the hand-marked charge
  (expect "already paid" with no call), the memo test on a fresh charge, replay of the old
  `send_invoice` event.

## Update, 2026-10-02, Checkpoints 8 and 9 closed except the ACH-blocked items

- **Owner, in writing:** on revision 0000024 (1) Retry on the hand-marked charge: "Provider
  invoice already showed paid before marking: not marked again. Check for a double payment."
  Nothing sent. (2) Fresh charge for operator halid: the dashboard invoice auto-linked,
  `create_invoice` and `send_invoice` processed, wire payment recorded, Stax shows the invoice
  paid with memo "Paid by wire on 10/02/2026. test wire two"; the resulting `create_transaction`
  event was ignored as not ACH (a cash-marked payment), which is right. (3) Replay of the old
  `send_invoice` event: processed, object ID from `mergeVars.invoice.id`.
- TODO-VERIFY-058 answered in practice: `email_receipt=0` accepted. TODO-VERIFY-055 answered:
  the paid status string is `PAID`.
- **Checkpoints 8 and 9 closed** except the items blocked on Stax ACH: transaction, settlement,
  return, refund, void and deposit events; refunds and voids of real debits; the
  "pay half, refund the half, pay in full" matrix row.

## Update, 2026-10-02, PLAN-010 (worker, database queue, reconciliation) — QA PASS, not pushed

- **Built:** the `jobs` table and `worker` command (the single Stax caller, container app
  `finance-staging-worker`, one replica, `STAX_CALLER=worker`); every screen action that
  reached Stax now queues a job and shows its state; the web app returns to `STAX_CALLER=none`
  and two replicas; the nightly reconciliation (`reconciliation_runs`, `reconciliation_mismatches`,
  `/reconciliation/` with Run now and Resolve); the scheduled Container Apps Job
  `finance-staging-reconcile` (cron 06:00/07:00 UTC, local gate 02:00 New York) **queues** the run
  for the worker; `age_pending_payment` fixture command; six D12 settings; CI updates the worker
  and the job image and verifies all three are live on the commit. ADR-0006: no Celery, no Redis.
- QA report `docs/qa/QA-010-worker-and-reconciliation.md`: PASS; 2,769 tests on PostgreSQL;
  one MAJOR (job as second caller) resolved by amending D5; one MINOR deferred in writing
  (invisible-character notes on the older mandatory notes).
- **Rollout order (two owner words):** first "push" (CI builds the image, rolls the web app,
  applies migrations 0005–0007; the worker and job updates fail harmlessly because the apps do
  not exist yet, so that one CI run ends red at the verification step), then "deploy" (Bicep with
  `webImage` = the new image: creates the worker app and the scheduled job, sets the web app to
  `none` / 2 replicas). Between the two, staff actions queue and wait; nothing is lost.
- Owner's done-when after the deploy: an operator edit reaches Stax within a minute; Run now
  shows counts; `age_pending_payment` + Run now sends the Sentry fatal email; the scheduled
  run row appears the next morning (two overnight runs for Checkpoint 10).

## Update, 2026-10-03, PLAN-010 on staging

- Owner typed "push": `main` at `3030829`; the web revision `finance-staging-web--0000026`
  went live on that image with migrations 0005–0007 applied; that CI run ended red at the new
  verification step because the worker app did not exist yet (expected, documented above).
- Owner typed "deploy": template deployment `staging-plan-010` succeeded (3 min 12 s, no failed
  operations). Now live: web `finance-staging-web--0000027` (`STAX_CALLER=none`, max 2
  replicas, healthz 200); worker `finance-staging-worker` (one replica, `STAX_CALLER=worker`,
  grace 60 s, environment check ok, "Worker started" 01:22 UTC, image tag `3030829`);
  scheduled job `finance-staging-reconcile` (cron `0 6,7 * * *`, `STAX_CALLER=none`,
  `reconcile --scheduled`, timeout 300, no retries). No job had queued between the two steps.
- Next: the owner's done-when clicks (operator edit through the worker; Run now;
  `age_pending_payment` then Run now for the Sentry email); the first scheduled run is expected
  at 06:00 UTC on 2026-10-03 (02:00 New York); Checkpoint 10 wants two overnight runs. The CI
  verification goes green on the next push, when all three resources exist.

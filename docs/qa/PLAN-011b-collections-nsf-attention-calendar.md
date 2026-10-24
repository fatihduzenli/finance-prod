# PLAN-011b — Collections ladder by Gmail, NSF re-debit, attention list, contact log, calendar and ICS, New York time, grid usability

**Date:** 2026-10-06 · **Author:** Fable (QA owner) · **Implementer:** Opus
**Origin:** owner's Prompt 11b text and items 7–8 of 2026-10-06. One infrastructure deployment
(vault references and a daily scheduled job); expect the owner's "deploy" after the push.

## 1. Restated objective

Collections become a process the app runs: overdue rent and one-off charges climb a ladder of
e-mails (day 3 friendly, day 10 formal, day 20 final by default) sent from the company mailbox
through the Gmail API with a service account and domain-wide delegation, from admin-editable
templates with merge fields, with a per-operator opt-out and a log of every message and its
delivery status; on the sandbox every message goes to a staff catch-all. A returned ACH debit is
handled by staff: the returns queue gains **Reschedule**, which asks for the agreed re-debit date
and creates one Stax one-off invoice for the original amount plus an NSF fee (100.00 on the
operator's first return, 50.00 afterwards, admin-editable, waivable with a note), the fee as its
own charge line, never automatically on the webhook. Until rescheduled, a returned debit sits on
the attention list and the operator's page. Staff keep a dated contact log per operator (calls,
e-mails, visits, promises to pay); broken promises surface. The dashboard gains an attention list
of twelve kinds of item, each linking to its page. A month calendar holds dated notes and
reminders (optional morning e-mail to the staff user) plus generated entries for debit days,
re-debit dates, insurance anniversaries and lease ends, and a revocable per-user ICS feed puts
the same items in Google Calendar. The project time zone becomes America/New_York with storage
in UTC; nothing migrates silently: the staging rows whose date would change are listed for the
owner before anything is applied. The grid gets a visible scrollbar, a fixed store column and
twelve months on a laptop. *(The twelve-months rule was retired by the owner on 2026-10-22, PLAN-012f Batch A: the grid scrolls at 1,280 px with 116 px months.)*

## 2. Affected packages

`backend/ledger` (models, migrations, services, views, templates, CSS), new `backend/mail/` (the
only code that talks to Gmail), `backend/payments` (returns path, one-off invoices with two line
items, job kinds, a daily tick command), `backend/finance` (settings, env guard, time zone),
`backend/accounts/registry.py`, `backend/tests`, `infra/` (vault references, a daily Container Apps
job), `.env.example`, `docs/`. New dependency, approved: `google-auth` (Apache-2.0), pinned; HTTP
through the already pinned `httpx`. **Out of scope:** SMS, DocuSign, installments (12b), any
inbound mail, Google Sheet push.

## 3. Facts checked on 2026-10-06 (public DNS, read-only)

- `globalretailgroups.com` SPF: `v=spf1 include:dc-aa8e722993._spfm.globalretailgroups.com ~all`,
  and that include resolves to `v=spf1 include:_spf.google.com ~all`. **Google's senders are
  covered**: mail sent by the Gmail API as the mailbox passes SPF with alignment.
- MX: Google (`aspmx.l.google.com` and alternates).
- DMARC: `v=DMARC1; p=quarantine; adkim=r; aspf=r; rua=mailto:dmarc_rua@onsecureserver.net` —
  quarantine policy, relaxed alignment; DMARC passes on aligned SPF alone.
- **DKIM: no `google._domainkey` record exists** (no other common selector either). Google then
  signs with its generic domain, which does not align. Nothing *must* be added for delivery to
  pass (SPF aligns), but the Workspace DKIM key **should** be published so that mail still passes
  DMARC when forwarded or when SPF breaks; the admin steps are in §9.

## 4. Decisions

| # | Decision | Reason |
|---|---|---|
| D1 | **Time zone** (owner item 7): `TIME_ZONE = "America/New_York"`, `USE_TZ = True` (storage UTC). Every `localdate()`-derived value (charge periods, debit days, "today" on the run page, the nightly job's date, report month boundaries, `initiated_on`, `settled_on` from provider timestamps) follows New York. `RECONCILE_TIMEZONE` stays a setting but defaults to the project zone. **Audit before apply:** a read-only command `tz_audit` lists, per table and column, the rows whose stored date differs from the date their source timestamp gives in New York: `payments.initiated_on`/`settled_on` from `provider_created_at` / the stored settlement timestamp in `raw_summary`, `invoices.issue_date` from `created_at`, `one_off_charges.due_date` and `rent_charges.due_on` are staff- or rule-set and are **not** candidates (listed as "unchanged by design"); it prints masked IDs with the old and new date and counts. `tz_apply` rewrites exactly the listed rows with an audit row each and refuses to run unless `--audited <count>` matches the current audit. Fable runs `tz_audit` on staging and shows the owner the list; `tz_apply` runs only on the owner's word. The time-zone switch itself ships in this plan's code; until `tz_apply` runs, the few rows are as listed | Owner item 7; before Prompt 13 |
| D2 | **Grid usability** (owner item 8): the grid wrapper shows a permanent horizontal scrollbar (`scrollbar-gutter: stable; overflow-x: scroll` with visible styling on WebKit), a second scrollbar mirrored above the table for long grids, the store column fixed (`position: sticky; left: 0`) with the header row fixed too, column widths 88 px per month and 220 px for the store column so 12 months fit at 1,280 px; the phone keeps the sideways scroll with the fixed column; a Playwright check at 1,280 and 390 px | Owner note |
| D3 | **Mail transport** `backend/mail/`: `MailSender` protocol (`send(Message) -> SentReceipt`), `GmailApiSender` (service-account JWT with `google-auth`, subject = the mailbox, scope `https://www.googleapis.com/auth/gmail.send`, token cached until expiry, `POST users/me/messages/send` with a base64url MIME message through the pinned `httpx`; timeouts 10 s connect / 30 s read; no retry on 4xx; one retry on 5xx/network after 5 s; errors as class names), `MemoryMailSender` (tests, local), `NullSender` when mail is off. Settings: `MAIL_ENABLED` (off by default), `GMAIL_SENDER` (the mailbox address; not a secret), `GMAIL_SERVICE_ACCOUNT_JSON` (**secret**, from Key Vault `gmail-service-account-key` by container-app secret reference; parsed at startup; never logged; the env guard checks it is JSON with `client_email`, `private_key` and `token_uri` keys without printing any value), `MAIL_CATCH_ALL` (an address; when set **every** message is delivered to it with the real recipient in the subject prefix `[to: x@y]` and header `X-Original-To`; **required when `APP_ENV != production`**, so a sandbox can never mail a real operator). Nothing outside `backend/mail/` imports `google.*` (test-fenced); the key never appears in `raw_summary`, audit rows or logs (test) | Owner item 1 |
| D4 | **Ladder.** Tables `collection_steps` (admin: `day_offset`, `template`, `active`, `position`; seeded 3 friendly, 10 formal, 20 final), `message_templates` (admin: `code`, `subject`, `body`; merge fields `{operator}`, `{store}`, `{amount}`, `{due_date}`, `{days_late}`, `{statement_link}`, `{company}`; rendered with a **whitelisted** substitution, never Django templates, every value escaped for text/plain; HTML part generated from the text), `operators.handled_personally` + `handled_personally_note` (billing), and `outbound_messages` reshaped from the reserved table: `operator`, `template`, `step`, `charge` (a rent or one-off charge, generic as audit rows), `to_address`, `subject`, `body_sha256`, `status` in {`queued`, `sent`, `failed`, `suppressed`, `catch_all`}, `provider_message_id`, `error` (class name), `queued_at`, `sent_at`, `sent_by` (null = the ladder). **Dedupe**: one row per (charge, step). The ladder runs in the daily tick (D8): for each overdue rent or non-refundable one-off charge with `balance > 0`, days late ≥ step offset, step not yet sent, operator with an e-mail and not handled personally, a `message_send` job is queued; the worker renders and sends. Staff can also send a step by hand from the charge page (billing). Every send, suppression and failure is in the log and the audit | Owner item 1 |
| D5 | **NSF and Reschedule** (owner item 2): the returns queue's resolution gains **Reschedule** (billing; admin for the fee waiver): re-debit date (New York, today or later), fee waived? (admin, note mandatory), note. It creates **one** Stax one-off invoice, through the existing `autopay_due` path (a one-occurrence schedule on that date from the linked bank method; `email` if the operator has no linked method, so staff can still send it), whose `Amounts` carries **two line items**: the original amount and the NSF fee. Locally two one-off charges: category `redebit` (price = returned amount; `source_payment` FK = the returned payment) and category `nsf_fee` (price from `fee_types` `nsf_fee_first` 100.00 or `nsf_fee_later` 50.00, admin-editable, seeded) with `companion_of` FK = the re-debit charge; both share the schedule/invoice (the invoice's `one_off_charge` is the re-debit charge; the fee charge links through `companion_of`); a payment on that invoice allocates to the re-debit charge first, then the fee. `operators.nsf_return_count` increments on Reschedule only (never on the webhook); a waived fee writes the fee charge as `waived` with the note so the count still increments. The return is marked resolved with resolution kind `rescheduled` and `redebit_date`. The same action applies to any returned debit (rent, insurance, one-off, later installments). Reports: `nsf_fee` counts as fees; `redebit` counts as what it re-collects (rent or fees by the source payment's kind) | Owner item 2 |
| D6 | **Attention list** (owner items 3, 5): `ledger/services/attention.py` returns items of kinds: overdue charge (by days late, worst first), returned debit not rescheduled (`"Returned debit not rescheduled: <store>, <amount>, returned <date>"`), promise to pay past due, insurance due in 30 days or premium missing, lease ending in 60 days, landlord lease ending in 90 days, store with no open date, operator with no e-mail, vacant store over 30 days (no active lease since the last ended), schedule flagged after a rent change, dead-letter webhooks, unresolved reconciliation mismatches, and calendar items of today; each item links to its page; the dashboard shows the list; the operator page shows the operator's own items as banners. Bounded queries | Owner items 3, 5 |
| D7 | **Contact log** (owner item 4): table `contact_entries` (operator, `kind` in {call, email, visit, promise}, `happened_on`, `promised_date`, `promised_amount` (promise only; CHECK), body (guarded, required with `visible_text`), author, created_at); billing adds; the operator page lists them newest first with CSV; a promise past its date with the charge still open is an attention item until a payment settles or a later entry closes it (`closed_by` FK) | Owner item 4 |
| D8 | **Calendar** (owner item 6): table `calendar_entries` (kind note/reminder, optional store/operator/lease, `on` date, title, body, `email_morning`, author, created_at); page `/calendar/` month view with previous/next, filters by kind and record, entries clickable; generated entries (not stored) for debit days (each active lease's next debits in the shown month), re-debit dates (D5), insurance anniversaries, lease ends; the attention list shows today's entries. **ICS feed**: `calendar_feeds` (user, `token_hash` (SHA-256 of a 32-byte token), `created_at`, `revoked_at`); `/calendar/feed/<token>.ics` without a session (the token is the credential; constant-time compare of the hash; HTTPS only; 404 on unknown or revoked; rate-limited per IP), returning stored and generated items for −30/+180 days as VEVENTs with stable UIDs; the user's settings page creates, shows once and revokes the token; every feed read is audited with the user, never the token. **Daily tick** (`collections_tick` command, run by a new Container Apps scheduled job `finance-<env>-daily` at 13:00 UTC = 09:00 New York, `STAX_CALLER=none`, which only queues): the ladder (D4), morning e-mails for today's `email_morning` entries to the staff user (catch-all applies), promise reminders | Owner item 6 |
| D9 | **Infrastructure** (one deployment): Key Vault secret **name** `gmail-service-account-key` referenced by the web and worker apps as `GMAIL_SERVICE_ACCOUNT_JSON`; plain env `MAIL_ENABLED`, `GMAIL_SENDER`, `MAIL_CATCH_ALL` as Bicep params (the catch-all address passed on the command line by the owner at deploy time, not committed); the daily job module (clone of the reconcile job module with its command and cron) wired in `main.bicep` and CI (image update + verification) | Owner item 1 |
| D10 | **Roles**: ladder steps/templates/fee types in the Django admin (admin); send a step by hand, contact log, calendar entries, Reschedule (billing; fee waiver admin); feed token per user (any role for their own); `tz_apply` is a command run by Fable on the owner's word | Pattern |

## 5. Interface contract

Settings/env: `TIME_ZONE`, `MAIL_ENABLED`, `GMAIL_SENDER`, `GMAIL_SERVICE_ACCOUNT_JSON` (secret),
`MAIL_CATCH_ALL`, `NSF` fee types in `fee_types`. Packages: `mail` (`get_sender()`, `Message`,
`SentReceipt`, `MailUnavailable`), `ledger/services/{collections,attention,contacts,calendar,nsf}.py`,
`payments/one_off_invoices` (two-line schedule), job kinds `message_send`, `calendar_email`. Commands:
`tz_audit`, `tz_apply --audited N`, `collections_tick`. Models: `CollectionStep`, `MessageTemplate`,
`OutboundMessage` (reshaped), `ContactEntry`, `CalendarEntry`, `CalendarFeed`, `Operator.handled_personally`,
`Operator.nsf_return_count`, `OneOffCharge.companion_of`, `OneOffCharge.source_payment`, categories
`redebit`, `nsf_fee`, `Payment.resolution_kind`/`redebit_date`. URLs: `calendar:page|feed|settings`,
`operators:contacts|contacts_add|contacts_export`, `returns:reschedule`, `charges:send_step`,
`one_off_charges:send_step`, `finance:settings` (feed token). Infra: `containerAppJobDaily.bicep`,
params `gmailServiceAccountSecretName`, `gmailSender`, `mailEnabled`, `mailCatchAll`.

## 6. Work breakdown

| Batch | Task | Done when |
|---|---|---|
| A | D1 time zone + `tz_audit`/`tz_apply`; D2 grid usability; D6 attention list; D7 contact log; D8 calendar, ICS feed, settings page (no e-mail yet); tests | `tz_audit` lists the rows on a fixture; the grid at 1,280 px shows 12 months with a visible scrollbar; attention items appear and link; a promise dated yesterday shows; a calendar note shows on the page and in the ICS |
| B | D3 mail package and fences; D4 ladder tables, templates, opt-out, log, `message_send` job, hand send, `collections_tick` (ladder + morning e-mails); tests with the memory sender; the catch-all rule | On a fixture overdue charge the tick queues the day-3 step, the worker sends to the catch-all with the original recipient in the subject, the log reads `catch_all`, a second tick sends nothing; mail off → `suppressed` |
| C | D5 Reschedule and NSF (fee types seeded, counter, two-line schedule/invoice, allocation, returns UI, attention item clears); D9 infra (vault refs, env, daily job, CI); `.env.example`; tests | Resolving a fixture return with a date creates the re-debit and fee charges and one schedule with two lines; the attention item disappears; Bicep compiles; what-if limited to the job, the env and the secret refs |
| Docs (Fable) | §9 admin steps (Workspace, Cloud project, service account, delegation, vault), DNS findings and the DKIM recommendation, RUNBOOK §15, DATA_OWNERSHIP, ADR-0010 (mail by delegated service account; NSF rule; New York time), README, `tz_audit` report for the owner, QA-011b | |

A → B → C in sequence.

## 7. Test plan (before implementation)

Happy paths per decision; boundaries: a charge exactly 3/10/20 days late at 00:00 and 23:59 New
York (the ladder uses New York days); DST days 2026-03-08 and 2026-11-01 for debit days, the
tick and the nightly gate; a lease ending today; an operator whose e-mail is added after a
suppression (next tick sends); the NSF counter's first vs second return; a waived fee; a return
of a one-off charge; the ICS for a user with no entries; a revoked token; `tz_audit` on rows
exactly at midnight UTC. Error paths: Gmail 401/403/5xx, timeout, malformed key JSON (startup
refusal, value-free); a template with an unknown merge field (refused on save); Reschedule on a
non-returned payment, in the past, twice; a feed token used after revocation. Adversarial (Fable):
the service-account key never in a log line, audit row, page, CSV, error message or the memory
sender's record; a template body with `{__class__}`/`{operator.__dict__}` renders nothing but
the whitelisted fields; `MAIL_CATCH_ALL` unset on `APP_ENV=staging` refuses to boot; a message to
a real address on the sandbox is impossible by construction (test sends to `ceo@example.com` and
asserts the envelope recipient is the catch-all); the ladder never sends to an operator marked
handled personally or without e-mail, never twice for a step, never for a paid or waived charge;
the ICS feed token is not guessable (256 bits, hash stored), a wrong token answers 404 in constant
time, a revoked one 404, and the feed never includes another user's private notes or any amount
beyond what a calendar title should carry (no bank data, no card numbers; guard applied to titles
and bodies); `tz_apply` refuses without a matching audit count and writes exactly the listed rows;
the grid CSS leaves no month unreachable (Playwright scrolls to the last column at 1,280 px);
Reschedule by a viewer → 403; a fee waiver by billing → 403; the NSF fee is never created by the
return webhook; the two-line schedule total equals amount + fee to the cent.

## 8. Risk register

- **Sending real mail from a test environment**: the catch-all is mandatory outside production
  and enforced at boot; the memory sender in tests; `MAIL_ENABLED` off by default.
- **A credential in the app**: the service-account key reaches the process only as a vault
  reference; never read by a model; a test fence on logging, audit, pages and errors.
- **Time-zone change touches every date**: audit before apply; the owner reviews the list; the
  switch is a one-line setting with the whole suite re-run under New York.
- **NSF on bank-side errors**: fee only on the staff action, never on the webhook; waivable.
- **ICS feed without a session**: a 256-bit token, hashed at rest, revocable, audited, rate-limited;
  the feed carries titles and dates only.
- **Two-line Stax invoice**: unverified against Stax until ACH works; the local allocation is tested.
- Irreversible: `tz_apply` rewrites listed rows (each audited with before/after); nothing else.

## 9. Owner steps (written for the Workspace admin; no model performs any of these)

1. **Google Cloud:** create a project (e.g. `finance-mail`); APIs & Services → Library → enable
   **Gmail API**; IAM & Admin → Service Accounts → create `finance-mailer`; Keys → Add key → JSON;
   note the service account's **Client ID** (numeric). Keep the JSON file private.
2. **Workspace Admin:** Security → Access and data control → API controls → **Manage Domain Wide
   Delegation** → Add new → paste the Client ID → scope `https://www.googleapis.com/auth/gmail.send`
   → Authorize.
3. **Key Vault (staging first):** from your own terminal, signed in:
   `az keyvault secret set --vault-name finance-sandbox-kv --name gmail-service-account-key --file <the JSON file>`
   then delete the local file. Never paste the content in chat or in the repo. For live later:
   the same into `finance-kv` under the same name (the app reads by name).
4. **DKIM (recommended):** Workspace Admin → Apps → Google Workspace → Gmail → Authenticate email
   → select the domain → Generate new record (2048-bit, selector `google`) → add the TXT record
   `google._domainkey.globalretailgroups.com` at GoDaddy → back in Admin, **Start authentication**.
   SPF already includes Google; DMARC stays as it is.
5. **Catch-all:** choose the staff address that receives every sandbox e-mail and give it to Fable
   for the deployment parameter.

## 10. Acceptance criteria

Gate green on both engines under `TIME_ZONE = "America/New_York"`; `tz_audit` run on staging and
its list shown to the owner before `tz_apply`; on staging after the deployment: the day-3 e-mail
of a fixture overdue charge arrives at the catch-all and is logged; Reschedule on a fixture return
creates the invoice with the 100.00 NSF line and the attention item disappears; a promise dated
yesterday shows in the attention list; a calendar note for Oct 20 shows on the calendar page and
in the owner's Google Calendar through the ICS feed; the grid shows 12 months with a visible
scrollbar on the laptop.

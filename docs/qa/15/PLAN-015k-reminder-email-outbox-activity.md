# PLAN-015k — Staff reminder e-mails in the Outbox and in the Activity line

**Owner's findings and decisions (2026-11-13). Gate class: small change (display, plus two values
added to an audit record's JSON; no migration, no money path).** Plan by Fable; implementation by
Opus; gate and sign-off by Fable. Record: `docs/qa/15/QA-015.md`. Branch `015k/reminder-outbox`
from main. Its own PR, **built after PLAN-015j is merged.**

## 1. Objective

A staff reminder e-mail and a promise-to-pay morning e-mail are sent directly
(`calendar.send_morning_email` → `mail.get_sender()`), never through the messaging engine, so they
are not Outbox messages: when mail is not set up the sender answers "held" and stores nothing, and
the only record is the audit row `calendar.morning_email` with `after.outcome`. The Outbox page
says "every message is held here", which is untrue for them. This change (1) lists those e-mails
in the Outbox as read-only rows, (2) words the Activity line from the stored outcome, and (3)
stores the address, the subject and the held reason in the audit record from now on.

## 2. Scope

**In:** `backend/ledger/services/calendar.py` (the audit payload of `send_morning_email`),
`services/detail_cards.py` (the Activity words), `services/messaging.py` or a small new
`services/outbox_list.py` (the merged list), `views/outbox.py`, `templates/ledger/outbox/page.html`,
tests, the Outbox and calendar-entry visual baselines if they move.
**Out:** the messaging engine's sending, `OutboundMessage` and its migrations, the mail senders,
the reminder's timing (PLAN-015h), "Send now" for these rows, the CSV exports, the portal.

## 3. Interface contract

- **Audit payload (new rows only).** `calendar.morning_email`'s `after` gains `to` (the author's
  address the message went or would have gone to), `subject` (the message's subject, at most the
  subject limit) and, when held, `reason` (`not_set_up` / `sandbox`, the receipt's). `outcome` and
  `on` unchanged. `no_address` rows carry no `to`. The body is never stored. Rows written before
  this change are not rewritten.
- **Activity words** (`detail_cards`): for this action the line reads, from `after`:
  "Reminder e-mail sent to <to>" (or "Reminder e-mail sent" without `to`), "Reminder e-mail held
  (mail not set up)", "Reminder e-mail held (address not on the allowlist)", "Reminder e-mail
  held" (an older row, no reason), "Reminder e-mail failed", "Reminder e-mail not sent (no
  address)". For a promise to pay the first words are "Promise e-mail". Every other action keeps
  `action_words`.
- **Outbox list.** One list, newest first, 50 a page, of `OutboundMessage` rows and of
  `calendar.morning_email` audit rows whose outcome is sent, held or failed (`no_address` is not a
  message). A staff row shows: When (the audit time); Store "—"; Operator "—" (a promise's
  operator for a promise); Template "Reminder e-mail" / "Promise to pay e-mail" with the subject
  under it (stored, else rebuilt from the entry while it exists, else "—") and no body; Rule "—";
  Status chip Sent / Held / Failed with the reason under it as the engine's rows word it; To the
  stored address, else the author's current address marked "(current address)", else "—";
  Scheduled for "—"; no action. Row id `staff-<audit pk>`.
- **Filters and paging over both sources.** Status: `sent`, `held`, `failed` select both sources;
  any other status selects engine rows only. Held for: `not_set_up`, `sandbox` select both (a
  staff row without a stored reason matches neither). From / To: the audit time's New York day,
  as `queued_at` is filtered. The page's keys come from one ordered query over both sources
  (a UNION of time, source, key) so that no request loads every row; the count is the sum.
  "Send all held for settings" and its count stay engine-only, and the page's note reads "Mail is
  not set up in this environment: every message is held here. Reminder e-mails to staff are listed
  and cannot be sent later."
- **Access:** unchanged (admin only). No new route.

## 4. Work breakdown (Opus; a commit per task)

1. The audit payload (`calendar.py`) with its tests.
2. The Activity words with tests for each outcome and for an older row.
3. The merged Outbox list, filters, paging and template; tests.
4. Baselines if moved; self-review and report.

## 5. Test plan

- Payload: sent, held for each reason, failed, no address; the body is in no audit row; an older
  row (no `to`) still renders everywhere.
- Activity: each line of §3; another action unchanged ("Charge payment recorded").
- Outbox: a held reminder appears with To = the author and "Held" + "mail not set up"; a promise
  e-mail appears; a deleted entry's older row shows "—" for the subject; order by time across
  both sources; page 2 of a mixed list of 120 rows holds the right 50; each filter alone and
  combined; a status no staff row can have (queued) shows engine rows only; counts in the header.
- Query count of the page bounded and independent of the number of rows.
- **Adversarial (Fable):** markup and a control character in a reminder's title and in a stored
  subject and address (escaped, no break); a NUL byte and a hostile value in each filter; a huge
  page number; a billing user and a viewer (403); an audit row of this action with `after` null,
  a list, or an unknown outcome (the page and the Activity card still render); no row offers
  "Send now" for a staff e-mail and a POST to `send_now` with an audit key is 404.
- Runs: the small gate; the new adversarial file also on PostgreSQL before the Publish Request.

## 6. Risk register

- **An address in the audit log** is new: the log is admin-only and already holds user names; the
  owner decided it (2026-11-13). Rule 1 does not apply (no card or account data); the subject is
  a staff-written title and passes the existing payment-data guard at entry.
- **UNION paging** across a model and the audit table: wrong order or count would mislead; pinned
  by the mixed-page tests on both databases.
- **Shared code:** `detail_cards.action_words` serves every Activity card; only this action's
  branch is added. The audit table is large: the query is narrowed by the indexed entity type
  (`calendar_entry`, `contact_entry`: the prefix of the `(entity_type, entity_id, at)` index),
  the action filter kept; `action` has no index.
- Not irreversible; no secret touched; no dependency added.

## 7. Acceptance criteria

1. A reminder e-mail held, sent or failed after this change is a row of the Outbox with the
   author's address, the subject and the status words of §3, and has no "Send now".
2. The Outbox's status, held-for and date filters and its paging return rows of both sources as §3.
3. The reminder's Activity line reads as §3 for each outcome; no line reads "Calendar morning email".
4. No file under `migrations/` changes; `OutboundMessage` is unchanged.
5. Every §5 case exists and passes; the small gate is green; the secret scan is clean.

## 8. Decisions (owner, 2026-11-13)

Display-only Outbox rows, no "Send now"; Activity words from the stored outcome; the address
stored from now on and shown for new lines, older lines read "sent" without it. Fable's additions,
told to the owner: the subject and the held reason are stored too (older held rows read "Held"
with no reason); a row with no address on file is not listed.

## 9. Addition (owner, 2026-11-14): the phone order of a Move-in charges row

PLAN-015j's deferred MINOR goes in this PR: on a phone (the one 719.98px block of app.css) a New
lease row's date / installment fields (`.movein-when`) come directly under its When select, before
"usual" and "More". Files: `finance/static/finance/app.css` (and `_new_lease.html` only if the
order cannot be set in CSS). Laptop layout unchanged. Tests: the phone e2e of
`tests/e2e/test_plan015j_new_lease.py` gains the order (select, then the fields, then More, by
their boxes' y); the two 390 px New lease baselines are regenerated only if the empty page moves.
Done after the branch is rebased on the main that holds PLAN-015j (task 5). Still small-change
class: a stylesheet rule.

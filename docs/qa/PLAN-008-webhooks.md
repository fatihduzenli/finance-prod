# PLAN-008 — Webhook receiver, processor, payment state machine, ACH return path

**Date:** 2026-10-01 · **Planner / QA owner:** Fable · **Implementer:** Opus
**Inputs:** Prompt 8 and the owner's amendments (2026-10-01, which win on conflict); `docs/CLAUDE.md`
Rules 1 to 4, 8; `docs/DATA_OWNERSHIP.md` §3; `docs/STAX_CLIENT.md`; `docs/inputs/STAX_ENDPOINTS_2026-09-30.md`
(webhook registration `POST /webhook` with `target_url` and `event`; event table on the Merchant
Webhooks page: `create_customer`, `update_customer`, `create_invoice`, `update_invoice`,
`send_invoice`, `create_scheduled_invoice`, `update_scheduled_invoice`, `delete_scheduled_invoice`,
`create_transaction`, `update_transaction`, `update_transaction_settled`, `create_deposit`,
`create_dispute`, `update_dispute`, `update_payment_method`; the page says a webhook's payload is
the same object `GET` returns); `docs/TODO-VERIFY.md` rows 002, 015.

## 1. Restated objective

Stax posts unsigned events to a URL that carries our secret token. The receiver compares the
token in constant time, answers 404 on any mismatch, stores the redacted raw event with a
deduplication hash, answers 200 at once, and hands the event to a processor that runs outside
the request. The processor never trusts the body: it re-fetches the object from Stax by ID
through the rate-limited client and applies it idempotently. Invoices mirror provider status,
dates and amount paid through the sync writer; transactions become payments with the state
machine pending → settled, pending → returned, plus voided and refunded; a return reopens the
rent charge, records the code and reason, and lands in a returns queue for staff; nothing
re-debits by itself. Failures are recorded, counted, and dead-lettered after three attempts with
a visible banner. The app registers its own webhooks against the staging URL and keeps the
registration IDs.

## 2. Stop conditions, answered

| Condition | Answer |
|---|---|
| Signatures or published source IPs available | `docs/STAX_ANSWERS.md` and TODO-VERIFY-002: no signature, no published IPs. The URL-token design stands. |
| Platform cannot answer before Stax's timeout | The handler does a token compare, one insert and returns; no provider call. Container Apps answers in milliseconds. Stax's timeout and retry policy are not in the spec (TODO-VERIFY-039); the design does not depend on them. |
| Spec silent on registration or event shapes | Registration is documented (`POST /webhook`, `target_url`, `event`; response shape with an `id` assumed, TODO-VERIFY-040). Event bodies are "the same object GET returns" (documented). **Where the event name travels is not documented** (header `stax-event-name` per TODO-VERIFY-015, or body): the receiver accepts the header, then body keys `event`/`type`/`event_name`, then classifies by the object's shape, and records which source it used (amendment 6). Reported, not stopped on. |

## 3. Decisions

| # | Decision | Reason |
|---|---|---|
| D1 | **Asynchronous processing without Celery (until Step 10):** the web process (the single Stax caller) runs one background worker thread fed by an in-process queue; the request handler stores the event and enqueues its ID. A management command `process_webhooks` processes any event with `processed_at` null (catch-up after a restart) and is the Step 10 entry point. Nothing in the request touches Stax | Prompt 8 item 2d; one caller; no new infrastructure |
| D2 | Route `POST /api/webhooks/stax/<token>/`, public and CSRF-exempt, listed in the registry as the one public unsafe route besides login; GET and other methods 404; wrong token 404 with an empty body and no database write; body larger than 256 KB → 404 as well (nothing revealed); constant-time compare with `hmac.compare_digest` against the configured token | Prompt 8 item 1 |
| D3 | Stored event: `event_type` (as resolved), `event_source` (`header`, `body`, `shape`, `unknown`; new column), `provider_object_id`, `dedupe_hash` = SHA-256 of event type + raw body bytes, `payload` (new jsonb column, redacted per Rule 1 by the existing `summary()` logic applied recursively, and guarded), `attempts`, `last_attempt_at`, `outcome` (`received`, `processed`, `duplicate`, `ignored`, `failed`, `dead_letter`), `error` (class name and field names only). Migration on `webhook_events` | Prompt 8 item 2b; amendment 8 |
| D4 | Processor per type, always after a re-fetch by ID: **customer** → operator fields that Stax owns? none; only `provider_snapshots` refreshed and the operator's sync state confirmed (our record is never overwritten, DATA_OWNERSHIP §3); **invoice** → local invoice found by `external_refs`, else created and linked to the operator by customer ID (`rent_charge` null when no schedule match; the matching logic of Prompt 7 is reused when `schedule_id` is present); `provider_status`, `sent_at`, `viewed_at`, `paid_at`, `amount_paid` written only through `provider_write()`; **schedule** → `collection_schedules.state` confirmed or flagged, never amounts; **transaction** → payment upsert by `external_refs` (`external_kind = transaction`), ACH only (a card transaction is recorded as `ignored: card`), linked to its invoice through the transaction's `invoice_id`, then that invoice is re-fetched too; **deposit** → `deposits` upsert; **dispute** → `audit_log` entry and `needs_review` on the linked charge (no dispute table yet) | Prompt 8 item 3; amendments 2, 5 |
| D5 | **State machine:** `create_transaction` (ACH, `success` true) → payment `pending`; `update_transaction_settled` with the transaction settled → `settled`, charge `amount_paid` recomputed and status `paid`/`partial`; with the transaction not settled → `returned`, `returned_at`, `return_code` and `return_reason` from the re-fetched transaction (`child_transactions[0].meta.achDescription` per the ACH page, TODO-VERIFY-041 for the exact keys), charge reopened to `unpaid` (or `partial`), `needs_review`, and the payment enters the **returns queue**; a transaction of type refund with a parent → new payment `is_refund`, parent linked, invoice re-fetched; voided → `voided`. **A charge is never paid on `create_transaction`.** Insurance transactions (invoice linked to an insurance charge) follow the same path with `kind = insurance` and `policy_year` | Amendments 2, 3, 5 |
| D6 | **Returns queue** `/returns/`: returned payments not yet resolved, with code, reason, operator, store, amount; "Resolve" (billing) with a mandatory note sets `resolved_at`/`resolved_by`/`resolution_note` (new columns on `payments`); no re-debit action exists in this plan | Amendment 3 |
| D7 | **Idempotency:** the dedupe hash rejects byte-identical redeliveries as `duplicate`; processing of distinct deliveries is idempotent because every write is an upsert keyed on external IDs and recomputations are pure; **replay** of a stored event (billing, button on the event page) re-runs the processor and must change nothing | Prompt 8 item 3 and test 1 |
| D8 | **Dead letter:** after three failed attempts `outcome = dead_letter`; `/webhooks/` staff page lists events with filters and a replay button; the admin registers `WebhookEvent` read-only; the run page shows a banner when any dead-letter event exists; a CRITICAL log line and a Sentry event (fingerprint `webhook-dead-letter`) on entering dead letter | Amendment 8; Prompt 8 item 5 |
| D9 | **Registration** by management command `register_webhooks` run inside the staging container: for each event in the list below, `POST /webhook` with `target_url = <APP_BASE_URL>/api/webhooks/stax/<token>/`; the returned ID stored in `external_refs` (`entity_type = webhook_registration`, `entity_id` = a deterministic UUID5 of the event name, `external_kind = webhook`); re-running lists existing registrations first (`GET /webhook`) and creates only the missing ones; `unregister_webhooks` deletes by stored ID. Refuses when `APP_ENV = local`. The command never prints the URL with the token; it prints the event names and masked IDs | Amendment 7 |
| D10 | **Event list registered:** `create_customer`, `update_customer`, `create_invoice`, `update_invoice`, `send_invoice`, `create_scheduled_invoice`, `update_scheduled_invoice`, `delete_scheduled_invoice`, `create_transaction`, `update_transaction`, `update_transaction_settled`, `create_deposit`, `create_dispute`, `update_dispute`, `update_payment_method`. The spec exposes no separate ACH-return event; returns arrive as `update_transaction_settled` with a non-settled result (TODO-VERIFY-041) | Amendment 1 |
| D11 | **Token hygiene:** the token never appears in a log line (the redacting formatter already holds it), in `audit_log`, in `webhook_events`, or in Sentry: `finance/sentry.py` drops `request.url` as well. The 404 path logs the request as `path=/api/webhooks/stax/[token]/` (the view's own log line, not Django's, which is suppressed for this route by returning 404 without raising) | Checkpoint 8 |
| D12 | **Month grid state:** a `pending` payment on a charge gives the charge status `pending` (the sheet's "9/9 pending"); the grid itself is Prompt 11, the status value exists now | Amendment 2 |
| D13 | **Sandbox proof** for transaction, settlement, deposit and return events is "blocked on Stax ACH"; they are unit-tested with recorded-shape payloads built from the spec's examples | Owner |

## 4. Interface contract (abridged; the decisions above are the contract)

Routes: `webhooks:receive` (`POST /api/webhooks/stax/<token>/`, public), `webhooks:list` (`/webhooks/`, GET any role),
`webhooks:detail` (`/webhooks/<id>/`), `webhooks:replay` (POST, billing), `returns:list` (`/returns/`),
`returns:resolve` (POST, billing). Commands: `process_webhooks`, `register_webhooks`, `unregister_webhooks`.
Modules: `backend/payments/webhooks/` (`receiver.py`, `queue.py`, `processor.py`, `handlers/`),
`backend/ledger/services/returns.py`, views and templates under `ledger/`. New columns: `webhook_events`
(`event_source`, `payload`, `attempts`, `last_attempt_at`), `payments` (`resolved_at`, `resolved_by`, `resolution_note`).
Provider interface: `list_webhooks`, `create_webhook`, `delete_webhook` (verified paths), used only by the two commands.

## 5. Work breakdown

| Batch | Task |
|---|---|
| A | Receiver, storage, dedupe, queue thread, `process_webhooks`, registry and CSRF exemption, Sentry URL scrub, event list and detail pages, replay, dead letter, banner, admin read-only; tests |
| B | Handlers: customer, invoice (with Prompt 7 matching reuse), schedule, transaction state machine, refund, void, return path and returns queue, deposit, dispute; insurance path; `register_webhooks`/`unregister_webhooks` and the provider methods; tests |
| C | README rows; `.env.example` unchanged (token exists) |

Docs by Fable: `docs/RUNBOOK.md` (registration steps, what to do on a dead letter), DATA_OWNERSHIP §3 additions, TODO-VERIFY 039 to 041.

## 6. Test plan

| # | Case | Author |
|---|---|---|
| 1 | Replaying the same webhook five times (byte-identical: four `duplicate`s; distinct deliveries of the same object: identical state after each) | Opus |
| 2 | Wrong, missing, truncated, over-long, case-changed token and a token with a trailing slash variant → 404, empty body, zero rows written; GET → 404; oversize body → 404, zero rows | Opus |
| 3 | **Lying payload:** body says invoice PAID; re-fetched invoice says SENT → invoice not paid, `provider_status` SENT, charge unchanged; the body's status is never read into any field | Opus |
| 4 | Refund event: new payment row with `is_refund`, parent linked, parent invoice re-fetched and updated | Opus |
| 5 | State machine: created → pending (charge not paid); settled → paid (amount and status); not settled → returned (code, reason, charge reopened, queue entry); voided; insurance kind and policy year; a settled event arriving before the created event; the same settled event twice | Opus |
| 6 | Card transaction ignored; non-JSON body stored and `ignored`; unknown event type stored and `ignored`; event source recorded as header, body or shape | Opus |
| 7 | Failure handling: a provider failure during processing records the class name, increments attempts, no tight retry; third failure → dead letter, CRITICAL line, Sentry event, banner on the run page | Opus |
| 8 | Registration command: creates the missing registrations only, stores IDs, refuses in local, never prints the token; unregister deletes by stored ID | Opus |
| 9 | Receiver makes no provider call and no outbound request; processing uses the limiter and breaker (a burst of 50 events: departures at most 2 per second) | Opus |
| 10 | Returns queue: list, resolve with mandatory note, roles, no re-debit route exists | Opus |
| 11 | Adversarial: token compare timing (constant-time function asserted), body with a card number is redacted in `payload` and never in `error`; event claiming another merchant's object; object ID with path characters; 10,000-event dedupe table growth; concurrent delivery of the same event twice (PostgreSQL); replay by viewer 403; a transaction linked to an invoice of another operator; Sentry event contains no URL; the token never appears in any log line during a 404 or a 200 | Fable |
| 12 | Whole suite both engines, lint, format, types, migrations, secret scan | Fable |

## 7. Risk register

| Risk | Treatment |
|---|---|
| Processing inside the web process is lost on restart | Events persist with `processed_at` null; `process_webhooks` catches up; Step 10 moves to Celery |
| Payload trusted "just for the status" | Handlers receive only the event type and object ID; the body is never passed to them (test 3 and 11) |
| Burst of webhooks pushes toward the lockout | One worker thread, limiter and breaker in front of every re-fetch; dedupe before any fetch |
| Token leaks via logs or Sentry | Redacting formatter; D11; test 11 |
| Event name location unknown | Three-way resolution with the source recorded; TODO-VERIFY-015 |
| ACH shapes unknown | Built from the spec's examples; sandbox proof blocked on Stax ACH (D13) |

## 8. Acceptance

Gate green on both engines; migrations clean and reversible; every row of §6 has a passing
test; on staging: an operator edit produces an `update_customer` event received, deduplicated
and processed within a minute; a dashboard invoice appears matched to the customer with
`provider_status` written by the sync writer; a replay changes nothing; a wrong token gets 404
and stores nothing.

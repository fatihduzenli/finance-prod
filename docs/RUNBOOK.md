# RUNBOOK.md — operating the Stax integration

**Date:** 2026-10-01. Companion to `docs/STAX_CLIENT.md` (limiter, breaker, alerts) and
`infra/README.md` (Azure). Every step here that changes a live system is a Publish action under
the root `CLAUDE.md` §6 and needs the owner's word in the moment.

## 1. Webhooks: how they work

- Stax posts events to `https://<app host>/api/webhooks/stax/<token>/`. The token is
  `STAX_WEBHOOK_URL_TOKEN` from the environment's Key Vault, 64 or more random characters.
- The receiver compares the token in constant time, answers **404** with an empty body to
  anything that is not an exact match (and to GET), stores the redacted event with a
  deduplication hash, answers **200**, and hands the event to a worker thread in the same
  process. The request never calls Stax.
- The processor re-fetches the object from Stax by ID through the rate-limited client and
  applies it. **The body is never trusted**; handlers receive only the event type and the ID.
- A processing failure records the error class and the field names, counts an attempt and
  stops. After three failed attempts the event is **dead-lettered**: a CRITICAL log line, a
  Sentry event with fingerprint `webhook-dead-letter`, and a banner on the run page.
- `python backend/manage.py process_webhooks` processes any stored event that is not yet
  processed and not dead-lettered, oldest first. Run it after a restart or when the banner shows
  failed events; it is also what Step 10's worker will call.

## 2. Registering the webhooks (staging first, live at Step 16)

The app registers its own webhooks; nothing is typed into the Stax dashboard, and nothing is
registered from a laptop.

1. The environment's `APP_BASE_URL` must be the public HTTPS host and
   `STAX_WEBHOOK_URL_TOKEN` must be set in the vault.
2. With the owner's approval, run inside the container:

   ```
   az containerapp exec -g finance-sandbox -n finance-staging-web \
     --command "python backend/manage.py register_webhooks"
   ```

   The command lists the existing registrations, creates only the missing ones for the fifteen
   events below, stores each registration ID in `external_refs`
   (`entity_type = webhook_registration`), and prints event names and masked IDs. It never
   prints the URL.
3. Events registered: `create_customer`, `update_customer`, `create_invoice`, `update_invoice`,
   `send_invoice`, `create_scheduled_invoice`, `update_scheduled_invoice`,
   `delete_scheduled_invoice`, `create_transaction`, `update_transaction`,
   `update_transaction_settled`, `create_deposit`, `create_dispute`, `update_dispute`,
   `update_payment_method`. Stax exposes no separate ACH-return event; a return arrives as
   `update_transaction_settled` with a non-settled result.
4. To remove them (before rotating the token or at cutover): `unregister_webhooks`, same way.
5. **Rotating the token:** set the new value in the vault, deploy so the app reads it, run
   `unregister_webhooks` **before** the deploy with the old value if possible, then
   `register_webhooks` after. During the gap Stax's deliveries get 404 and are retried by Stax
   or caught by the nightly reconciliation (Step 10).

## 3. Checking a delivery

- `/webhooks/` lists events with type, source of the event name (header, body, shape),
  outcome, attempts and duplicates; `/webhooks/<id>/` shows the redacted payload.
- To prove the endpoint from outside: a POST to the URL with a wrong token must return 404
  with an empty body and add no row to `/webhooks/`.
- Replay (billing): re-runs the processor on a stored event; the result must be the same
  state. A replay is how a failed event is retried after the cause is fixed.

## 4. When the dead-letter banner shows

1. Open `/webhooks/`, filter by outcome `dead_letter`, open the event.
2. Read the error class and field names. `ProviderRejected` with field names means the
   re-fetch was refused; `ProviderUnavailable` or `TransportError` means Stax or the network;
   `CircuitOpen` means the breaker was open (see `docs/STAX_CLIENT.md` §6).
3. Fix the cause, then **Replay**. Do not replay in a loop; one click, read the outcome.
4. If the object no longer exists at Stax the outcome is `ignored: not at the provider`; that
   is final.

## 5. Returns queue

`/returns/` lists ACH returns (payment `returned`) that nobody has resolved. Each shows the
return code and reason as Stax reported them. Resolving needs a note. **Nothing re-debits by
itself**: a new debit needs the operator's authorisation and is a staff action in a later
prompt.

## 6. Payment states

`pending` (debit created, money not yet moved; the sheet's "9/9 pending") → `settled` (the
charge is paid) or `returned` (the charge is reopened, the return goes to the queue);
`voided`; a refund is a new payment row marked as a refund and linked to its parent. A charge is
never paid on creation.

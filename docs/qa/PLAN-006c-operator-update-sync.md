# PLAN-006c — An edit of a linked operator updates the Stax customer

**Date:** 2026-10-02 · **Planner / QA owner:** Fable · **Implementer:** Opus
**Origin:** Checkpoint 8 click 1 (owner, 2026-10-02): an edited phone never reached Stax and the
page showed "linked" with no failure. Root cause: PLAN-006's `update_operator` never calls the
provider, a deviation Fable accepted in QA-006 and should not have. Prompt 6 items 2 and 3 apply
to update as well as create.

## 1. Rules

1. `update_operator` on an operator with a current Stax customer reference, when any field that
   the customer payload carries changed (display name, legal name, company, email, phone; the
   address fields once they are sent), saves locally first and then, inside the same row lock and
   through the same service path as create, calls `update_customer(customer_external_id,
   CustomerInput)` built by the existing `customer_input()` (same field rules: phone normalised,
   empty optional fields omitted, 255 caps). Success: `sync_status = linked`, audit
   `operator.sync_updated` with the changed field names (names only, not values beyond the
   masked email domain). Failure (`ProviderError`): `sync_status = failed`, `sync_failure` =
   class name, `sync_failure_fields` from the error, `sync_attempted_at`, audit
   `operator.sync_failed`, message on the page: "Saved here. The payment provider did not accept
   the update: <class> (fields …). Retry when ready." The local change stands.
2. `sync_operator(..., retry=True)` on a `failed` operator that already has a customer reference
   performs the **update**, not a lookup or create; the 60-second lock applies.
3. An edit that changes none of the payload fields makes no provider call and keeps the status.
4. An edit that removes the email of a linked operator still updates Stax (email omitted) and
   stays `linked`; the receipts note changes accordingly.
5. A status change on the page: after a successful update the detail page shows "Provider
   customer updated <time>"; after a failure the existing failure block and retry button.
6. A webhook `update_customer` for that customer then arrives; the handler already refreshes the
   snapshot without overwriting ours (PLAN-008). Nothing changes there.

## 2. Tests

| # | Case | Author |
|---|---|---|
| 1 | Linked operator, phone changed → `update_customer` called once with the normalised digits; status linked; audit `operator.sync_updated` names `phone` only | Opus |
| 2 | Linked operator, provider rejects (422 with fields) → local change kept, status failed, failure class and fields stored and shown, retry button after 60 s; retry calls `update_customer` once, not `create_customer` or `find_customers` | Opus |
| 3 | Edit of a non-payload field (notes, address while not sent) → no call | Opus |
| 4 | Email removed on a linked operator → update sent with no email, status linked | Opus |
| 5 | Unlinked operator (awaiting link or pending) → behaviour unchanged (no update call) | Opus |
| 6 | Two concurrent edits of one linked operator → two sequential calls or one, never a crash; final state consistent (PostgreSQL) | Opus |
| 7 | Adversarial: provider rejects with a body naming the token; a 5xx after retries; a phone that fails the rule is refused by the form before any call; an edit by billing on an archived operator; the audit row never carries the full email | Fable |
| 8 | Whole suite both engines, lint, format, types, migrations | Fable |

## 3. Acceptance

Gate green; on staging: editing the phone of a linked operator produces `PUT /customer/{id}` in
the transport log, the Stax dashboard shows the new phone, an `update_customer` webhook arrives
and is processed, and the page shows the outcome.

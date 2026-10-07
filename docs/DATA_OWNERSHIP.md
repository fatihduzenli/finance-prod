# DATA_OWNERSHIP.md — who owns each field, and what happens when the two sides disagree

**Date:** 2026-09-29 · **Schema:** `docs/qa/PLAN-004-schema.md` · **Status:** for the owner's
line-by-line review at Checkpoint 4.

## 1. The rule in one paragraph

Our database owns **business data**: who the operators are, which stores they lease, what
rent and insurance are due, what staff wrote and decided. **Stax owns money state**: whether
an invoice was sent, viewed or paid, and whether a payment is pending, settled or returned.
We keep a copy of money state so screens are fast. The copy is never the truth. When the copy
and Stax disagree, **Stax wins for payment fields, the copy is overwritten, and the
disagreement is written to `audit_log`.** Business fields are never overwritten from Stax.

## 2. Owners used in the tables below

| Owner | Meaning | Who may write it |
|---|---|---|
| **App** | Business data. Ours. | Staff through the app, within their role |
| **Stax** | Money state copied from the provider | Only the sync layer (webhook processing and the reconciliation job) |
| **Derived** | Computed from other fields | Nobody directly; the database or the app computes it |
| **System** | Bookkeeping: identifiers, timestamps | The app, automatically |
| **Import** | Written once by the sheet migration | The import, then read-only |

## 3. When the two sides disagree

| Situation | What happens |
|---|---|
| Copied invoice status differs from Stax | Copy overwritten with Stax's value. `audit_log` row with before and after. |
| Copied payment status or amount differs from Stax | Copy overwritten. `audit_log` row. The charge's paid amount is recomputed. |
| Stax reports a payment we have no row for | Row created from Stax. `audit_log` row. Staff see it as unmatched until it is linked to a charge. |
| We hold a Stax-method payment that Stax does not know | Never deleted automatically. Flagged for staff. `audit_log` row. |
| A payment recorded by hand (Zelle, wire, other) | Ours. Stax has no knowledge of it and cannot overrule it. |
| Invoice total in Stax differs from ours | **Not overwritten.** The amount due is business data. Flagged for staff, `audit_log` row, and collection on that charge stops until resolved. |
| Operator name or email differs between Stax and us | **Not overwritten.** Ours is the business record. Flagged for staff. For email the flag says: receipts go to the address stored in Stax; fix it in Stax (owner, 2026-09-30). |
| A provider status string we have never seen | Stored exactly as received. Never mapped to a guess. |
| A pending payment older than the configured number of days with no news | Flagged as `pending_too_old` on `/reconciliation/` and alerted through Sentry (fatal); threshold `PENDING_PAYMENT_MAX_BUSINESS_DAYS=5` US business days (Prompt 10, owner amendment 3). |
| The nightly reconciliation finds a transaction at Stax with no local payment | Row created through the webhook handler's own path; `payment_restored` mismatch; `audit_log` row. An orphaned `external_refs` row left by a deleted payment is removed first (audited). |
| A deposit row names a transaction we hold no settled payment for, or a settled ACH payment of the last 14 days (at least 3 days old) is in no deposit | `deposit_unmatched` / `settled_payment_undeposited` mismatch for staff; nothing is written to the payment. Field names unverified until live data (TODO-VERIFY-030, 060). |
| Stax's ACH rejection list names a payment no return webhook reached us for | The existing return path is applied from the re-fetched transaction (`returned`, charge reopened, returns queue); `return_missed_by_webhook` mismatch. A rejection naming no local payment is counted only (TODO-VERIFY-061). |
| A deposit (refundable fee type) is paid | Held as a liability: excluded from every revenue figure (P&L, ledger totals); shown as "Deposits held". Resolved once at lease end: refunded (money out), withheld (revenue that month) or transferred (a paid deposit on the operator's other lease). |
| A store has no `store_costs` row | Landlord rent and margin are **unknown**, never 0, on the store page and in the P&L; the row is flagged. |
| A mismatch is resolved by staff | Note required (guarded, invisible characters do not count); `resolved_at`, `resolved_by`, `audit_log` row. The underlying data is not changed by resolving. |
| A webhook body says an invoice is paid or a transaction settled | **Ignored.** Only the object re-fetched from Stax by ID is applied (Prompt 8). The body is stored, redacted, for the record. |
| A wire, Zelle or other payment is recorded against a charge that has a Stax invoice | Our ledger keeps the real method. **Stax is told the invoice was paid "in cash"**, with the memo "Paid by <method> on <date>. <note>", because Stax offers no wire or Zelle method and this is what staff did by hand in the dashboard (owner Q19, 2026-10-02). Before marking, the invoice is re-fetched; if Stax already shows it paid, nothing is sent and the mismatch is recorded. A **partial** payment is not sent to Stax until the unit of `apply_balance` is verified (TODO-VERIFY-028); the charge is flagged. |
| An ACH return arrives for a settled payment | Payment `returned`; the charge's paid amount is recomputed and it reopens; the return enters the returns queue; nothing re-debits by itself. |

The detection and repair code arrives with Prompts 8 and 10. This document fixes the rule
they implement.

## 4. How the database enforces it

| Guard | Where | What it does |
|---|---|---|
| Provider-status guard | PostgreSQL trigger on `invoices` | A write to `provider_status` is refused unless it comes through `payments.sync_writer.provider_write()`. |
| Column comment | `invoices.provider_status` | States that only the sync layer writes it. |
| Card-number guard | Application validator on every open text and JSON column, plus a PostgreSQL trigger on 17 tables | Refuses any 13 to 19 digit number that passes the Luhn check, with a message that never repeats the value. |
| Append-only audit | PostgreSQL trigger on `audit_log` | Refuses every UPDATE and DELETE. |
| Identifier isolation | `external_refs` | The only place a Stax ID of a business record is stored. No business table has a provider ID column. |
| Exact money | `numeric(12,2)` everywhere, `Decimal` in code | Inexact binary numbers are refused by the field itself. |
| Report indexes | `rent_charges (status, due_on)`, `payments (settled_on, status)`, `invoices (provider_status)`, `one_off_charges (operator_id, status)`, `insurance_charges (lease_id, due_on)` (migration 0022) | Read paths of the dashboard, arrears, aging and "failed attempts"; no ownership change. |

**Limits you should know.**
- The triggers exist on PostgreSQL, which is what staging, production and CI use. A local
  sqlite database has the application validators only.
- The provider-status guard prevents accidents and bugs. It is not a barrier against a person
  with direct database access, who could set the same session flag.
- About one in ten random 13 to 19 digit numbers passes the Luhn check by chance. A long
  reference number typed into a note can therefore be refused. Writing it with a letter or a
  dot in it avoids that. Tests over 10,000 ordinary phone numbers, invoice numbers, amounts
  and dates produced no refusal.
- The guard detects card numbers. Bank routing and account patterns are checked by the
  application only, not by the database trigger.

## 5. Field by field

### operators
| Field | Owner | Note |
|---|---|---|
| `id`, `created_at`, `updated_at` | System | |
| `display_name`, `legal_name`, `company` | App | Never overwritten from Stax |
| `email`, `phone` | App | Email is optional; used for receipts only |
| `address_line1`, `address_line2`, `city`, `state`, `postal_code`, `country` | App | |
| `notes` | App | Guarded free text |
| `enrolment_status` | App | Set by staff when the Stax customer and payment-method IDs are linked |
| `sync_status`, `sync_failure`, `sync_attempted_at` | System | Our record of the last attempt to find or create the Stax customer; `sync_failure` is a class name only |
| `creation_key`, `needs_review` | System / App | Duplicate-submission guard; review flag set when several Stax customers matched |
| `archived_at` | App | |

### stores
| Field | Owner | Note |
|---|---|---|
| `id`, `created_at`, `updated_at` | System | |
| `external_label`, `store_number`, `city`, `state` | App | The label is not unique and is never a key |
| `open_date`, `open_date_note`, `closed_at`, `status` | App | |
| `needs_open_date` | Derived | True exactly when `open_date` is empty; blocks insurance scheduling only |
| `needs_review` | App | Set by the import, cleared by staff |

### leases
| Field | Owner | Note |
|---|---|---|
| `id`, `created_at`, `updated_at` | System | |
| `store_id`, `operator_id` | App | |
| `debit_day`, `second_debit_day` | App | |
| `collection_status`, `review_reason`, `payment_channel` | App | A lease with an unknown channel cannot be active |
| `started_on`, `ended_on`, `end_reason`, `needs_review` | App | |

### lease_rent_history
| Field | Owner | Note |
|---|---|---|
| `id`, `created_at` | System | |
| `lease_id`, `amount`, `effective_from`, `effective_to` | App | The only record of what the rent was and when |
| `source` | Import / App | `sheet_current`, `sheet_inferred` or `staff` |

### collection_schedules
| Field | Owner | Note |
|---|---|---|
| `id`, `created_at`, `updated_at` | System | |
| `lease_id`, `kind`, `seq`, `debit_day`, `amount` | App | What we intend the schedule to be |
| `state` | App | Our view of the schedule. Stax's own schedule record is reached through `external_refs`. If Stax's amount or day differs, collection stops and staff decide |

### rent_charges
| Field | Owner | Note |
|---|---|---|
| `id`, `local_ref`, `created_at`, `updated_at` | System | `local_ref` guards against duplicate creation |
| `lease_id`, `period_month`, `seq`, `due_on`, `amount_due` | App | One row per lease, month and debit |
| `amount_paid` | Derived | Sum of settled payments for the charge; recomputed after every payment change |
| `balance` | Derived | Computed by the database: due minus paid |
| `status` | Derived | From payments: a Stax outcome changes it only through the sync layer |
| `legacy` | Import | True for history from the sheet; never pushed to Stax |
| `needs_review` | App | |

### insurance_premiums
| Field | Owner | Note |
|---|---|---|
| `id`, `updated_at` | System | |
| `policy_year`, `amount` | App | Admin only |
| `cost` | App | Admin only; our cost of the policy year (PLAN-010b D9); null = unknown, never 0 |

### insurance_charges
| Field | Owner | Note |
|---|---|---|
| `id`, `created_at`, `updated_at` | System | |
| `lease_id`, `policy_year`, `due_on`, `amount` | App | Generated from the store's open date and the premium table |
| `needs_premium` | Derived | True exactly when `amount` is empty |
| `needs_review` | App | |
| `legacy`, `legacy_status` | Import | Status read from the sheet's cell note for imported years |
| *(status)* | Derived | Not stored. Paid, pending or unpaid is computed from payments each time |

### charge_notes, notes
| Field | Owner | Note |
|---|---|---|
| `id`, `created_at` | System | |
| charge or entity reference | App | |
| `body` | App | Guarded free text. Never sent to Stax |
| `author_id`, `author_label` | System | `author_label` names the import when no user wrote it |

### charge_adjustments
| Field | Owner | Note |
|---|---|---|
| `id`, `created_at`, `created_by_id` | System | |
| `from_charge_id`, `to_charge_id`, `amount`, `reason` | App | |
| `note` | App | Mandatory. Guarded free text |

### store_balances
| Field | Owner | Note |
|---|---|---|
| `id`, `created_at` | System | |
| `store_id`, `kind`, `amount` | Import | Opening values from the sheet, sign as in the sheet |
| `needs_review` | App | |

Superseded by `one_off_charges` with `legacy=true` (PLAN-010b D13, PLAN-010d D1); removed in Step 13.

### fee_types
| Field | Owner | Note |
|---|---|---|
| `id`, `created_at`, `updated_at` | System | |
| `code`, `name`, `default_cost`, `default_price`, `refundable`, `active`, `sort_order` | App | Admin only. `refundable` marks a deposit (a liability). `first_year_insurance` is seeded inactive (ADR-0007) |

### one_off_charges (was `setup_charges`; PLAN-010d D1)
| Field | Owner | Note |
|---|---|---|
| `id`, `created_at`, `updated_at` | System | |
| `operator_id`, `lease_id` | App | The operator always (except a legacy setup row on a lease without one); the lease when one applies |
| `category`, `collection`, `description` | App | `setup`, `arrears`, `adjustment`, `damage`, `other`; how it is collected; the invoice line (guarded) |
| `fee_type_id`, `price`, `cost`, `vendor`, `due_date`, `note` | App | `fee_type` for `setup` only. `price` is what the operator owes; `cost` is ours, null = unknown. Price locked once a payment exists |
| `amount_paid`, `balance`, `status` | Derived | From settled payments, as `rent_charges`; `waived` and `voided` set by their actions |
| `waived_at`, `waived_by_id`, `waive_note` | App | Admin; note mandatory |
| `voided_at`, `voided_by_id`, `void_note` | App | Billing, after the provider confirmed the invoice is deleted; note mandatory |
| `charge_now_key` | System | The idempotency ID of the single "Charge now" attempt; set once, never changed |
| `review_message`, `needs_review` | App / Stax | Provider refusal text (class names only), reference conflicts |
| `legacy` | Import | Step 13 opening balances |

### deposit_resolutions
| Field | Owner | Note |
|---|---|---|
| `id`, `created_at` | System | |
| `setup_charge_id`, `kind`, `amount`, `withheld_amount`, `resolved_on`, `method`, `note`, `to_lease_id`, `created_by_id` | App | Admin; once per deposit. `refunded` writes a `refund_deposit` payment; `transferred` creates a paid deposit on the target lease; `withheld` becomes revenue in its month |

### store_costs
| Field | Owner | Note |
|---|---|---|
| `id`, `created_at`, `updated_at` | System | |
| `store_id`, `landlord_rent`, `other_monthly_cost`, `other_cost_description`, `landlord_lease_end`, `effective_from`, `effective_to`, `notes` | App / Import | Date-effective, one open row per store, no overlap among live rows. No row = cost unknown, never 0. `source` says `staff` or `import` |
| `superseded_at`, `superseded_by_id`, `change_note` | App | A correction (admin) writes a new row and marks the old one superseded; a superseded row counts for nothing and stays as history |

### invoices
| Field | Owner | Note |
|---|---|---|
| `id`, `creation_key`, `created_at`, `updated_at` | System | `creation_key` guards against duplicate creation |
| `invoice_number` | App | Format decided at Checkpoint 4 |
| `operator_id`, `rent_charge_id`, `insurance_charge_id`, `charge_adjustment_id` | App | At most one source |
| `issue_date`, `due_date`, `memo` | App | |
| `subtotal`, `tax`, `total` | App | If Stax's total differs: flagged, never overwritten |
| `internal_status` | App | Our workflow |
| **`provider_status`** | **Stax** | Sync layer only. Stored exactly as Stax returns it |
| **`amount_paid`** | **Stax** | Overwritten from Stax on disagreement |
| **`sent_at`, `viewed_at`, `paid_at`** | **Stax, derived** | The invoice object carries no such timestamps (observed 2026-10-02). Each is set once, from the time our receiver got the first webhook whose re-fetch showed the status (sent, viewed, paid). A timestamp Stax ever reports would win. |
| `allow_card` | App | Always false. The business accepts no cards |
| `allow_ach` | App | |
| `legacy` | Import | |

### invoice_line_items
| Field | Owner | Note |
|---|---|---|
| all fields | App | Totals are validated against Stax's response when the invoice is created (Prompt 7) |

### payments
| Field | Owner | Note |
|---|---|---|
| `id`, `created_at`, `updated_at` | System | |
| charge and invoice references (`rent_charge`, `insurance_charge`, `charge_adjustment`, `one_off_charge`), `kind`, `policy_year` | App | Linking a payment to a charge is our decision. Kinds `one_off` and `refund_deposit` belong to a one-off charge; `refund_deposit` is money out, manual only, never refundable or voidable |
| `method` | Stax for `ach`; App for `zelle`, `wire`, `other` | |
| **`amount`, `status`** | **Stax** for method `ach`; **App** for manual methods | Overwritten from Stax on disagreement, for `ach` only |
| **`initiated_on`, `settled_on`, `returned_at`, `return_code`, `return_reason`, `provider_created_at`** | **Stax** for method `ach` | |
| `is_refund`, `parent_payment_id` | Stax for `ach`; App for manual | |
| `reference`, `note` | App | `note` is mandatory for manual methods |
| `recorded_by_id` | System | The staff member who recorded a manual payment |
| `raw_summary` | Stax | A trimmed summary. Never any payment-method detail |

### external_refs, provider_snapshots
| Field | Owner | Note |
|---|---|---|
| `entity_type`, `entity_id` | App | Which of our records this ID belongs to |
| `provider`, `external_kind`, `external_id` | Stax | The ID itself |
| `superseded_at` | App | Set when a record is re-created on the provider side |
| `payload`, `fetched_at` | Stax | Trimmed copy of what Stax returned, with the time it was read |

### deposits
| Field | Owner | Note |
|---|---|---|
| all fields except `id`, `created_at` | Stax | Read-only here |

### webhook_events, sync_state
| Field | Owner | Note |
|---|---|---|
| `received_at`, `event_type`, `provider_object_id`, `dedupe_hash` | Stax | As received. The event body is not trusted as data; the object is fetched again by ID |
| `processed_at`, `outcome`, `error` | System | |
| `job_name`, `last_success_at`, `cursor` | System | |

### documents
| Field | Owner | Note |
|---|---|---|
| `id`, `uploaded_at` | System | |
| `store_id` / `lease_id` / `operator_id` | App | Exactly one. The record the file belongs to; an operator holds **dispute evidence only** (PLAN-012) |
| `kind`, `note` | App | `note` is guarded free text |
| `original_filename`, `content_type`, `size_bytes`, `sha256` | System | Name sanitised; type read from the content; the digest is an identifier, held to 64 hex characters |
| `blob_path` | System | Where the bytes live in the private blob container; never a filename; the file itself is **never** in the database |
| `source` | System | `upload`; `docusign` reserved |
| `uploaded_by_id`, `deleted_at`, `deleted_by_id`, `delete_note` | System / App | Soft delete by an admin with a note; the blob stays |

Files are subject to Rule 1 (ADR-0008): PDF and DOCX text is checked at upload; images and
scans are covered by the stated policy, not by a check.

### contact_entries, contact_corrections (PLAN-011b, PLAN-012)
| Field | Owner | Note |
|---|---|---|
| `kind`, `happened_on`, `body`, `promised_date`, `promised_amount`, `lease_id` | App | Guarded free text; the author may change them for 15 minutes, then corrections are appended (`contact_corrections`, never edited) |
| `status` | App / System | `open`, `kept`, `rescheduled`, `broken`; `kept` is written by the system when a matching payment settles (`kept_by_payment_id`) or by staff recording the previous promise's outcome |
| `deleted_at`, `deleted_by_id`, `delete_reason` | App | Admin soft delete with a reason; the row stays and counts for nothing |

### invoice_drafts, invoice_draft_lines (PLAN-012)
| Field | Owner | Note |
|---|---|---|
| all fields | App | A draft is ours until **Send** turns it into one invoice and one one-off charge per line; `line_total` is quantity times unit amount to the cent, enforced by a CHECK; `sent_invoice_id` links the invoice; a cancelled draft stays |

### invoice_line_items — `one_off_charge_id` (PLAN-012)
| Field | Owner | Note |
|---|---|---|
| `one_off_charge_id` | App | The charge a builder line became; the allocation key for a payment on a multi-line invoice (lines in order). A `kind` column is **S1**, the owner's decision (BACKLOG §2) |

### disputes, dispute_evidence (PLAN-012)
| Field | Owner | Note |
|---|---|---|
| `status`, `reason`, `amount`, `respond_by`, `provider_created_at`, `changed_at`, `raw_summary`, `fetched_at` | **Stax** | Read by the worker, stored verbatim (status and reason are the provider's words); never edited here. `raw_summary` holds the documented fields and the transaction **masked**; the provider's dispute ID is in `external_refs` (`dispute`) |
| `payment_id`, `operator_id`, `store_id` | System | Derived at sync from the disputed transaction's external ref; empty when the transaction is not ours |
| `dispute_evidence.name`, `document_id`, `status`, `sent_at`, `error` | App / System | The file is a `documents` row of the operator (kind `dispute_evidence`); `status` queued/sent/failed; `error` a class name only; the provider's file ID in `external_refs` (`dispute_file`) |

### outbound_messages (reserved, no UI)
| Field | Owner | Note |
|---|---|---|
| all fields | App | Empty until Phase 7 items (a) and (b). A DocuSign envelope ID will live in `external_refs`, not here |

### audit_log, legacy_records, import_audit
| Field | Owner | Note |
|---|---|---|
| `audit_log`, all fields | System | Append-only |
| `legacy_records`, all fields | Import | The sheet rows as they were |
| `import_audit`, all fields except `needs_review` | Import | Which cell became which row, by which rule |
| `import_audit.needs_review` | App | |

## 6. Owner decisions (Checkpoint 4, 2026-09-30, in writing)

The owner read this document line by line and decided:

1. **Invoice number format is `INV-YYYY-NNNN`, and old numbers carry forward.** The schema
   accepts this format (four or more sequence digits, so the counter can pass 9999). The
   Rents tab holds no invoice numbers, so the source of the old numbers is still to be
   named by the owner before Prompt 13 (the import); until then no number is invented.
2. **Invoice total mismatch:** collection on that charge stops until staff resolve it.
   Confirmed.
3. **A Stax-method payment that Stax does not know:** flagged, never deleted. Confirmed.

**Card guard tested on staging (2026-09-30, by Fable at the owner's request; no screen for
the new tables exists yet, so a browser test was not possible).** Both attempts to save the
public test number into an operator note were refused and the table stayed empty:

| Path | Refusal, verbatim |
|---|---|
| Application layer, `Operator(notes=...).save()` | `This text may not contain card or account numbers.` |
| Direct database write, raw SQL `INSERT` bypassing the application | `This text may not contain card or account numbers.` followed by `CONTEXT:  PL/pgSQL function finance_reject_payment_data() line 27 at RAISE` |

Neither message contains the number. Rows before and after: 0. Owner decision: the new
tables join the admin panel in Prompts 6 and 7 as planned, not earlier.

**Two notes for the record (owner, 2026-09-30):**
- When an operator's email differs between Stax and us, **receipts still go to the address
  stored in Stax.** The mismatch flag must tell staff to fix it in Stax. (Added to §3.)
- Bank routing and account patterns are guarded in the application only. **A database-level
  guard is added to the Prompt 14 security scope.**

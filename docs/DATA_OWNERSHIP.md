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
| A pending payment older than the configured number of days with no news | Flagged as a mismatch and alerted (Prompt 10). |
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
| **`sent_at`, `viewed_at`, `paid_at`** | **Stax** | Overwritten from Stax on disagreement |
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
| charge and invoice references, `kind`, `policy_year` | App | Linking a payment to a charge is our decision |
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

### documents, outbound_messages (reserved, no UI)
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

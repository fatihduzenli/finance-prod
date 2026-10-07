## QA Report — PLAN-012b: the invoice builder's Item combo box

**Date:** 2026-10-07 · **QA owner:** Fable · **Implementer:** Opus
**Origin:** owner's click 1 of Prompt 12 on staging (the "Fee type / Free text" column).
**Plan:** `docs/qa/PLAN-012b-builder-item-combo.md` · ships with PLAN-012a (`d94ea1c`) in one push.

**Scope.** `backend/ledger` (models: `InvoiceDraftLine.item`, `description` optional,
`InvoiceLineItem.details`; migration `0032_builder_item`; `invoice_builder` resolving the
item by name, saving catalogue items, the item in the charge description and the detail in
the note; `InvoiceLineForm` with the datalist and the Save tick; the draft form, `_invoice`
partial and `invoice_lines.js`), `backend/payments/one_off_invoices.py` (the provider line's
`details`), tests. **Unchanged:** `payments/provider.py`, `payments/stax/`, every other builder
behaviour, `infra/`, workflows, `.env.example`. No new dependency or environment variable.

**Commands run (final code).** Env as in QA-012; PostgreSQL 16 in a throwaway container.

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .`, `uv run ruff format --check .` | n/a | 0 | clean |
| `uv run mypy` (strict) | n/a | 0 | no issues in 349 source files |
| `uv lock --check` | n/a | 0 | unchanged |
| `manage.py makemigrations --check --dry-run` | sqlite, PostgreSQL 16 | 0 | No changes detected |
| `uv run pytest -m "not e2e" -q -o faulthandler_timeout=300` | sqlite | 0 | 4,576 passed, 576 skipped (4,539 before) |
| same | PostgreSQL 16 | 0 | 5,149 passed, 3 skipped (5,102 before) |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 7 passed |
| `bash scripts/check-no-secrets.sh`; pattern scan of the diff and new files | n/a | 0 | clean |
| `TODO-VERIFY` comments all have a register row | n/a | 0 | yes |
| Bicep build | n/a | — | not applicable: `infra/` untouched |

Baseline before PLAN-012b (QA-012a): PostgreSQL 5,102 passed; sqlite 4,539 passed.

**Tests (Gate 2).** Opus: 15 new functions in `test_invoice_builder.py` (a catalogue name in
any case resolves; a typed item with a detail is category `other`, its Stax text the name and
`details` the detail; the fallback line keeps `details`; Save creates an audited, offered item
with slug code, price and sort order, offered by the next form; one item for one name on two
lines; a lease refuses the same saved name twice; slug collisions `-2`, `-3`, reserved codes,
unicode and the 64-character cut; Save ignored on an offered name; refused on an inactive or
NSF name on create and on edit with nothing written; Save on edit; 200-character item accepted,
201 refused, 120-character detail accepted; one datalist, `list` on every row, no "Free text";
an empty item with an amount refused on the page; preview and print escape item and detail),
a seeded 0032 round trip on both engines, the guard-trigger registry, the draft-line factory.
`test_plan012_adversarial.py` (PLAN-012) stayed green unedited.

Fable's adversarial file (`test_plan012b_adversarial.py`, 11 cases): the NSF fee type names
never link however they are typed (lower, upper, title, padded) and cannot be saved over, with
the catalogue count unchanged; an inactive catalogue name is a typed `other` line and cannot be
saved over; a viewer cannot create a catalogue item through the builder (403 on the route, the
service refuses, no row, no audit row); a card number, a blank and an invisible character
cannot become an item or a catalogue entry and the number is in no audit row; two concurrent
saves of one new name leave exactly one catalogue item (PostgreSQL, two threads); the
provider receives the item's text and the detail for both a catalogue and a typed line, the
line items and charges carry them, the catalogue line is `setup` and the typed one `other`,
and preview and print escape a `<script>` item and an `&` detail; the Stax body's line items
carry `details` and none of our form field names; no builder template, form or script offers
"Free text".

No test weakened, skipped or deleted. Coverage is not measured in this repository.

**Findings (Gates 3 and 4).**

| # | Severity | Where | Finding | Outcome |
|---|---|---|---|---|
| 1 | MINOR | migration 0032 backfill | A migrated line that has a fee type keeps its old description, so a line whose description was the fee type's own name now shows that name twice (item and detail) | **Deferred by written decision (Fable, 2026-10-07):** only pre-existing draft lines are affected, all of them staging test data from the last day; production has none; staff can clear the detail on edit. No migration change. |

Design choices accepted as built: reserved codes (a saved item never takes an NSF or
category code, so "Damage" becomes `damage-2` and bills as `setup` like every catalogue
item, per plan D4); the same typed name on several lines links to the one new item, and a
lease refuses two of them at save time; Send resolves the stored lines by name again (a
fee type made inactive meanwhile turns the line into a typed `other` line instead of a
refusal); `update_draft` takes the operator's row lock after the draft's (same order as
Send); the catalogue insert runs in a savepoint and reuses a concurrent winner, five tries,
then a clean refusal; two fee types with one name prefer the offered one; the detail is
appended to the charge note uncut (the note is unbounded, the detail at most 120
characters); the charge description still cuts at 120 while the Stax line text carries the
full item.

Gate 4 notes: the item, detail and memo go through the payment-data guard and the
PostgreSQL triggers (0032 covers both new columns); the catalogue write is billing-only,
audited, slug-unique under the operator lock; the datalist is plain HTML (no new script
source); the pages escape; nothing new reaches the provider but the line `details`, a
field the mapping already sent; no new route; no new secret; `.env.example` unchanged.

**Confidentiality gate (§5.4).** Secret scan clean. No secret-store read by either model. No
new credential-capable file type. No new secret name.

**Residual risk.**
- The datalist and the Save-tick toggling have no browser test; the e2e shells pass but none
  opens the builder. The owner's click on staging is the browser check.
- Name resolution could link a typed line to a catalogue item staff did not mean when names
  coincide; the list shows the match while typing and the preview shows the linked item's
  category (plan §7).
- Finding 1 above, deferred.

**Verdict: PASS** (pending the owner's staging click of plan §8: a two-line draft on store
2476 with one catalogue item and one typed item with a detail, sent, and a saved item offered
in the next draft).

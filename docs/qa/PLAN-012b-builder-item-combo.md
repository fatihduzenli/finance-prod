# PLAN-012b — Invoice builder lines: an Item combo box instead of "Fee type / Free text"

**Date:** 2026-10-07 · **Origin:** owner's click 1 of Prompt 12 on staging, in writing:
*"the 'Fee type' column must work like Stax's item field: a combo box where staff either pick a
catalogue item or type a new item name on the spot. 'Free text' as a dropdown value goes away.
Columns become: Item (combo: catalogue or typed), Description (optional detail), Quantity,
Unit amount. A typed item name becomes the Stax line item text; the description, if any, is
appended as the line detail. Add an optional 'Save as catalogue item' tick on a typed item
(admin and billing). Fix with the next push, keep the rest of the builder as is."*
**Lead/QA:** Fable · **Implementer:** Opus · **Ships with:** PLAN-012a (`d94ea1c`), one push.

## 1. Restated objective

Each builder line has an **Item**: staff type into one box that offers the catalogue's names
and accepts any new name; a name that matches an offered catalogue item (exactly, ignoring
case and surrounding spaces) *is* that item, anything else is a typed item. **Description**
becomes an optional detail under the item. The Stax line carries the item name as its text
and the description as its `details`; the charge's description is the item name (with the
quantity suffix as today). A typed item may be ticked **Save as catalogue item**, which
creates the catalogue entry when the draft is saved or sent, so the next draft offers it.
Nothing else in the builder changes: lines, totals, preview, print, Send, Charge now, cancel,
allocation, 410 on the old form.

## 2. Affected packages

`backend/ledger` (model and one migration, builder service, line form and formset, the draft
form template and `invoice_lines.js`, preview and print templates), `backend/payments/one_off_invoices.py`
(the provider line's `details`), tests. **Out of scope:** the catalogue admin, `payments/stax/`,
`payments/provider.py` (`LineItem.details` already exists), every other builder behaviour,
`infra/`, workflows.

## 3. Decisions

- **D1 — the Item column.** `InvoiceDraftLine.item` (guarded char, `NAME_MAX_LENGTH`, not
  blank, CHECK): the line's text. `fee_type` stays (nullable): set when `item` names an
  offered catalogue item (`invoice_builder.offered_fee_types()`: active, never the NSF
  codes), else `None`. `description` becomes an optional detail (blank allowed; the
  `_not_blank` CHECK on it is dropped). Resolution is server-side, by name, case- and
  space-insensitive; the form never sends a fee type id. Two offered items with the same
  name would be ambiguous: the catalogue has no such rule today, so the resolver takes the
  first in catalogue order and the plan records it (the admin keeps names distinct).
- **D2 — the combo box.** A text input with a `<datalist>` of the offered names (one list
  per form, referenced by every row including rows the JS adds). No new JavaScript library;
  `invoice_lines.js` keeps the `list` attribute when it clones a row. The column header is
  **Item**; the placeholder reads "Catalogue item or a new name". **No "Free text" option
  exists anywhere.**
- **D3 — what Stax and the charge see.** `InvoiceLineItem.details` (free text, blank allowed)
  holds the line's description; `InvoiceLineItem.description` holds the item name. The
  provider line is `LineItem(description=item, quantity, unit_price, details=description)`
  (the fallback for a non-multiplying total keeps its rule, with the detail still in
  `details`). The one-off charge's `description` is `charge_description(line)` built from
  the item (quantity suffix as today); the line's detail is appended to the charge's
  internal `note` after the existing sentence ("Detail: …"), cut to fit.
- **D4 — Save as catalogue item.** A per-line checkbox `save_item`, shown and honoured only
  when the item is typed (not an offered name). On Save draft, Edit and Send, before the
  lines are written and inside the same transaction: a `FeeType` is created with `name` =
  the item, `code` = a slug of the name (ASCII, at most 64; a numeric suffix `-2`, `-3` … on
  collision; under the operator's row lock already held), `default_price` = the line's unit
  amount, `default_cost = None`, `refundable = False`, `active = True`, `sort_order` = the
  current maximum + 1; audited `fee_type.created` with the draft named; the line then
  links `fee_type` to it. If an **inactive** or NSF fee type already has that name, the
  save is refused for that line with "A catalogue item with this name exists but is not
  offered; an admin can change it in the catalogue." and nothing is written. Billing and
  admin may tick it (the builder is billing-only already); the box is never ticked by
  default. Note for the owner: a catalogue item's charges are category `setup` (that is
  what the catalogue has meant since 10b); a typed item without the tick stays `other`.
- **D5 — migration 0032.** Add `item` (backfill: the fee type's name when the line has one,
  else the old description), set `description` to `""` on lines without a fee type (their
  text moved to `item`), add the CHECKs, drop the old one; add `invoice_line_items.details`
  (backfill `""`). Reverse: `description` restored from `item` where blank, then the
  columns dropped. Seeded round-trip test as in 0031.

## 4. Interface contract

- `LineInput(item: str, description: str, quantity, unit_amount, save_item: bool = False)`;
  `fee_type` leaves the input (resolved from `item`). `CleanLine` gains `item`, `fee_type`
  resolved, `save_item`.
- `InvoiceDraftLine.item`; `InvoiceLineItem.details`.
- Refusals: `LINE_ITEM_REQUIRED` ("Line {n}: an item is required."), `LINE_ITEM_TOO_LONG`,
  `LINE_ITEM_PAYMENT_DATA`, `LINE_SAVE_NOT_OFFERED` (D4), the existing description and
  amount rules. `LINE_DESCRIPTION_REQUIRED` and `LINE_FEE_TYPE` go away.
- Templates: header **Item | Description | Quantity | Unit amount | Save as catalogue item |
  Remove**; preview and print show the item on the line and the detail beneath it.
- No new environment variable, dependency or provider change.

## 5. Work breakdown (one batch, Opus)

| Task | Files | Done when |
|---|---|---|
| Model, migration 0032, round trip | `ledger/models.py`, `ledger/migrations/0032_*.py`, `tests/test_migrations_roundtrip.py` | forward and back on empty and seeded databases, both engines |
| Service: resolve by name, save catalogue item, item in charge description and note | `ledger/services/invoice_builder.py` | §6 service cases |
| Form, formset, template, datalist, JS, preview, print | `ledger/forms.py`, `ledger/views/invoices.py`, `templates/ledger/invoices/{_draft_form,draft,preview,print}.html`, `static/ledger/invoice_lines.js` | the page has no "Free text" string; a cloned row keeps the datalist; the e2e shell still passes |
| Provider line details | `payments/one_off_invoices.py` | the single create carries `details` per line |
| Tests | `tests/test_invoice_builder.py` (and the files above) | §6 |

## 6. Test plan (before implementation)

Happy: pick a catalogue name → the line links the fee type, the charge is its category, the
Stax line text is the name; type a new name with a detail → no fee type, charge `other`, Stax
line text the name and `details` the detail; tick Save → a `FeeType` exists with the slug
code, the line links it, the next form's datalist offers it, audit row present; the same name
typed twice in one draft with Save ticked on both → one catalogue item. Boundaries: name
matching ignores case and surrounding spaces; a name of 200 characters accepted, 201 refused;
a detail of 120 accepted; a slug collision takes `-2`; an empty item with a unit amount →
"an item is required"; a blank row still ignored. Error paths: Save ticked on an offered name
→ ignored, nothing created; Save on a name held by an inactive or NSF fee type → refused with
the sentence and no draft written; a card number in the item or detail → refused. Preview
and print: item and detail rendered, no "Free text". Adversarial (Fable): the NSF fee type
names never resolve to their fee type however they are typed; a viewer cannot create a
catalogue item through the builder (403, no row); two concurrent saves of the same new name
leave one catalogue item (unique code) or one clean refusal; the provider request carries
`details` and no fee type id; a typed item name holding `<script>` is escaped on preview and
print and reaches Stax as text; a migrated free-text line keeps its text as the item and the
old charges and totals are untouched.

## 7. Risk register

- The migration moves text between two columns; the round trip proves it on seeded rows.
- Creating catalogue rows from the builder is a new write path to `fee_types`; it is
  billing-only, audited, and slug-unique under the operator lock.
- Name resolution could link a typed line to a catalogue item staff did not mean when names
  coincide; the datalist shows the match while typing, and the preview shows the linked
  item's category. Recorded.
- Irreversible: none.

## 8. Acceptance criteria

Gate green on both engines; the "Free text" string absent from the builder's templates and
forms; the owner's click on staging: a two-line draft on store 2476 with one catalogue item
and one typed item with a detail, previewed, sent → one Stax invoice with two lines (text =
item names, the typed line's detail shown) and two charges; a typed item saved to the
catalogue appears in the next draft's list.

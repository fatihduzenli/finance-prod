# Monvera — mockup handoff

For whoever draws the next page mockups (the owner in a design tool, a designer, or a tool fed
this file). It says what the app already looks like, which values are fixed, what a mockup must
contain, and how it is checked. The source of truth for the look is `docs/design/DESIGN-SPEC.md`
(owner-approved 2026-10-22, dark theme 2026-10-27); this file is the short, practical version of it
for drawing new pages. Values below are copied from `backend/finance/static/finance/tokens.css`,
the only file with literal colours.

## 1. What to deliver

| Item | Rule |
|---|---|
| File | `docs/design/<page>-dark-laptop.png` (dark theme, laptop) — the primary mockup; add `<page>-light-laptop.png` only when light differs in layout; `<page>-dark-phone.png` for the phone when the page's phone layout is not the default one-column stack |
| Laptop frame | 1440 × (as tall as needed), the app shell included: sidebar 232 px, content column up to 1240 px wide with 28 px / 32 px padding |
| Phone frame | 390 × (as tall as needed), the top bar (menu, logo, search) and the bottom tab bar (Dashboard, Stores, Monthly rent, More) included |
| Theme | Dark by default; both themes share every size, only the tokens change (§3). System is not offered (Light · Dark only, owner 2026-11-05) |
| Content | Realistic test data, never real customer data: stores "#101 0101 MAPLE AVENUE", operators "Northwind Stores", amounts with cents ("$1,066.67"), dates as "Nov 15, 2026" |
| Copy | Final wording. The built page uses the mockup's words verbatim; every departure is recorded in QA |

Existing references in `docs/design/`: `dashboard`, `store-workspace`, `monthly-rent-overview`
(each with a `-dark-laptop-built.png` beside the mockup showing what was built), `login-dark-laptop`,
`new-lease-dark-laptop`, `reports-dark-laptop`, plus the 2026-10-22 light set (operator workspace,
transfer wizard, stores list, calendar, create invoice, charge detail, profit and loss, daily charges,
help, states, add payment, store workspace phone).

**Start from the built page, not from scratch.** Every page already has a screenshot in
`backend/tests/visual/baseline/` named `<page>-1280.png`, `<page>-1280-dark.png`, `<page>-390.png`,
`<page>-390-dark.png` (1280 px laptop, 390 px phone). Pages captured: dashboard, stores, operators,
operator, operator-form, grid (monthly rent), leases, new-lease, new-store, invoice-builder,
search-new-invoice, record-payment, add-payment, payments, payment-detail, void-payment,
charge-detail, lease-end-statement, calendar, reports, aging, pnl, daily-charges, settings,
message-templates, outbox, bank-deposits, help, help-topic, login, sign-in-required, not-found,
store-log-contact, operator-log-contact, and the store tabs store-charges / -invoices / -payments /
-schedules / -documents / -contacts.

## 2. The shell (fixed)

- **Sidebar** (laptop): 232 px, dark navy `#0B2545` in light (a navy-to-teal gradient), `#171721`
  in dark; logo top; items Dashboard, Stores, Operators, Monthly rent, Reports (with its sub-items
  when open), Admin (collapsible); bottom: Appearance (Light · Dark), the user card
  (name, role, Preferences · Help · Sign out).
- **Top of the content**: the sandbox banner "SANDBOX — TEST SYSTEM, NO REAL MONEY" (amber,
  full width) on the sandbox; then the page header bar (64 px min, surface background, bottom
  border): `h1` 20 px/600 (a store's title 24 px/700 with its status badge), the page's actions on
  the right (pill buttons), tabs under it on workspace pages.
- **Phone** (≤ 768 px): one column; the sidebar becomes a top bar (menu button, logo, search
  button) and a bottom tab bar with four items; the content keeps the laptop's DOM order; a long
  form's primary button sits in a fixed save bar above the tab bar.
- **No drop shadows** anywhere; borders carry the hierarchy (`--shadow-none`). Dark theme cards
  have no border (the surface colour does the work).

## 3. Tokens

### Core palette

| Token | Light | Dark | Use |
|---|---|---|---|
| `--bg` | #F4F6F9 | #171721 | page background |
| `--surface` | #FFFFFF | #1E1E2A | cards, header bar, inputs |
| `--surface-2` | #F9FAFC | #272735 | table headers, tinted rows |
| `--border` / `--border-soft` | #E3E8EF / #EEF1F5 | #2E2E3D / #26263A | card and row rules |
| `--input-border` | #D5DCE6 | #70707D | inputs, pill buttons |
| `--text` / `--text-2` | #0B2545 / #3D4B60 | #EDEDF3 / #C3C3CC | body / secondary |
| `--muted` | #5F6E85 | #A3A3B3 | labels, sub-lines, descriptions |
| `--faint` | #9AA7B8 | #70707D | decoration only, never text |
| `--link` | #0B7C74 | #2EC4A6 | links, teal text buttons, step numbers, icons in tinted squares |
| `--navy` / `--teal` / `--green` | #0B2545 / #13A89E / #2EC4A6 | same | brand; `--teal` is the logo's "vera" and the active tab's border |
| `--btn-primary-bg` | #0E8F86 (border #0B7C74) | #0E8F86 (border #0A6B64) | the one primary button per page, white 600 label |
| `--btn-danger-bg` | #B42318 | same | the confirming button of a destructive action |
| `--btn-dark-bg` | #0B2545 | #2E2E3D | the dark pill button (filters' Apply, pagination's current page) |
| `--month-current` | #EEF6F5 | rgba(46,196,166,.08) | tinted squares: icons, initials, the grid's current month |
| `--sandbox-bg` / `--sandbox-text` | #FFF3C4 / #6B4E00 | #3A3420 / #F0D68A | the sandbox banner and pill |
| `--backdrop` | rgba(11,37,69,.45) | rgba(0,0,0,.6) | behind dialogs |

### Status colours (badges, figures, grid cells)

| State | Light bg / text | Dark bg / text |
|---|---|---|
| paid | #E3F5E8 / #176B33 | #1F3A2C / #7FDCA4 |
| unpaid (also "overdue" text and red figures) | #FDE8E6 / #B42318 | #3A2226 / #F2A3A8 |
| overdue (solid chip, e.g. "Overdue 45 days") | #B42318 / #FFFFFF | #5A1D24 / #FFD6D9 |
| pending (amber: promises, "3 unmatched", warnings) | #FFF3C4 / #6B4E00 | #3A3420 / #F0D68A |
| partial | #FFE8D6 / #8A3B00 | #3D2A1A / #F5B98A |
| waived, neutral (grey: "Vacant · 23 days") | #E8ECF2 / #3D4B60 | #2E2E3D / #C3C3CC |

Flash messages (toasts) use the same four pairs as success / warning / error / info. Charts:
income #5B6BE8, expenses #C9CEDD, net #0E8F86 (dark: #7C8CF8, #3E4460, #2EC4A6); calendar dots:
rent teal, settles green, promise amber #B7791F, lease end red.

### Type, radii, spacing

- **Font**: Inter (self-hosted; no other font). Sizes: xs 11, sm 12, base 13, md 14 (body),
  lg 15 (card titles, h2), h1 20, store title 24, KPI figure 28; weights 400 / 500 / 600 / 700.
  Section labels: 13 px, 600, uppercase, letter-spacing .04em, muted ("SUMMARY", "COLLECTIONS").
- **Radii**: card 12 px, controls and inputs 8 px, pills (buttons, badges, segmented controls) 99 px.
- **Spacing scale**: 4, 8, 12, 16, 20, 24, 32 px. Card padding 18 px / 20 px; card gap 20 px;
  two-column workspace grid 2fr / 1fr with a 20 px gap.

## 4. Components (draw these, not new ones)

| Component | Look |
|---|---|
| Card | surface, 12 px radius, 1 px border (light only), 18/20 px padding; optional header row with a section label left and a teal "… ›" link right |
| Section label | uppercase muted 13/600 (see §3) |
| Buttons | pill; secondary = surface with input border; primary = teal fill, white 600 label, one per page; dark = navy fill; danger = red fill; small variant `btn-sm`; teal text buttons ("+ New operator", "Copy from operator", "+ Add item", "Change", "Upload") as plain link-coloured text |
| Inputs | 8 px radius (pill-shaped in forms since 12f: 99 px on single-line inputs), 40 px tall, label above in 13/600; "optional" as a muted hint under the label; errors in `--error-text` under the field |
| Select | native select, same frame as inputs |
| Checkbox / radio | native; the **switch** (toggle) is a 40×22 track with a knob, teal when on |
| Segmented control | pill track on `--bg`, segments 12 px/500 muted, the chosen one on surface with a 1 px ring and 600 weight (filters, "Later · Use #1991's account · Enter a new account") |
| Badge | pill, 12/600, status colours, with an icon or dot ("✓ Active", "○ Vacant · never leased", "● $13,950.00 overdue") |
| KPI card | label muted, figure 28/700, note line; four across on the dashboard |
| Report card | icon in a 36 px tinted square + title 15/600 on one row; muted description; figure 22/700 with its sub-line left and "Open ›" right on the bottom row |
| Numbered step card | a 22 px teal circle with the number, title 15/600, a muted subtitle; used by the New lease page |
| Summary card (sticky) | "SUMMARY" label, label/value rows (labels muted), a rule, a bold total row, the primary button full width, "Cancel" under it, a muted help line |
| Table | header row on `--surface-2` in muted 12/600, 1 px row rules, numbers right-aligned with tabular figures, the row's primary cell bold |
| Dialog | centred, surface, 12 px radius, `h2` title with a × close; `sm` 440 px, `lg` 640 px; a bottom sheet on phones; buttons in a right-aligned footer |
| Toast | top of the content, status colours, an icon, dismiss × |
| Pagination | "1–3 of 3" left; ‹ Previous · [1] · Next › pills right |
| Tabs | underline tabs on workspace pages; the active tab's underline is `--teal` |
| Empty state | one muted sentence ("No notes yet.", "No documents yet.") inside the card |

**Icons** (SVG, stroke, 16–20 px; the set is pinned by a test — a new icon is a code change, ask
first): dashboard, stores, operators, monthly-rent, reports, admin, search, menu, more, plus, file,
image, calendar, user, check, circle, circle-half, dash, chevron-down, chevron-right, arrow-right,
x, banknote, pin, trash, alert, dot, info, inbox, lock, bell, handshake, phone, mail, map-pin.
Emoji are not used.

## 5. Content rules a mockup must respect

- **Money**: always with cents and a dollar sign ("$1,214,550.00"); red only for unpaid/overdue
  amounts; green for a positive margin or "Paid up".
- **Dates**: "Nov 15, 2026"; ranges "Nov 15–30"; ordinals "1st", "15th"; months by name in
  sub-lines ("collected in October"); never ISO dates in page text.
- **The payment provider's name** comes from the settings. Write it as `{provider}` in any
  sentence ("Paid outside {provider}") — the built page never carries the literal name.
- **No card or bank numbers** anywhere, not even as sample data (the app refuses them). A bank
  shows as "Chase ····4821"; a card never appears.
- **The sandbox pill** text is fixed: "SANDBOX — TEST SYSTEM, NO REAL MONEY" (phone: "SANDBOX —
  TEST SYSTEM").
- **One primary button per page**; destructive actions confirm in a dialog.
- **Every control has a label**; placeholder text is not a label (a visually hidden label is fine).
- **Contrast**: text on its background at 4.5:1 (3:1 for large text and UI); every text/background
  pair in the tokens has a documented pair line and a test. A new colour is a token first.

## 6. Page families (where a new page should sit)

Per `DESIGN-SPEC.md` §4: **workspace** (store, operator: header with tabs, 2fr/1fr grid, side
cards LEASE · NOTES · NEXT · STORE COST · DOCUMENTS), **list** (stores, operators, leases,
invoices, payments: search + filter card, table, pagination, "Download spreadsheet"), **form /
builder** (create invoice, add payment, new lease: cards of fields with a sticky summary or
total on the right), **detail** (charge, invoice, payment), **report** (P&L, aging, revenue:
range controls, table, totals, download), **wizard** (transfer, end lease: numbered steps in one
frame), **sign-in** (the two-panel page with the mosaic), **states** (empty lists, flashes,
errors), **help**. Pick the family, reuse its frame, and only draw what differs.

## 7. How a mockup becomes a page

1. Drop the PNG(s) in `docs/design/` with the names in §1 and say in one line what changed and
   where the page is reached from.
2. Fable writes the plan (contract, tests, risks) and Opus builds it to the mockup; any
   departure (a field the model lacks, a word the provider rule forbids) is stated in the QA
   record and the PR.
3. The gate regenerates the page's screenshots in both themes at 1280 and 390 px and Fable
   compares them to the mockup by eye; the visual suite then guards the page pixel by pixel.
4. A `-built.png` copy beside the mockup records what shipped when it differs.

## 8. Checklist before handing a mockup over

- [ ] Dark laptop frame at 1440 px with the shell; phone frame when the layout is not the default stack
- [ ] Only tokens from §3; no shadows; 12 / 8 / 99 px radii
- [ ] Components from §4; icons from the pinned set
- [ ] Final copy; `{provider}` instead of the provider's name; no card or bank numbers; money with cents; dates as "Nov 15, 2026"
- [ ] One primary button; every control labelled; empty state drawn
- [ ] The page's family named and what differs from the built screenshot listed

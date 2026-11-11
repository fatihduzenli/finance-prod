# PLAN-015 Batch 6 — admin, help and states in the dashboard's language (owner, 2026-11-10)

Fable's plan for Batch 6 of `docs/qa/PLAN-015-design-v2.md` (§2: "Admin pages' cards, help, the empty states and flashes; the sign-in page stays"). References: `docs/design/dashboard-v2-dark-laptop.png` and the built dashboard; `docs/design/daily-charges-laptop.png`, `help-laptop.png`, `states-laptop.png` (2026-10-24) and DESIGN-SPEC §4 States for what stays. Standing approval of 2026-11-10. Gate class: **small change + the full visual comparison** and Fable's by-eye check in both themes and widths; the full suites on the PR's CI.

## 1. Objective

The Admin pages (Daily charges, Activity log, Messages from the processor, Nightly check, Settings, Message templates and its editor), Preferences and Help take the dashboard's header; Daily charges' figures become KPI cards v2; every admin card opens with the card head; statuses shown as plain text become chips; the two hand-laid filter forms lose their inline styles for the filter card's classes; the "too slow" notice becomes a styled notice. Toasts, empty states, the error pages and the sign-in family stay as the approved States design. Templates and CSS only; no query, route or model.

## 2. Packages and scope

`backend/` only: `ledger/templates/ledger/run/{today,_operations}.html`, `ledger/templates/ledger/audit/page.html`, `ledger/templates/ledger/webhooks/{list,detail}.html`, `ledger/templates/ledger/reconciliation/page.html`, `ledger/templates/ledger/settings/{index,templates,template_edit}.html`, `finance/templates/finance/settings.html`, `finance/templates/finance/help/{index,topic,_topics}.html`, the "too slow" notice on the pages that carry it (`audit`, `dashboard`, `grid`, `arrears`, `aging`, `pnl`, `reports/index`, `deposits`, `revenue`, `payments_by_method`, `statement`), `finance/static/finance/app.css`, tests, visual baselines (+ four new visual pages). **Out of scope:** the sign-in family (`accounts/templates/two_factor/*`, `accounts/401.html`, `mfa_required`, `signed_out_everywhere`), the 404/403/form-failed pages and `_error_card.html`, the toasts, `_empty_state.html` and the plain table empty rows (tests require them plain), the Django admin, the run strip's logic, the filter forms' fields and behaviour, the `{% filters %}` tag (not introduced here), any help content.

## 3. Interface contract

### 3.1 Headers
Every page above takes `div.page-header > div.page-title-block (h1 [+ p.page-sub]) + div.page-actions`. Daily charges keeps its "Admin ›" crumb (text, no link, as the test pins) above the `h1` and moves the date to `p.page-sub#run-date` (class `page-sub` replacing `page-date`); Nightly check moves its first muted intro sentence (l.6) to `p.page-sub` (the second, `#run-now-note`, stays by the button); the Activity log, Messages, Settings, Message templates, Preferences and Help have no sub-line unless one sentence already sits directly under the `h1`. No breadcrumb is added where the tests forbid one (`NO_PARENT`). Actions stay in `page-actions`, the primary last.

### 3.2 Daily charges
`#run-kpis` becomes `section.kpi-row.kpi2-row#run-kpis` of four `{% kpi2 %}` cards: Created today (icon `check`, sub = today's note "4 today · 1 catch-up"), Due (`calendar`), Skipped (`alert`, value tone `pending`… — `kpi2` has no pending value tone: use the badge "n skipped" pending tone with a muted value? No: give `kpi2` a `pending` value tone as it gained `paid`/`muted`, with its AA pair test), Large debits (`alert`, `unpaid` when > 0); ids kept on the values (`value_id`). The run strip stays. Cards: `#due-today-card`'s `.card-header.run-due-header` becomes `.card-head` (the `h2#due-today-title`, the segmented `#due-filter`, `#export-csv` right); the other cards (`#skipped-today-card`, `#recent-runs-card`, `#last-run-card`, `#autopay-charges-card`, `#insurance-due-card`, and `_operations.html`'s five) open with `div.card-head > h2` (ids kept). Statuses stay badges (pinned); `span.badge.run-created` stays.

### 3.3 Activity log and Messages from the processor
- The hand-laid filter forms keep their fields, ids and buttons but drop every inline `style=` for the filter card's classes (`.filter-card`, `.filter-field`, a `.filter-actions` row with Show and the export link); no `{% filters %}`.
- The pagers take the list footer's shape: `div.pager.list-footer[role=navigation][aria-label=Pages]` > `span.pager-count` ("Page n" as today) + `span.pager-pages` with `a.btn.btn-sm[rel=prev|next]` / `span.btn.btn-sm.pager-off` (Newer / Older, Previous / Next as today's words).
- Messages list: the Outcome cell becomes `{% chip %}` with `data-role="status"`: processed/ok → paid, ignored/duplicate → neutral, failed/error → unpaid, pending/queued → pending (Opus maps every outcome value the view can produce and tests each); the words unchanged (`event.outcome_words`).
- Messages detail: `#webhook-event-card` opens with `div.card-head > h2.section-label` "Event"; `#payload-card`'s `h2` inside a card head.
- The Activity log's too-slow notice: §3.6.

### 3.4 Nightly check
`#last-run-card`, `#findings-card` (its `.card-header` → `.card-head`, the export link right) and `#recent-runs-card` open with card heads; the last run's status text becomes `{% chip %}` (done → paid, running → pending, failed → unpaid; the words unchanged, `id="last-status"` kept on the chip's text); the findings table's state column, if it is text, becomes a chip the same way.

### 3.5 Settings, Message templates, Preferences, Help
- Settings: the five `section.card.settings-card`'s `.card-header` → `.card-head` (ids and order kept — `test_plan015c_settings.py:99` slices by id); the `#templates-card` paragraph unchanged.
- Message templates: Active / Inactive text → `{% chip %}` (paid / neutral) in `td[data-role=status]`; "Internal" stays a badge; the editor's `#template-preview` `.card-header` → `.card-head`; `#template-card` gains a card head "Template".
- Preferences: `#appearance-card` and `#calendar-feed-card` open with card heads (ids on the `h2`s kept).
- Help: the header only (§3.1); the topics card and the article keep their classes (`.help-title` pinned).

### 3.6 The "too slow" notice
Every `<p class="warning" id="…-too-slow" role="status">` becomes `<p class="side-note warning" id="…" role="status">` (the existing styled notice; ids and text unchanged). No new CSS.

### 3.7 CSS
Additions only: `.kpi2-value.tone-pending` and its pair; `.filter-actions`; the card-head margin where the admin pages' `.card-header` rule applied; removal of `.page-date` and `.run-kpis` / the hand-written `.kpi*` rules only when unused after this batch (grep; the old `kpi` tag itself was handled in Batch 5).

## 4. Work breakdown (one Opus session; one commit per task)
1. **Headers and the too-slow notice** (§3.1, §3.6); re-pin `test_run_layout.py:88` (`page-date#run-date` → `page-sub`), `test_audit_page.py:417` (the notice's class), `test_design_components.py::PAGE_HEADER`/`LIST_PAGES` if the regex needs the new shape, `test_help_layout.py` ("<h1>Help</h1>" kept), `test_plan012f_d_pages.py::NO_PARENT` (unchanged); new test: each of the nine pages' header shape, and the notice's class on every page that renders it.
2. **Daily charges** (§3.2); re-pin `test_run_layout.py` `kpi()` reads and the tone strings, the CSS test (:390 — `.kpi-value.tone-pending` → `.kpi2-value.tone-pending`), `test_dashboard_v2.py:815` (ids unchanged); new tests: the four cards' ids and tones; the card heads.
3. **Activity log, Messages, Nightly check** (§3.3, §3.4); re-pin `test_audit_page.py:271`, `test_plan012f_components.py:192` and `test_responsive.py:224` (the table-scroll string unchanged), `test_webhooks.py` (:523, :915 unchanged; the outcome cell), `test_reconciliation.py` (`id="last-status"` on the chip text), `test_design_components.py::test_every_paged_list_uses_the_pager` (the `Page ` count kept); new tests: every outcome and status chip; the filter card has no inline style; the pager shape.
4. **Settings, templates, Preferences** (§3.5); re-pin `test_plan015c_settings.py:99`, `test_messaging_engine.py:324`, `test_plan015_batch0.py:351`, e2e `test_messaging_14_d.py` selectors (ids kept); new tests: the heads and the template status chips.
5. **Visual**: regenerate `daily-charges`, `settings`, `message-templates`, `help`, `help-topic` (+ `not-found`, `login`, `sign-in-required`, `dashboard` must be byte-identical); add `activity-log` (`/audit/`, admin), `webhooks` (`/webhooks/`), `nightly-check` (`/reconciliation/`), `preferences` (`/settings/`) to `test_pages.py` with new baselines; `visual.sh "daily-charges or settings or message-templates or help or not-found or login or sign-in-required or dashboard or activity-log or webhooks or nightly-check or preferences"`; list every changed PNG.
6. **Self-review and checks** (the brief).

## 5. Test plan (Fable verifies)
Happy paths per §4. Boundaries: Daily charges with nothing due (the zero cards muted; the strip "none"); an Activity log over the time budget (the notice); a webhook event with an unknown outcome word (neutral chip, no crash); the Nightly check with no run yet (`#no-run`); Message templates all active. Adversarial: a toast with `<script>` (unchanged tests); a 403 on /audit/ for billing; no provider literal in templates (the processor's name through `company.provider_name`). Accessibility: card heads' `h2`s with their ids; chips' glyphs; the notice's `role=status`; the AA pair of the pending tone. The one-line-help rule green on every changed template (the Nightly check's two muted lines: one moves to the sub-line).

## 6. Risks
- Many exact strings pinned (`test_run_layout`, `test_states`, `test_help_layout`): re-pin, never loosen; the empty rows stay plain.
- The admin pages were "byte-identical" guards in Batch 2: that rule ends here by design.
- The sign-in family and the error pages must not change: their baselines are the proof.
- No behaviour change in the filter forms (fields, names, buttons unchanged).

## 7. Acceptance criteria
1. The nine pages' headers share the dashboard's shape; Daily charges shows four `kpi2` cards with ids and tones.
2. Every admin card opens with a card head; the outcome, last-run and template statuses are chips with `data-role="status"`.
3. The two filter forms carry no inline style; both pagers are `pager list-footer`.
4. Every too-slow notice is `p.side-note.warning` with its id.
5. The sign-in, error and dashboard baselines byte-identical; the named pages' baselines changed; the four new pages added; Fable's by-eye check recorded.
6. Small gate green locally; the full suites green in CI.

## 8. Told to the owner / candidates
- The Activity log and Messages filters keep their own forms (no as-you-type); converting them to the list filter card is a behaviour change for a later note.
- Help content and the "Tip" box of DESIGN-SPEC §4 Help are untouched (no tip is used today).

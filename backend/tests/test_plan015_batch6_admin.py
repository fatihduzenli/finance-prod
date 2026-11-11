"""PLAN-015 Batch 6 (docs/qa/15/PLAN-015-batch-6-admin.md): the Admin pages, Preferences and
Help in the dashboard's language — the dashboard's header (§3.1), the "too slow" notice styled
(§3.6), Daily charges' figures as KPI cards v2 and its cards' heads (§3.2), the Activity log's
and Messages' filter cards and pagers, the outcome and last-run chips (§3.3, §3.4), the
Settings, Message templates and Preferences cards' heads and the template status chips (§3.5)."""

from __future__ import annotations

import re
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from django.template import Context, Template
from django.test import Client, override_settings
from django.utils import timezone

from accounts.roles import Role
from finance.templatetags.components import CHIP_GLYPHS
from ledger.views import audit as audit_views
from ledger.views import dashboard as dashboard_view
from ledger.views import reports as report_views
from ledger.views import webhooks as webhook_views
from payments.models import (
    ReconciliationRun,
    ReconciliationStatus,
    ReconciliationTrigger,
    WebhookEvent,
)
from tests import test_run_layout as run_layout
from tests.receivables_helpers import an_operator
from tests.test_operator_views import client_for
from tests.test_rent_run import health  # noqa: F401 (autouse fixture)

pytestmark = pytest.mark.django_db

BACKEND = Path(__file__).resolve().parents[1]
TEMPLATES = BACKEND / "ledger" / "templates" / "ledger"
FINANCE_TEMPLATES = BACKEND / "finance" / "templates" / "finance"


def page(client: Client, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code)
    return response.content.decode()


#: The page's own header (not the bar's .page-header-inner around it).
PAGE_HEADER = re.compile(r'<div class="page-header(?: detail-head)?">')


def header_of(html: str) -> str:
    found = PAGE_HEADER.search(html)
    assert found is not None
    return html[found.start() : html.index("</header>", found.start())]


def content_of(html: str) -> str:
    """The page after its header (the sidebar and the header left out)."""
    found = PAGE_HEADER.search(html)
    assert found is not None
    return html[html.index("</header>", found.start()) :]


def admin() -> Client:
    return client_for(Role.ADMIN)[0]


def rendered_icon(name: str) -> str:
    return Template("{% load icons %}{% icon name 16 %}").render(Context({"name": name}))


def rendered_chip_glyph(tone: str) -> str:
    """The glyph {% chip %} leads a chip of the tone with (finance.templatetags.components)."""
    return Template("{% load icons %}{% icon name 12 %}").render(
        Context({"name": CHIP_GLYPHS[tone]})
    )


# --- §3.1 The headers -----------------------------------------------------------------------------

#: The nine pages and the title each header's title block opens with (after the breadcrumb on
#: Daily charges and Message templates).
HEADERS = [
    ("/run/", "Daily charges"),
    ("/audit/", "Activity log"),
    ("/webhooks/", "Messages from Stax"),
    ("/reconciliation/", "Nightly check"),
    ("/admin-settings/", "Settings"),
    ("/admin-settings/templates/", "Message templates"),
    ("/settings/", "Preferences"),
    ("/help/", "Help"),
    ("/help/collecting-an-invoice/", "Help"),
]


@pytest.mark.parametrize(("path", "title"), HEADERS)
def test_each_page_has_the_dashboards_title_block(path: str, title: str) -> None:
    header = header_of(page(admin(), path))
    assert re.match(
        r'<div class="page-header(?: detail-head)?"><div class="page-title-block">'
        rf"(?:<nav [^>]*>.*?</nav>)?<h1>{re.escape(title)}</h1>",
        header,
        re.S,
    ), header[:400]
    assert header.count("<h1") == 1


def test_the_pages_without_a_sub_line_have_none() -> None:
    for path, _ in HEADERS:
        if path in ("/run/", "/reconciliation/"):
            continue
        assert '<p class="page-sub"' not in header_of(page(admin(), path)), path


def test_daily_charges_keeps_the_admin_crumb_and_the_date_as_the_sub_line() -> None:
    header = header_of(page(admin(), "/run/"))
    assert header.startswith(
        '<div class="page-header"><div class="page-title-block">'
        '<nav class="breadcrumb" aria-label="Breadcrumb" id="breadcrumb"><span>Admin</span> '
        '<span aria-hidden="true">›</span></nav><h1>Daily charges</h1>'
        '<p class="page-sub" id="run-date">'
    ), header[:400]
    assert 'class="page-date"' not in header
    # The actions follow the title block, the primary last.
    actions = header[header.index('<div class="page-actions">') :]
    assert actions.index('id="match-invoices"') < actions.index('id="create-charges"')


def test_the_nightly_checks_intro_is_its_sub_line_and_the_run_note_stays_by_the_button() -> None:
    html = page(admin(), "/reconciliation/")
    header = header_of(html)
    assert (
        '<h1>Nightly check</h1><p class="page-sub" id="reconciliation-sub">Each night the check'
        " re-reads open invoices, recent transactions, bank deposits and ACH rejections from"
        " Stax, and marks pending payments that are too old."
    ) in header
    assert "Identifiers are masked.</p></div>" in header
    content = content_of(html)
    assert "Each night the check" not in content
    assert '<p id="run-now-note"><span class="muted">Runs the nightly check now.' in content


# --- §3.6 The "too slow" notice -------------------------------------------------------------------

#: The pages that carry the notice, with its id.
TOO_SLOW_PAGES = [
    ("/audit/", "audit-too-slow"),
    ("/", "dashboard-too-slow"),
    ("/grid/", "grid-too-slow"),
    ("/arrears/", "arrears-too-slow"),
    ("/reports/aging/", "aging-too-slow"),
    ("/reports/pnl/", "pnl-too-slow"),
    ("/reports/", "reports-too-slow"),
    ("/reports/deposits/", "report-too-slow"),
    ("/reports/revenue/", "report-too-slow"),
    ("/reports/payments-by-method/", "report-too-slow"),
]


@pytest.fixture
def too_slow(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(compute: Any) -> Any:
        raise report_views.ReportTooSlow

    for module in (audit_views, dashboard_view, report_views):
        monkeypatch.setattr(module, "bounded", refuse)


@pytest.mark.usefixtures("too_slow")
@pytest.mark.parametrize(("path", "element_id"), TOO_SLOW_PAGES)
def test_the_too_slow_notice_is_the_styled_side_note(path: str, element_id: str) -> None:
    html = page(admin(), path)
    notice = (
        f'<p class="side-note warning" id="{element_id}" role="status">{report_views.TOO_SLOW}</p>'
    )
    assert notice in html
    assert '<p class="warning"' not in html


@pytest.mark.usefixtures("too_slow")
def test_the_statement_too_slow_notice_is_the_styled_side_note() -> None:
    operator = an_operator("Slow Statement")
    html = page(admin(), f"/operators/{operator.pk}/statement/")
    notice = '<p class="side-note warning" id="report-too-slow" role="status">'
    assert f"{notice}{report_views.TOO_SLOW}</p>" in html


def test_no_template_keeps_the_plain_warning_paragraph() -> None:
    found = [
        path.relative_to(BACKEND).as_posix()
        for root in (TEMPLATES, FINANCE_TEMPLATES)
        for path in root.rglob("*.html")
        if '<p class="warning"' in path.read_text(encoding="utf-8")
    ]
    assert found == []


# --- §3.2 Daily charges ------------------------------------------------------------------------


@pytest.fixture
def run_day(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: run_layout.TODAY)


def kpi_cards(html: str) -> dict[str, tuple[str, str, str, str]]:
    """Daily charges' figure row: per figure id, (icon path, title, the figure's class, figure)."""
    row = html[
        html.index('<section class="kpi-row kpi2-row" id="run-kpis" aria-label="Figures">') :
    ]
    row = row[: row.index("</section>")]
    found = {}
    for card in row.split('<div class="kpi2">')[1:]:
        icon = re.search(r'<span class="kpi2-icon" aria-hidden="true">(.*?)</span>', card, re.S)
        title = re.search(r'<p class="kpi2-title">(.*?)</p>', card)
        value = re.search(r'<span class="(kpi2-value[^"]*)" id="([a-z-]+)">(.*?)</span>', card)
        assert icon and title and value, card
        found[value.group(2)] = (icon.group(1), title.group(1), value.group(1), value.group(3))
    return found


@pytest.mark.usefixtures("run_day")
@override_settings(LARGE_DEBIT_WARNING_AMOUNT=Decimal("2000.00"))
def test_daily_charges_four_figures_are_kpi2_cards_with_their_ids_icons_and_tones() -> None:
    billing = client_for(Role.BILLING)[0]
    run_layout.world(billing)
    cards = kpi_cards(page(billing, "/run/"))
    assert list(cards) == ["kpi-created", "kpi-due", "kpi-skipped", "kpi-large"]
    icons = {name: rendered_icon(name) for name in ("check", "calendar", "alert")}
    assert cards["kpi-created"] == (icons["check"], "Created today", "kpi2-value", "5")
    assert cards["kpi-due"] == (icons["calendar"], "Due today", "kpi2-value", "$7,000.00")
    assert cards["kpi-skipped"] == (icons["alert"], "Skipped", "kpi2-value tone-pending", "1")
    assert cards["kpi-large"] == (icons["alert"], "Large debits", "kpi2-value tone-unpaid", "1")
    assert 'class="kpi"' not in page(billing, "/run/")


@pytest.mark.usefixtures("run_day")
def test_with_nothing_due_every_daily_charges_figure_is_a_muted_zero() -> None:
    cards = kpi_cards(page(client_for(Role.BILLING)[0], "/run/"))
    assert [(card[2], card[3]) for card in cards.values()] == [
        ("kpi2-value tone-muted", "0"),
        ("kpi2-value tone-muted", "$0.00"),
        ("kpi2-value tone-muted", "0"),
        ("kpi2-value tone-muted", "0"),
    ]


def opens_with_card_head(html: str, section_id: str) -> str:
    """The section's first element: the card head (its markup up to the head's end)."""
    found = re.search(
        rf'<section [^>]*id="{section_id}"[^>]*>\s*<div class="card-head[^"]*">', html
    )
    assert found is not None, section_id
    return html[found.end() : html.index("</div>", found.end())]


@pytest.mark.usefixtures("run_day")
@override_settings(LARGE_DEBIT_WARNING_AMOUNT=Decimal("2000.00"))
def test_every_daily_charges_card_opens_with_the_card_head() -> None:
    billing = client_for(Role.BILLING)[0]
    run_layout.world(billing)
    html = page(billing, "/run/")
    heads = {
        "due-today-card": '<h2 id="due-today-title">Due today</h2>',
        "skipped-today-card": '<h2 id="skipped-today-title">Skipped today</h2>',
        "recent-runs-card": '<h2 id="recent-runs-title">Recent runs</h2>',
        "last-run-card": "<h2>Created by the last run</h2>",
        "autopay-charges-card": "<h2>Repeating debits created today</h2>",
        "insurance-due-card": "<h2>Insurance due in the next ",
        "today-run": "<h2>Today's run</h2>",
        "failed-attempts": '<h2 id="failed-heading">Failed payment attempts: ',
        "not-sent-card": '<h2 id="not-sent">Invoices created but not sent: ',
        "last-reconciliation-card": "<h2>Last nightly check</h2>",
    }
    for section_id, title in heads.items():
        assert opens_with_card_head(html, section_id).startswith(title), section_id
    due_head = html[html.index('<div class="card-head run-due-header">') :]
    due_head = due_head[: due_head.index("</a></div>")]
    assert due_head.index('id="due-today-title"') < due_head.index('id="due-filter"')
    assert due_head.index('id="due-filter"') < due_head.index('id="export-csv"')
    assert '<div class="card-header' not in html[html.index('id="run-kpis"') :]


# --- §3.3 The Activity log and Messages from the processor -------------------------------------


def an_event(outcome: str, event_type: str = "update_invoice") -> WebhookEvent:
    return WebhookEvent.objects.create(
        event_type=event_type,
        provider_object_id=f"object-{uuid.uuid4().hex[:8]}",
        dedupe_hash=uuid.uuid4().hex,
        outcome=outcome,
    )


def outcome_cell(html: str, event: WebhookEvent) -> str:
    row = html[html.index(f'href="/webhooks/{event.pk}/"') :]
    row = row[: row.index("</tr>")]
    found = re.search(r'<td data-role="status"[^>]*>(.*?)</td>', row, re.S)
    assert found is not None, row
    return found.group(1)


#: Every outcome the processor stores (payments.webhooks: received on arrival, processed with or
#: without its note, ignored with its reason, failed, dead_letter after the third failure, an empty
#: one), an unknown word, and the chip each shows with the words of WebhookEvent.outcome_words.
OUTCOMES = [
    ("received", "pending", "Received"),
    ("processed", "paid", "Processed"),
    ("processed: unlinked (2 candidates)", "paid", "Processed: unlinked (2 candidates)"),
    ("ignored", "neutral", "Ignored"),
    ("ignored: not matched", "neutral", "Ignored: not matched"),
    ("failed", "unpaid", "Failed"),
    ("dead_letter", "unpaid", "Gave up"),
    ("something_new: x", "neutral", "something_new: x"),
]


@pytest.mark.parametrize(("outcome", "tone", "words"), OUTCOMES)
def test_each_message_outcome_is_a_chip_in_its_tone(outcome: str, tone: str, words: str) -> None:
    event = an_event(outcome)
    cell = outcome_cell(page(client_for(Role.VIEWER)[0], "/webhooks/"), event)
    assert cell.startswith(f'<span class="status-chip status-chip-{tone}">'), cell
    assert cell.endswith(f"<span>{words}</span></span>"), cell


def test_a_message_without_an_outcome_shows_a_dash_and_no_chip() -> None:
    event = an_event("")
    assert outcome_cell(page(client_for(Role.VIEWER)[0], "/webhooks/"), event) == "—"


def test_an_outcome_word_is_escaped_in_its_chip() -> None:
    event = an_event("<b>odd</b>: <i>x</i>")
    cell = outcome_cell(page(client_for(Role.VIEWER)[0], "/webhooks/"), event)
    assert "<b>" not in cell and "&lt;b&gt;odd&lt;/b&gt;: &lt;i&gt;x&lt;/i&gt;" in cell
    assert cell.startswith('<span class="status-chip status-chip-neutral">')


def filter_card(html: str) -> str:
    start = html.index('<div class="card filter-card">')
    return html[start : html.index("</form></div>", start)]


@pytest.mark.parametrize("path", ["/audit/", "/webhooks/"])
def test_the_two_filter_forms_carry_no_inline_style(path: str) -> None:
    card = filter_card(page(admin(), path))
    assert "style=" not in card
    assert card.count('<span class="filter-field">') == (6 if path == "/audit/" else 2)
    actions = card[card.index('<span class="filter-actions">') :]
    assert '<button class="btn" type="submit">' in actions and "Download spreadsheet</a>" in actions


def test_the_activity_log_filter_keeps_its_fields_and_buttons() -> None:
    card = filter_card(page(admin(), "/audit/"))
    for name in ("action", "user", "entity_type", "entity_id", "from", "to"):
        assert f'name="{name}"' in card, name
    assert '<button class="btn" type="submit">Show</button>' in card
    assert 'id="audit-export"' in card


#: The list footer's shape (ledger/_list_footer.html) with the page's own words.
FOOTER = re.compile(
    r'<div class="pager list-footer" role="navigation" aria-label="Pages">\s*'
    r'<span class="pager-count">(.*?)</span>\s*<span class="pager-pages">(.*?)</span>\s*</div>',
    re.S,
)


def test_the_activity_log_pager_is_the_list_footer_inside_the_table_card() -> None:
    html = page(admin(), "/audit/")
    card = html[html.index('<div class="card table-card">') :]
    found = FOOTER.search(card)
    assert found is not None
    assert found.group(1) == "Page 1"
    assert found.group(2) == (
        '<span class="btn btn-sm pager-off" aria-disabled="true">Newer</span>'
        '<span class="btn btn-sm pager-off" aria-disabled="true">Older</span>'
    )
    assert card.index("</table>") < found.start()


def test_the_messages_pager_is_the_list_footer_with_previous_and_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(webhook_views, "PAGE_SIZE", 1)
    for _ in range(3):
        an_event("processed")
    client = client_for(Role.VIEWER)[0]
    found = FOOTER.search(page(client, "/webhooks/?page=2"))
    assert found is not None
    assert found.group(1) == "Page 2 of 3 (3 messages)"
    assert found.group(2) == (
        '<a class="btn btn-sm" rel="prev" href="?page=1">Previous</a>'
        '<a class="btn btn-sm" rel="next" href="?page=3">Next</a>'
    )
    first = FOOTER.search(page(client, "/webhooks/?outcome=processed"))
    assert first is not None
    assert first.group(2) == (
        '<span class="btn btn-sm pager-off" aria-disabled="true">Previous</span>'
        '<a class="btn btn-sm" rel="next" href="?outcome=processed&amp;page=2">Next</a>'
    )


def test_one_page_of_messages_draws_no_pager() -> None:
    an_event("processed")
    assert 'class="pager' not in page(client_for(Role.VIEWER)[0], "/webhooks/")


def test_the_message_detail_cards_open_with_the_card_head() -> None:
    event = an_event("processed")
    html = page(client_for(Role.VIEWER)[0], f"/webhooks/{event.pk}/")
    assert opens_with_card_head(html, "webhook-event-card") == (
        '<h2 class="section-label">Event</h2>'
    )
    assert opens_with_card_head(html, "payload-card") == "<h2>Content (without account data)</h2>"


# --- §3.4 The Nightly check -----------------------------------------------------------------------


def a_run(status: str) -> ReconciliationRun:
    return ReconciliationRun.objects.create(
        trigger=ReconciliationTrigger.SCHEDULED, status=status, started_at=timezone.now()
    )


def last_status(html: str) -> str:
    return html.split('<dd id="last-status">', 1)[1].split("</dd>", 1)[0]


@pytest.mark.parametrize(
    ("status", "tone", "words"),
    [
        (ReconciliationStatus.DONE, "paid", "Done"),
        (ReconciliationStatus.RUNNING, "pending", "Running"),
        (ReconciliationStatus.FAILED, "unpaid", "Failed"),
    ],
)
def test_the_last_runs_status_is_a_chip_in_its_tone(status: str, tone: str, words: str) -> None:
    a_run(status)
    shown = last_status(page(client_for(Role.BILLING)[0], "/reconciliation/"))
    assert shown == (
        f'<span class="status-chip status-chip-{tone}">{rendered_chip_glyph(tone)}'
        f"<span>{words}</span></span>"
    )


def test_before_any_run_the_nightly_check_says_so_in_its_card() -> None:
    html = page(client_for(Role.BILLING)[0], "/reconciliation/")
    card = html[html.index('id="last-run-card"') :]
    card = card[: card.index("</section>")]
    assert '<p class="muted empty" id="no-run">No nightly check has run yet.</p>' in card
    assert 'id="last-status"' not in card


def test_the_nightly_check_cards_open_with_the_card_head() -> None:
    a_run(ReconciliationStatus.DONE)
    html = page(client_for(Role.BILLING)[0], "/reconciliation/")
    assert opens_with_card_head(html, "last-run-card") == "<h2>Last run</h2>"
    assert opens_with_card_head(html, "recent-runs-card") == "<h2>Recent runs</h2>"
    findings = opens_with_card_head(html, "findings-card")
    assert findings == (
        '<h2>Findings</h2><a class="btn card-link" id="export-csv"'
        ' href="/reconciliation/export.csv">Download spreadsheet</a>'
    )
    assert 'class="card-header' not in content_of(html)

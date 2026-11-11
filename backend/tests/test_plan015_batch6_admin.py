"""PLAN-015 Batch 6 (docs/qa/15/PLAN-015-batch-6-admin.md): the Admin pages, Preferences and
Help in the dashboard's language — the dashboard's header (§3.1), the "too slow" notice styled
(§3.6), Daily charges' figures as KPI cards v2 and its cards' heads (§3.2), the Activity log's
and Messages' filter cards and pagers, the outcome and last-run chips (§3.3, §3.4), the
Settings, Message templates and Preferences cards' heads and the template status chips (§3.5)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from django.test import Client

from accounts.roles import Role
from ledger.views import audit as audit_views
from ledger.views import dashboard as dashboard_view
from ledger.views import reports as report_views
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

"""The phone layout (docs/qa/PLAN-011-dashboard-reports-mobile.md D9): the
``responsive_table`` tag, its use on every list, detail and report page, the style sheet's
breakpoints and touch targets, the "Menu" toggle, and the sign-in pages on the same style
sheet. The browser side (a 390 px viewport) is in ``tests/e2e/test_mobile.py``.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from django.conf import settings
from django.template import Context, Template, TemplateSyntaxError
from django.test import Client

import documents_storage
from accounts.roles import Role
from documents_storage.memory import MemoryDocumentStorage
from finance.templatetags.responsive import headers, responsive
from tests.auth_helpers import make_user, verified_client
from tests.test_ledger_models import new_lease, saved

pytestmark = pytest.mark.django_db

BACKEND = Path(settings.BASE_DIR)
TEMPLATES = BACKEND / "ledger" / "templates" / "ledger"
APP_CSS = BACKEND / "finance" / "static" / "finance" / "app.css"
#: Tables that keep their own sideways scroll region (the grid builds 12,000 cells in
#: Python; labelling them would cost the page's 2 s and the grid is never shown as cards).
OWN_SCROLL = {"grid/page.html"}
SCROLL_TEMPLATES = {"audit/page.html"} | {
    f"reports/{path.name}" for path in (TEMPLATES / "reports").glob("*.html")
}


def client_for(role: Role) -> Client:
    user, device = make_user(f"layout-{uuid.uuid4().hex[:8]}", role)
    assert device is not None
    return verified_client(user, device)


def render(markup: str, **context: object) -> str:
    return Template("{% load responsive %}" + markup).render(Context(context))


# --- The tag --------------------------------------------------------------------------------


def test_each_cell_is_labelled_with_its_column_header() -> None:
    out = render(
        "{% responsive_table %}<table id='t'><thead><tr><th>Store</th>"
        "<th>Amount <small>(USD)</small></th></tr></thead>"
        "<tbody><tr><td>A</td><td>1.00</td></tr><tr><td>B</td><td>2.00</td></tr></tbody></table>"
        "{% endresponsive_table %}"
    )
    assert out.startswith("<div class=\"table-cards\"><table id='t'>")
    assert out.count('<td data-label="Store">') == 2
    assert out.count('<td data-label="Amount (USD)">') == 2


def test_colspan_headers_and_cells_are_counted() -> None:
    table = (
        '<table><thead><tr><th colspan="2">Who</th><th>Total</th></tr></thead>'
        '<tbody><tr><td>a</td><td>b</td><td>c</td></tr><tr><td colspan="3">No rows.</td></tr>'
        '<tr><th scope="row">Sum</th><td colspan="1">x</td><td>y</td></tr></tbody></table>'
    )
    assert headers(table) == ["Who", "Who", "Total"]
    out = responsive(table)
    assert (
        '<td data-label="Who">a</td><td data-label="Who">b</td><td data-label="Total">c</td>' in out
    )
    assert '<td colspan="3">No rows.</td>' in out
    # The row header takes the first column; its cells take the next ones.
    assert '<td colspan="1" data-label="Who">x</td><td data-label="Total">y</td>' in out


def test_a_cell_keeps_its_own_label_and_an_empty_header_gives_none() -> None:
    out = responsive(
        "<table><thead><tr><th></th><th>Name</th></tr></thead>"
        '<tbody><tr><td><input type="radio"></td><td data-label="Own">n</td></tr></tbody></table>'
    )
    assert '<td><input type="radio"></td>' in out
    assert '<td data-label="Own">n</td>' in out
    assert out.count("data-label") == 1


def test_header_text_is_escaped_once_in_the_label() -> None:
    out = responsive(
        '<table><thead><tr><th>Fees &amp; "costs" &lt;net&gt;</th></tr></thead>'
        "<tbody><tr><td>1</td></tr></tbody></table>"
    )
    assert '<td data-label="Fees &amp; &quot;costs&quot; &lt;net&gt;">1</td>' in out


def test_a_cell_value_never_becomes_a_label() -> None:
    hostile = '"><script>alert(1)</script>'
    out = render(
        "{% responsive_table %}<table><thead><tr><th>Note</th></tr></thead>"
        "<tbody><tr><td>{{ v }}</td></tr></tbody></table>{% endresponsive_table %}",
        v=hostile,
    )
    assert hostile not in out
    assert '<td data-label="Note">&quot;&gt;&lt;script&gt;' in out


def test_a_table_without_a_head_is_left_as_it_is() -> None:
    table = "<table><tbody><tr><td>1</td></tr></tbody></table>"
    assert responsive(table) == table


def test_scroll_mode_wraps_the_table_in_a_sideways_scroll_region() -> None:
    out = render(
        '{% responsive_table "scroll" %}<table id="r"><thead><tr><th>Month</th></tr></thead>'
        "<tbody><tr><td>2026-08</td></tr></tbody></table>{% endresponsive_table %}"
    )
    assert out == (
        '<div class="table-scroll" tabindex="0"><table id="r"><thead><tr><th>Month</th></tr>'
        '</thead><tbody><tr><td data-label="Month">2026-08</td></tr></tbody></table></div>'
    )


@pytest.mark.parametrize("arguments", ['"stack"', '"cards" "scroll"'])
def test_an_unknown_mode_is_a_template_error(arguments: str) -> None:
    with pytest.raises(TemplateSyntaxError):
        Template(
            "{% load responsive %}"
            f"{{% responsive_table {arguments} %}}{{% endresponsive_table %}}"
        )


# --- Every table uses it ------------------------------------------------------------------------


def _table_templates() -> list[Path]:
    return sorted(path for path in TEMPLATES.rglob("*.html") if "<table" in path.read_text())


def test_every_table_in_the_ledger_templates_is_responsive() -> None:
    found = _table_templates()
    assert len(found) >= 25
    for path in found:
        relative = path.relative_to(TEMPLATES).as_posix()
        text = path.read_text()
        if relative in OWN_SCROLL:
            assert 'class="grid-scroll"' in text
            continue
        assert text.count("<table") == text.count("{% responsive_table"), relative
        assert text.count("</table>") == text.count("{% endresponsive_table %}"), relative
        # The library may be loaded with others ({% load money responsive %}, PLAN-012c D9).
        assert re.search(r"\{% load [^%]*\bresponsive\b[^%]*%\}", text), relative
        mode = (
            '{% responsive_table "scroll" %}'
            if relative in SCROLL_TEMPLATES
            else "{% responsive_table %}"
        )
        assert text.count(mode) == text.count("<table"), relative


@pytest.mark.parametrize(
    "path",
    [
        "/operators/",
        "/stores/",
        "/leases/",
        "/run/",
        "/payments/",
        "/returns/",
        "/webhooks/",
        "/arrears/",
    ],
)
def test_the_lists_render_as_cards(path: str) -> None:
    page = client_for(Role.VIEWER).get(path).content.decode()
    assert '<div class="table-cards">' in page, path
    assert 'data-label="' in page or "<tbody>" in page, path


def test_the_reconciliation_page_renders_as_cards() -> None:
    page = client_for(Role.BILLING).get("/reconciliation/").content.decode()
    assert re.search(r'<div class="table-cards">\s*<table id="mismatches">', page)


@pytest.mark.parametrize(
    "path",
    [
        "/reports/aging/",
        "/reports/revenue/",
        "/reports/payments-by-method/",
        "/reports/deposits/",
        "/reports/pnl/",
    ],
)
def test_the_reports_scroll_sideways(path: str) -> None:
    page = client_for(Role.ADMIN).get(path).content.decode()
    assert '<div class="table-scroll" tabindex="0">' in page, path
    assert '<div class="table-cards">' not in page, path


def test_the_audit_page_scrolls_sideways() -> None:
    page = client_for(Role.ADMIN).get("/audit/").content.decode()
    assert re.search(r'<div class="table-scroll" tabindex="0">\s*<table id="audit-rows">', page)


def test_the_grid_keeps_its_own_scroll_region_with_a_sticky_first_column() -> None:
    page = client_for(Role.VIEWER).get("/grid/").content.decode()
    assert '<div class="grid-scroll" tabindex="0"' in page
    assert "table.grid th.store-col{position:sticky;left:0;" in page


def test_the_detail_pages_render_as_cards(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(documents_storage, "get_storage", MemoryDocumentStorage)
    lease = saved(new_lease())
    client = client_for(Role.ADMIN)
    for path in (
        f"/stores/{lease.store_id}/",
        f"/leases/{lease.pk}/",
        f"/operators/{lease.operator_id}/",
    ):
        page = client.get(path).content.decode()
        assert page.count('<div class="table-cards">') >= 2, path
    statement = client.get(f"/operators/{lease.operator_id}/statement/").content.decode()
    assert re.search(
        r'<div class="table-scroll" tabindex="0">\s*<table id="open-charges">', statement
    )
    store_page = client.get(f"/stores/{lease.store_id}/?tab=documents").content.decode()
    assert re.search(r'<div class="table-cards">\s*<table id="documents-list">', store_page)


# --- Style sheet and layout ----------------------------------------------------------------


def test_the_style_sheet_has_both_breakpoints_touch_targets_and_print() -> None:
    css = APP_CSS.read_text()
    assert "@media (max-width: 719.98px)" in css
    assert "@media (max-width: 1023.98px)" in css
    assert "--tap: 44px" in css
    assert css.count("min-height: var(--tap)") >= 4
    assert "@media print" in css
    assert ".table-cards > table td[data-label]::before { content: attr(data-label);" in css
    assert re.search(
        r"\.table-scroll > table th:first-child, \.table-scroll > table td:first-child "
        r"\{ position: sticky; left: 0;",
        css,
    )
    # No external asset.
    assert "url(" not in css and "@import" not in css


def test_the_menu_folds_behind_a_toggle_without_script() -> None:
    page = client_for(Role.VIEWER).get("/").content.decode()
    nav = page[page.index('<nav aria-label="Main">') : page.index("</nav>")]
    assert (
        '<input type="checkbox" id="nav-toggle" class="nav-toggle" aria-controls="nav-links">'
        in nav
    )
    assert '<label for="nav-toggle" class="nav-toggle-label" id="nav-menu">Menu</label>' in nav
    assert "<script" not in nav
    assert '<meta name="viewport" content="width=device-width, initial-scale=1.0">' in page
    assert '<link rel="stylesheet" href="/static/finance/app.css">' in page


def test_the_sign_in_page_uses_the_style_sheet_and_keeps_its_fields() -> None:
    page = Client().get("/account/login/").content.decode()
    assert '<link rel="stylesheet" href="/static/finance/app.css">' in page
    assert '<meta name="viewport" content="width=device-width, initial-scale=1.0">' in page
    assert 'name="auth-username"' in page and 'name="auth-password"' in page
    assert 'name="login_view-current_step"' in page

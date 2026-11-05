"""PLAN-015 Batch 0 (docs/qa/PLAN-015-design-v2.md §4.1, §4.4, §6): the sidebar's user card
and the Reports chevron of Design v2. The components themselves are pinned in
tests/test_design_components.py."""

from __future__ import annotations

import re

import pytest

from accounts.roles import Role
from tests.test_plan012f_shell import APP_CSS, body, client_for, element

pytestmark = pytest.mark.django_db


# --- The user card ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "label"), [(Role.VIEWER, "Viewer"), (Role.BILLING, "Billing"), (Role.ADMIN, "Admin")]
)
def test_the_sidebar_card_shows_the_tile_the_name_the_role_and_the_three_links(
    role: Role, label: str
) -> None:
    html = body(client_for(role, first_name="Dana", last_name="Whitfield").get("/"))
    card = element(html, "user-card", "</form></div>")
    assert card.index('class="avatar-tile" aria-hidden="true">DW</span>') < card.index(
        '<span class="user-name" id="user-menu-name">Dana Whitfield</span>'
    )
    assert card.index('id="user-menu-name"') < card.index(
        f'<span class="user-role" id="user-role">{label}</span>'
    )
    links = re.findall(r'id="(nav-preferences|nav-help|nav-sign-out)"', card)
    assert links == ["nav-preferences", "nav-help", "nav-sign-out"]
    assert card.index(f">{label}</span>") < card.index('id="nav-preferences"')
    assert "Property manager" not in html  # no title field (owner, §9.4): the role's display


def test_a_user_without_a_name_shows_the_username_and_its_initials() -> None:
    html = body(client_for(Role.VIEWER).get("/"))
    card = element(html, "user-card", "</form></div>")
    name = re.search(r'id="user-menu-name">([^<]+)</span>', card)
    assert name is not None and name.group(1).startswith("shell12f-")
    assert 'aria-hidden="true">SH</span>' in card  # "shell12f-…": one word, its first two letters


def test_the_user_card_tile_is_hidden_and_the_name_is_text_beside_it() -> None:
    html = body(client_for(Role.ADMIN, first_name="Élodie", last_name="O'Brien").get("/"))
    card = element(html, "user-card", "</form></div>")
    assert '<span class="avatar-tile" aria-hidden="true">EO</span>' in card
    assert "Élodie O&#x27;Brien</span>" in card


def test_the_more_sheet_shows_the_same_identity_without_the_sidebar_ids() -> None:
    html = body(client_for(Role.BILLING, first_name="Dana", last_name="Whitfield").get("/"))
    sheet = element(html, "more-sheet", "</details>")
    user = re.search(r'<li class="more-user">(.*?)</li>', sheet, re.S)
    assert user is not None
    assert user.group(1) == (
        '<div class="user-identity"><span class="avatar-tile" aria-hidden="true">DW</span>'
        '<div class="user-identity-text"><span class="user-name">Dana Whitfield</span>'
        '<span class="user-role">Billing</span></div></div>'
    )
    # Above Preferences, Help and Sign out, which keep their rows.
    assert sheet.index('class="more-user"') < sheet.index('href="/settings/"')
    assert html.count('id="user-menu-name"') == 1 and html.count('id="user-role"') == 1


def test_the_name_is_marked_on_preferences_and_help_only() -> None:
    client = client_for(Role.VIEWER, first_name="Dana")
    for path, marked in (("/settings/", True), ("/help/", True), ("/", False), ("/stores/", False)):
        card = element(body(client.get(path)), "user-card", "</form></div>")
        assert ('id="user-menu-name" aria-current="true"' in card) is marked, path


def test_the_card_links_take_the_sidebar_link_token() -> None:
    css = APP_CSS.read_text(encoding="utf-8")
    assert (
        ".user-meta a, .user-meta .link-button { color: var(--sidebar-link); "
        "text-decoration: none; }"
    ) in css
    assert ".user-role { font-size: var(--fs-sm);" in css
    assert ".user-name { display: block; color: var(--sidebar-active-text); font-size: " in css


# --- The Reports chevron ------------------------------------------------------------------------

REPORT_PAGES = ["/reports/", "/arrears/", "/reports/pnl/", "/payments/", "/calendar/", "/leases/"]
OTHER_PAGES = ["/", "/stores/", "/operators/", "/grid/", "/webhooks/", "/settings/", "/help/"]


def reports_group(html: str) -> str:
    sidebar = element(html, "sidebar", "</aside>")
    return sidebar[sidebar.index('id="nav-reports-menu"') : sidebar.index('id="nav-admin-menu"')]


@pytest.mark.parametrize("path", REPORT_PAGES + OTHER_PAGES)
def test_the_reports_group_is_open_on_report_pages_only(path: str) -> None:
    group = reports_group(body(client_for(Role.BILLING).get(path)))
    opened = '<details class="nav-disclosure" open>' in group
    assert opened is (path in REPORT_PAGES), path
    if not opened:
        assert '<details class="nav-disclosure">' in group


def test_the_reports_heading_stays_a_link_and_the_chevron_is_its_summary() -> None:
    group = reports_group(body(client_for(Role.VIEWER).get("/")))
    heading = re.search(
        r'<a class="nav-item nav-group-heading" href="/reports/" id="nav-reports">', group
    )
    assert heading is not None
    summary = re.search(
        r'<summary class="nav-group-toggle" id="nav-reports-toggle">(.*?)</summary>', group, re.S
    )
    assert summary is not None
    assert summary.group(1).startswith('<span class="visually-hidden">Reports pages</span>')
    assert '<svg class="icon icon-chevron-right" width="16" height="16"' in summary.group(1)
    assert group.index("</a>") < group.index("<details")  # the link is outside the summary
    assert group.index("</summary>") < group.index('<ul class="nav-sublist">')
    for label in ("Unpaid balances", "Profit and loss", "Payments", "Calendar", "Leases"):
        assert f">{label}</a>" in group, label


def test_admin_keeps_its_chevron() -> None:
    sidebar = element(body(client_for(Role.ADMIN).get("/")), "sidebar", "</aside>")
    admin = sidebar[sidebar.index('id="nav-admin-menu"') :]
    assert re.search(
        r'<summary class="nav-item nav-group-heading" id="nav-admin">.*?<span>Admin</span>'
        r'<svg class="icon icon-chevron-down"',
        admin,
        re.S,
    )


def test_the_chevron_turns_down_when_open_and_is_a_36px_target() -> None:
    css = APP_CSS.read_text(encoding="utf-8")
    assert (
        ".nav-disclosure[open] > .nav-group-toggle .icon-chevron-right "
        "{ transform: rotate(90deg); }"
    ) in css
    toggle = css.split("\n.nav-group-toggle {", 1)[1].split("}", 1)[0]
    assert "width: 36px; height: 40px;" in toggle and "position: absolute;" in toggle
    assert ".nav-group-split > .nav-item { padding-right: 44px; }" in css
    assert ".nav-group.is-open" not in css  # the class is gone with the <details>

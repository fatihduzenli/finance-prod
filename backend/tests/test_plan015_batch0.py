"""PLAN-015 Batch 0 (docs/qa/PLAN-015-design-v2.md §4.1, §4.4, §6): the sidebar's user card
and the Reports chevron of Design v2, and Appearance as Light and Dark only (accounts 0005 and
portal 0003 both ways, Dark by default and signed out, a posted ``system`` refused, the controls'
two segments). The components themselves are pinned in tests/test_design_components.py."""

from __future__ import annotations

import importlib
import re
import uuid
from collections.abc import Iterator

import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, RequestFactory

from accounts.models import Theme, UserPreference
from accounts.roles import Role
from accounts.themes import DEFAULT_THEME, THEMES, preference_for, set_theme, theme_for
from ledger.models import Operator
from tests.auth_helpers import make_user
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


# --- Appearance: Light and Dark only (§4.4) -----------------------------------------------------

ACCOUNTS_BEFORE = ("accounts", "0004_payment_status_period")
PORTAL_BEFORE = ("portal", "0002_portal_payments")


def _migrate(targets: list[tuple[str, str]]) -> None:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(targets)


@pytest.fixture
def leaves() -> Iterator[list[tuple[str, str]]]:
    found = MigrationExecutor(connection).loader.graph.leaf_nodes()
    yield found
    _migrate(found)


def _themes() -> tuple[list[tuple[str, str]], list[str]]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT u.username, p.theme FROM accounts_userpreference p "
            "JOIN auth_user u ON u.id = p.user_id ORDER BY u.username"
        )
        preferences = [(str(name), str(theme)) for name, theme in cursor.fetchall()]
        cursor.execute("SELECT theme FROM portal_users ORDER BY email")
        portal = [str(theme) for (theme,) in cursor.fetchall()]
    return preferences, portal


def _portal_check_accepts(theme: str) -> bool:
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("UPDATE portal_users SET theme = %s", [theme])
            raise _RolledBackError
    except IntegrityError:
        return False
    except _RolledBackError:
        return True


class _RolledBackError(Exception):
    """Undoes a probe write."""


@pytest.mark.django_db(transaction=True)
def test_the_theme_migrations_move_system_to_dark_and_go_back(
    leaves: list[tuple[str, str]],
) -> None:
    """accounts 0005 and portal 0003 on this run's engine (sqlite, and PostgreSQL when the suite
    runs on it): a stored ``system`` becomes ``dark``, Light and Dark stay; the portal's CHECK
    refuses ``system`` after and accepts it again after the reverse; the rows stay ``dark``."""
    _migrate([ACCOUNTS_BEFORE, PORTAL_BEFORE])
    old = MigrationExecutor(connection).loader.project_state([ACCOUNTS_BEFORE, PORTAL_BEFORE]).apps
    users = old.get_model("auth", "User")
    preference = old.get_model("accounts", "UserPreference")
    for name, theme in (("theme-a", "system"), ("theme-b", "light"), ("theme-c", "dark")):
        preference.objects.create(user=users.objects.create(username=name), theme=theme)
    operators = old.get_model("ledger", "Operator")
    portal_user = old.get_model("portal", "PortalUser")
    for index, theme in enumerate(("system", "light")):
        portal_user.objects.create(
            operator=operators.objects.create(display_name=f"Theme {index} LLC"),
            email=f"theme{index}@example.com",
            theme=theme,
        )
    assert _themes() == (
        [("theme-a", "system"), ("theme-b", "light"), ("theme-c", "dark")],
        ["system", "light"],
    )
    assert _portal_check_accepts("system")

    _migrate(leaves)
    assert _themes() == (
        [("theme-a", "dark"), ("theme-b", "light"), ("theme-c", "dark")],
        ["dark", "light"],
    )
    assert not _portal_check_accepts("system")
    assert _portal_check_accepts("dark") and _portal_check_accepts("light")

    _migrate([ACCOUNTS_BEFORE, PORTAL_BEFORE])  # the reverse: the choices come back, rows stay
    assert _themes() == (
        [("theme-a", "dark"), ("theme-b", "light"), ("theme-c", "dark")],
        ["dark", "light"],
    )
    assert _portal_check_accepts("system")
    _migrate(leaves)  # and forward again
    assert _themes()[1] == ["dark", "light"]


def test_the_migrations_alter_choices_and_defaults_and_the_portal_check_only() -> None:
    accounts = importlib.import_module("accounts.migrations.0005_theme_light_or_dark")
    portal = importlib.import_module("portal.migrations.0003_theme_light_or_dark")
    kinds = [type(step).__name__ for step in accounts.Migration.operations]
    assert kinds == ["RunPython", "AlterField"]
    kinds = [type(step).__name__ for step in portal.Migration.operations]
    assert kinds == ["RemoveConstraint", "RunPython", "AlterField", "AddConstraint"]
    for migration in (accounts.Migration, portal.Migration):
        field = next(s for s in migration.operations if type(s).__name__ == "AlterField").field
        assert field.choices == [("light", "Light"), ("dark", "Dark")] and field.default == "dark"
        assert all(
            step.reversible for step in migration.operations if type(step).__name__ == "RunPython"
        )
    assert not hasattr(portal, "GUARDED_COLUMNS")


@pytest.mark.django_db
def test_a_new_user_and_a_new_portal_user_default_to_dark() -> None:
    from tests.test_ledger_models import new_operator, saved
    from tests.test_portal import new_portal_user

    user = make_user(f"theme-{uuid.uuid4().hex[:8]}", Role.VIEWER)[0]
    assert theme_for(user) == "dark"
    assert UserPreference.objects.create(user=user).theme == Theme.DARK
    portal = saved(new_portal_user(operator=saved(new_operator()), email="dark@example.com"))
    portal.refresh_from_db()
    assert portal.theme == "dark"
    assert [value for value, _ in Theme.choices] == ["light", "dark"]
    assert THEMES == ("light", "dark") and DEFAULT_THEME == "dark"


@pytest.mark.django_db
@pytest.mark.parametrize("value", ["system", "System", " dark", "auto", ""])
def test_set_theme_refuses_system_with_the_existing_refusal(value: str) -> None:
    user = make_user(f"theme-{uuid.uuid4().hex[:8]}", Role.VIEWER)[0]
    with pytest.raises(ValueError, match="unknown theme"):
        set_theme(user, value)
    assert not UserPreference.objects.filter(user=user).exists()


@pytest.mark.django_db
def test_a_stored_value_no_longer_offered_reads_as_dark() -> None:
    """A row the migration did not reach (a value written around the model) is never rendered
    as ``system``: :func:`theme_for` and :func:`preference_for` read it as Dark."""
    client = client_for(Role.VIEWER)
    user = User.objects.get(pk=int(client.session["_auth_user_id"]))
    UserPreference.objects.create(user=user, theme="dark")
    UserPreference.objects.filter(user=user).update(theme="system")
    assert theme_for(user) == "dark"
    request = RequestFactory().get("/")
    request.user = user
    assert preference_for(request).theme == "dark"
    assert '<html lang="en" data-theme="dark">' in body(client.get("/"))


SIGNED_OUT_STAFF = ["/account/login/", "/stores/"]  # the sign-in page, "Sign in required" (401)


@pytest.mark.parametrize("path", SIGNED_OUT_STAFF)
def test_the_signed_out_staff_pages_are_dark(path: str) -> None:
    response = Client().get(path)
    assert response.status_code in (200, 401), path
    page = response.content.decode()
    assert '<html lang="en" data-theme="dark">' in page, path
    assert re.findall(r'<meta name="theme-color"[^>]*>', page) == [
        '<meta name="theme-color" content="#171721">'
    ]


@pytest.mark.parametrize(
    "path",
    ["/portal/login/", "/portal/reset/", "/portal/invite/not-a-token/", "/portal/no-such-page/"],
)
def test_the_signed_out_portal_pages_are_dark(path: str) -> None:
    response = Client().get(path)
    assert response.status_code in (200, 404), (path, response.status_code)
    assert '<html lang="en" data-theme="dark">' in response.content.decode(), path


def test_the_portal_controls_offer_light_and_dark_only() -> None:
    from tests.test_portal import active_user, portal_client

    operator = saved_operator()
    client = portal_client(active_user(operator))
    for path in ("/portal/", "/portal/account/"):
        page = client.get(path).content.decode()
        forms = re.findall(r'<form[^>]*class="portal-theme"[^>]*>.*?</form>', page, re.S)
        assert forms, path
        for form in forms:
            assert re.findall(r'name="theme" value="(\w+)"', form) == ["light", "dark"], path
        assert 'data-theme="dark"' in page and "System" not in page, path


def saved_operator() -> Operator:
    from tests.test_ledger_models import new_operator, saved

    return saved(new_operator(email="portal-theme@example.com"))


def test_the_staff_switch_has_two_segments_in_the_sidebar_the_sheet_and_settings() -> None:
    page = body(client_for(Role.BILLING).get("/settings/"))
    forms = re.findall(
        r'<form[^>]*id="(theme-switch|more-theme-switch|settings-theme-switch)"[^>]*>(.*?)</form>',
        page,
        re.S,
    )
    assert sorted(name for name, _ in forms) == [
        "more-theme-switch",
        "settings-theme-switch",
        "theme-switch",
    ]
    for _, form in forms:
        assert re.findall(r"<button[^>]*>([^<]+)</button>", form) == ["Light", "Dark"]
        assert re.findall(r'value="(\w+)" aria-pressed="true"', form) == ["dark"]
    assert "System" not in page and "this device" not in page

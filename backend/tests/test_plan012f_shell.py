"""PLAN-012f Batch A (docs/qa/PLAN-012f-approved-design.md D1 to D5, §6 A): the spec's tokens,
the self-hosted Inter, the sidebar shell with its page header, the phone shell, the icons and
the PWA colours.

The tokens file holds the spec's ``:root`` block name for name and value for value; no 12c
token name is left in a style sheet or a template; no font comes from another origin and the
font files are the release's, found by the static finders. The sidebar lists its six items in
order with their icons and marks the page's; the user card names the user and the role with
Help and a POST Sign out; the banner comes first and the page header follows it on every page;
the page header's title is 20/600 and a page that does not fill the header shows its title
block. The phone bar, the search field, the bottom bar and the More sheet are checkboxes,
labels and a <details> (no script needed), the bar's item follows the page, and every target is
at least 44 px. Two e2e cases at 390 x 844 and one at 1,280 x 800 check them in a browser.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.staticfiles import finders
from django.http import HttpResponse, HttpResponseBase
from django.template import Context, Template, TemplateSyntaxError, engines
from django.test import Client, RequestFactory, override_settings

from accounts.roles import Role
from finance import context_processors, pwa
from finance.templatetags.icons import ICONS, icon
from tests.auth_helpers import make_user, verified_client

BACKEND = Path(settings.BASE_DIR)
REPO = BACKEND.parent
STATIC = BACKEND / "finance" / "static" / "finance"
TOKENS_CSS = STATIC / "tokens.css"
APP_CSS = STATIC / "app.css"
PRINT_CSS = STATIC / "print.css"
SPEC = REPO / "docs" / "design" / "DESIGN-SPEC.md"
TEMPLATE_ROOTS = (
    BACKEND / "ledger" / "templates",
    BACKEND / "finance" / "templates",
    BACKEND / "accounts" / "templates",
)
#: Inter v4.1, web/InterVariable.woff2 of the release asset Inter-4.1.zip (PLAN-012f D2).
INTER_SHA256 = "693b77d4f32ee9b8bfc995589b5fad5e99adf2832738661f5402f9978429a8e3"
#: The 12c token names (PLAN-012f D1): none may be left.
OLD_TOKEN = re.compile(
    r"--(?:ink|paper|line|status-|s-\d|t-(?:xs|sm|md|lg|xl|2xl)|r-\d|shadow-\d|focus|on-|accent"
    r"|info-|danger|warning|success-|control-border|page\b|navy-2|highlight-bg|kind-|topbar-h"
    r"|nav-w)"
)
LITERAL_COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(")
DECLARATION = re.compile(r"(--[a-z0-9-]+)\s*:\s*([^;]+);")


def client_for(role: Role, **names: str) -> Client:
    user, device = make_user(f"shell12f-{uuid.uuid4().hex[:8]}", role)
    for field, value in names.items():
        setattr(user, field, value)
    user.save()
    assert device is not None
    return verified_client(user, device)


def body(response: HttpResponseBase) -> str:
    assert response.status_code == 200, response
    assert isinstance(response, HttpResponse)
    return response.content.decode()


def element(html: str, element_id: str, closing: str) -> str:
    start = html.index(f'id="{element_id}"')
    return html[html.rfind("<", 0, start) : html.index(closing, start) + len(closing)]


def style_sheets() -> list[Path]:
    return sorted(STATIC.glob("*.css"))


def templates() -> Iterator[Path]:
    for root in TEMPLATE_ROOTS:
        yield from sorted(root.rglob("*.html"))


# --- D1: the tokens --------------------------------------------------------------------------


def declarations(css: str) -> dict[str, str]:
    """Each custom property of ``css`` (comments dropped), its value with whitespace folded."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return {name: " ".join(value.split()) for name, value in DECLARATION.findall(css)}


def spec_block() -> str:
    text = SPEC.read_text(encoding="utf-8")
    block = text.split("## 1. Tokens", 1)[1].split("```css", 1)[1].split("```", 1)[0]
    assert ":root {" in block
    return block


#: The owner's decisions of 2026-10-22 on Batch A (PLAN-012f D1): three values nudged to AA
#: and the primary button's two tokens; every other token is the spec's.
OWNER_TOKENS = {
    "--muted": "#5F6E85",
    "--link": "#0B7C74",
    "--paid-text": "#176B33",
    "--btn-primary-bg": "#0E8F86",
    "--btn-primary-border": "#0B7C74",
}
#: The token Batch B adds beside the spec's block (PLAN-012f D6): the grid's current-month
#: tint, the value DESIGN-SPEC §4 writes.
BATCH_B_TOKENS = {"--month-current": "#EEF6F5"}
#: The two tokens Batch C adds beside it (the dashboard's progress fills, decoration only).
BATCH_C_TOKENS = {"--progress-pending": "#E5B800", "--progress-partial": "#F08A24"}
#: The token Batch D adds (PLAN-012f D13): the sign-in pages' brand panel, the sidebar
#: gradient's two stops at the 160 degrees DESIGN-SPEC §4 Login writes.
BATCH_D_TOKENS = {"--login-gradient": "linear-gradient(160deg, #0B2545 0%, #0E4A5C 100%)"}
#: The token Batch E adds (PLAN-012f-b E-4): the error flash message's text, DESIGN-SPEC §4
#: States' #8A1C14.
BATCH_E_TOKENS = {"--error-text": "#8A1C14"}
#: The names PLAN-012g Batch A adds for the dark theme, with their light values (A1).
BATCH_12G_TOKENS = {
    # PLAN-012g Batch D (the dark walk): the navy button, the pager and the dialog backdrop.
    "--btn-dark-bg": "#0B2545",
    "--btn-dark-text": "#FFFFFF",
    "--backdrop": "rgba(11,37,69,.45)",
    "--sidebar-bg": "#0B2545",
    "--sidebar-border": "transparent",
    "--sidebar-card-bg": "rgba(255,255,255,.08)",
    "--phone-bar-button-bg": "rgba(255,255,255,.10)",
    "--header-bg": "var(--surface)",
    "--card-border": "var(--border)",
    "--text-on-accent": "#FFFFFF",
    "--btn-primary-text": "#FFFFFF",
    "--btn-danger-bg": "#B42318",
    "--sandbox-bg": "#FFF3C4",
    "--sandbox-text": "#6B4E00",
    "--seg-track": "var(--bg)",
    "--seg-thumb": "var(--surface)",
    "--seg-thumb-ring": "var(--border)",
    "--popover-border": "var(--border)",
    "--popover-shadow": "none",
    "--flash-success-bg": "#E3F5E8",
    "--flash-success-text": "#176B33",
    "--flash-warning-bg": "#FFF3C4",
    "--flash-warning-text": "#6B4E00",
    "--flash-error-bg": "#FDE8E6",
    "--flash-error-text": "#8A1C14",
    "--flash-info-bg": "#E8ECF2",
    "--flash-info-text": "#0B2545",
    "--chart-income": "#5B6BE8",
    "--chart-expenses": "#C9CEDD",
    "--chart-net": "#0E8F86",
    "--chart-axis": "var(--border)",
    "--chart-label": "var(--muted)",
    "--chart-overdue": "#B42318",
    "--chart-paid": "#1B7F3B",
    "--chart-upcoming": "#0B2545",
    "--chart-ring-empty": "var(--neutral-bg)",
    "--dot-rent": "#13A89E",
    "--dot-settles": "#1B7F3B",
    "--dot-promise": "#B7791F",
    "--dot-lease-end": "#B42318",
}


def light_block(css: str) -> str:
    """The light ``:root { … }`` block of ``css`` (the dark blocks follow it, PLAN-012g A1)."""
    start = css.index(":root {")
    return css[start : css.index("}", start) + 1]


def test_the_tokens_file_holds_the_spec_block_name_for_name_and_value_for_value() -> None:
    spec = declarations(spec_block())
    assert len(spec) >= 50  # the parse found the spec's block
    expected = {
        **spec,
        **OWNER_TOKENS,
        **BATCH_B_TOKENS,
        **BATCH_C_TOKENS,
        **BATCH_D_TOKENS,
        **BATCH_E_TOKENS,
        **BATCH_12G_TOKENS,
    }
    assert spec["--teal"] == "#13A89E" and expected["--teal"] == "#13A89E"  # --teal unchanged
    assert "#EEF6F5" in SPEC.read_text(encoding="utf-8")  # the spec's value for the tint
    tokens_css = TOKENS_CSS.read_text(encoding="utf-8")
    assert declarations(light_block(tokens_css)) == expected
    # Nothing outside the light block defines a new name (the @font-face rule declares none;
    # the dark blocks of PLAN-012g redefine names of the light block only).
    assert set(declarations(tokens_css)) == set(expected)


def test_faint_is_never_text() -> None:
    # --faint is decoration only (PLAN-012f D1, owner 2026-10-22): no sheet and no template
    # uses it, so no text that must be read is set in it.
    for path in [APP_CSS, PRINT_CSS, *templates()]:
        assert "--faint" not in path.read_text(encoding="utf-8"), path


def test_the_primary_button_takes_its_tokens() -> None:
    css = APP_CSS.read_text(encoding="utf-8")
    assert (
        ".btn-primary { background: var(--btn-primary-bg); border-color: "
        "var(--btn-primary-border); color: var(--btn-primary-text); font-weight: 600; }"
    ) in css
    assert "pair: --surface on --btn-primary-bg\n" in TOKENS_CSS.read_text(encoding="utf-8")


def test_the_comparison_sees_a_changed_value() -> None:
    root = light_block(TOKENS_CSS.read_text(encoding="utf-8"))
    changed = root.replace("--teal: #13A89E;", "--teal: #13A89F;")
    expected = {
        **declarations(spec_block()),
        **OWNER_TOKENS,
        **BATCH_B_TOKENS,
        **BATCH_C_TOKENS,
        **BATCH_D_TOKENS,
        **BATCH_E_TOKENS,
        **BATCH_12G_TOKENS,
    }
    assert declarations(root) == expected
    assert changed != root and declarations(changed) != expected


def test_no_12c_token_name_is_left_in_a_sheet_a_template_or_an_inline_style() -> None:
    offenders = [
        f"{path.relative_to(BACKEND)}:{number}"
        for path in [*style_sheets(), *templates()]
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if OLD_TOKEN.search(line)
    ]
    assert offenders == []
    assert OLD_TOKEN.search("color: var(--ink-2)") and OLD_TOKEN.search("var(--s-4)")
    assert not OLD_TOKEN.search("var(--text-2) var(--space-4) var(--surface-2) var(--fs-sm)")


def test_no_literal_colour_in_app_css_or_print_css() -> None:
    for path in (APP_CSS, PRINT_CSS):
        lines = path.read_text(encoding="utf-8").splitlines()
        assert [n for n, line in enumerate(lines, 1) if LITERAL_COLOUR.search(line)] == [], path


def test_the_pwa_colours_follow_the_tokens() -> None:
    spec = declarations(spec_block())
    assert pwa.THEME_COLOR == spec["--navy"].lower()
    assert pwa.BACKGROUND_COLOR == spec["--bg"].lower()


# --- D2: Inter, self-hosted ------------------------------------------------------------------


def test_no_font_comes_from_another_origin() -> None:
    for path in [*style_sheets(), *templates()]:
        text = path.read_text(encoding="utf-8")
        assert "fonts.googleapis" not in text and "fonts.gstatic" not in text, path
        for face in re.findall(r"@font-face\s*\{[^}]*\}", text):
            assert "http" not in face and "//" not in face, path
    faces = re.findall(r"@font-face\s*\{([^}]*)\}", TOKENS_CSS.read_text(encoding="utf-8"))
    assert len(faces) == 1
    assert "font-display: swap;" in faces[0]
    assert 'src: url("fonts/InterVariable.woff2") format("woff2");' in faces[0]


def test_the_font_files_are_found_and_are_the_release_files() -> None:
    found = finders.find("finance/fonts/InterVariable.woff2")
    assert found, "the static finders do not find the font"
    served = Path(str(found)).read_bytes()
    assert len(served) <= 400 * 1024
    assert hashlib.sha256(served).hexdigest() == INTER_SHA256
    assert (REPO / "static" / "fonts" / "InterVariable.woff2").read_bytes() == served
    for licence in (
        REPO / "static" / "fonts" / "Inter-OFL.txt",
        STATIC / "fonts" / "Inter-OFL.txt",
    ):
        assert "SIL Open Font License, Version 1.1" in licence.read_text(encoding="utf-8")


@pytest.mark.django_db
@override_settings(WHITENOISE_USE_FINDERS=True)
def test_the_font_answers_from_the_app() -> None:
    response = Client().get("/static/finance/fonts/InterVariable.woff2")
    response.close()
    assert response.status_code == 200
    assert response["Content-Type"].startswith("font/woff2")


# --- D3: the sidebar, the user card, the banner and the page header --------------------------

SIDEBAR_ITEMS = (
    ("nav-home", "dashboard", "Dashboard"),
    ("nav-stores", "stores", "Stores"),
    ("nav-operators", "operators", "Operators"),
    ("nav-grid", "monthly-rent", "Monthly rent"),
    ("nav-reports", "reports", "Reports"),
    ("nav-admin", "admin", "Admin"),
)


@pytest.mark.django_db
def test_the_sidebar_lists_its_items_in_order_with_their_icons() -> None:
    html = body(client_for(Role.VIEWER).get("/"))
    sidebar = element(html, "sidebar", "</aside>")
    positions = []
    for item_id, icon_name, label in SIDEBAR_ITEMS:
        item = re.search(
            rf'id="{item_id}"[^>]*>(<svg class="icon icon-{icon_name}" .*?</svg>)'
            rf"<span>{label}</span>",
            sidebar,
            re.S,
        )
        assert item is not None, item_id
        assert 'width="18" height="18"' in item.group(1) and 'aria-hidden="true"' in item.group(1)
        positions.append(item.start())
    assert positions == sorted(positions)
    assert '<a class="sidebar-logo" id="sidebar-logo" href="/">' in sidebar
    assert 'height="48">' in sidebar and "monvera_logo_dark_transparent.svg" in sidebar


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("path", "current"),
    [
        ("/", "nav-home"),
        ("/stores/", "nav-stores"),
        ("/operators/", "nav-operators"),
        ("/grid/", "nav-grid"),
        ("/reports/", "nav-reports"),
        ("/arrears/", "nav-arrears"),
        ("/webhooks/", None),  # the Admin group: the item has no id; its heading is "true"
    ],
)
def test_the_sidebar_marks_the_page_it_is_on(path: str, current: str | None) -> None:
    sidebar = element(body(client_for(Role.BILLING).get(path)), "sidebar", "</aside>")
    marked = re.findall(r'<(?:a|summary)\b[^>]*aria-current="page"[^>]*>', sidebar)
    assert len(marked) == 1, marked
    if current:
        assert f'id="{current}"' in marked[0]
    else:
        assert 'href="/webhooks/"' in marked[0]
        assert '<details class="nav-disclosure" open>' in sidebar
    # A group opens under its pages only.
    assert ('class="nav-group is-open"' in sidebar) is (path in ("/reports/", "/arrears/"))


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("role", "label"), [(Role.VIEWER, "Viewer"), (Role.BILLING, "Billing"), (Role.ADMIN, "Admin")]
)
def test_the_user_card_names_the_user_and_the_role(role: Role, label: str) -> None:
    html = body(client_for(role, first_name="Fatih", last_name="D.").get("/"))
    card = element(html, "user-card", "</div>\n    </div>")
    assert '<span class="user-name" id="user-menu-name">Fatih D.</span>' in card
    assert f'<span class="user-role" id="user-role">{label}</span> · ' in card
    assert (
        '<a href="/settings/" id="nav-preferences">Preferences</a> · '
        '<a href="/help/" id="nav-help">Help</a> · <form'
    ) in card
    assert re.search(
        r'<form method="post" action="/account/logout/"><input type="hidden" '
        r'name="csrfmiddlewaretoken" value="[^"]+"><button type="submit" class="link-button" '
        r'id="nav-sign-out">Sign out</button></form>',
        card,
    )


@pytest.mark.django_db
def test_the_user_card_escapes_the_name() -> None:
    html = body(client_for(Role.VIEWER, first_name="<b>Ada</b>").get("/"))
    assert "&lt;b&gt;Ada&lt;/b&gt;" in element(html, "user-card", "</div>")
    assert "<b>Ada</b>" not in html


@pytest.mark.django_db
@pytest.mark.parametrize("path", ["/", "/stores/", "/grid/", "/reports/", "/help/", "/settings/"])
def test_the_banner_comes_first_and_the_page_header_follows_on_every_page(path: str) -> None:
    html = body(client_for(Role.ADMIN).get(path))
    opened = html.index("<body")
    assert html.count('id="sandbox-banner"') == 1
    assert opened < html.index('id="sandbox-banner"') < html.index('id="sidebar"')
    assert html.index('id="sidebar"') < html.index('id="page-header"') < html.index("<main")
    assert "<title>[SANDBOX] " in html
    css = APP_CSS.read_text(encoding="utf-8")
    # The banner sits over the content column (row 1), the page under it (row 2).
    assert (
        "body.app > #sandbox-banner { grid-column: 2; grid-row: 1; position: sticky !important;"
        in css
    )
    assert ".content { grid-column: 2; grid-row: 2;" in css


def test_the_page_header_and_the_content_follow_the_spec() -> None:
    css = APP_CSS.read_text(encoding="utf-8")
    assert (
        ".page-header-bar { background: var(--header-bg); border-bottom: 1px solid var(--border); }"
        in css
    )
    assert (
        ".page-header-bar h1 { margin: 0; color: var(--text); font-size: var(--fs-h1); "
        "font-weight: 600; }" in css
    )
    assert (
        "main { box-sizing: border-box; width: 100%; max-width: 1240px; padding: 28px 32px; }"
        in css
    )
    assert "--sidebar-w: 232px;" in css
    assert (
        ".sidebar { grid-column: 1; grid-row: 1 / span 2; position: sticky; top: 0; height: 100vh;"
        in css
    )
    assert (
        "padding: 20px 14px; background: var(--sidebar-bg); background-image: "
        "var(--sidebar-gradient); box-shadow: inset -1px 0 0 var(--sidebar-border); "
        "color: var(--sidebar-text); }" in css
    )
    assert (
        ".nav-item { display: flex; align-items: center; gap: var(--space-3); box-sizing: "
        "border-box; width: 100%; padding: 10px 12px; border-radius: var(--radius-ctl); color: "
        "var(--sidebar-text); font-size: var(--fs-md); font-weight: 500;" in css
    )
    assert (
        '.nav-item[aria-current="page"], .nav-sublist a[aria-current="page"] { background: '
        "var(--sidebar-active-bg); color: var(--sidebar-active-text); font-weight: 600; }" in css
    )
    # The user card: white at 8 % on the gradient, a token since PLAN-012g A5.
    assert "background: var(--sidebar-card-bg);" in css


@pytest.mark.django_db
def test_every_page_fills_the_header_with_its_title_and_the_search_box_where_it_has_one() -> None:
    client = client_for(Role.ADMIN)
    for path, title in (
        ("/", "Dashboard"),
        ("/stores/", "Stores"),
        ("/grid/", "Monthly rent overview"),
        ("/operators/", "Operators"),
        ("/help/", "Help"),
    ):
        html = body(client.get(path))
        header = element(html, "page-header", "</header>")
        assert f"<h1>{title}</h1>" in header, path
        assert ('id="nav-search"' in header) is (path in ("/", "/stores/", "/grid/")), path
        main = html[html.index("<main") : html.index("</main>")]
        assert "<h1>" not in main, path  # the title is in the header only


def test_every_page_template_fills_the_header_block() -> None:
    for path in templates():
        text = path.read_text(encoding="utf-8")
        if '{% extends "finance/base.html" %}' not in text:
            continue
        has_header = "{% block header %}" in text
        assert has_header or path.name == "preview.html", path  # the preview takes the default
        assert '<div class="page-header">' not in text.split("{% block content %}", 1)[1], path


@pytest.mark.django_db
def test_the_default_header_is_the_title_block() -> None:
    user, _ = make_user(f"shell12f-{uuid.uuid4().hex[:8]}", Role.VIEWER)
    request = RequestFactory().get("/")
    request.user = user
    page = engines["django"].from_string(
        '{% extends "finance/base.html" %}{% block title %}Plain & {{ word }}{% endblock %}'
    )
    html = page.render({"word": "<b>x</b>"}, request)
    header = element(html, "page-header", "</header>")
    # The block's own text as written, its variable escaped: the same as the tab title.
    assert '<div class="page-header"><h1>Plain & &lt;b&gt;x&lt;/b&gt;</h1></div>' in header
    assert "<title>Monvera · Plain & &lt;b&gt;x&lt;/b&gt;</title>" in html


def test_the_page_title_tag_renders_nothing_outside_a_page() -> None:
    assert Template("{% load shell %}[{% page_title %}]").render(Context()) == "[]"


# --- D4: the phone shell ---------------------------------------------------------------------


@pytest.mark.django_db
def test_the_phone_bar_and_the_search_field_work_without_script() -> None:
    html = body(client_for(Role.VIEWER).get("/stores/"))
    bar = element(html, "phone-bar", "</header>")
    assert (
        '<label for="nav-toggle" class="phone-bar-button" id="nav-menu"><svg class="icon '
        'icon-menu" width="22"' in bar
    )
    assert 'alt="Monvera" height="30">' in bar
    assert (
        '<label for="search-toggle" class="phone-bar-button" id="phone-search-button"><svg '
        'class="icon icon-search" width="22"' in bar
    )
    # The checkboxes come before what they open, so the CSS reaches it with "~".
    order = [
        html.index(marker)
        for marker in (
            'id="nav-toggle"',
            'id="search-toggle"',
            'id="phone-bar"',
            'id="phone-search"',
            'id="sidebar"',
        )
    ]
    assert order == sorted(order)
    search = element(html, "phone-search", "</form>")
    assert 'method="get" action="/search/"' in search and 'maxlength="100"' in search
    css = APP_CSS.read_text(encoding="utf-8")
    phone = css.split("@media (max-width: 768px) {", 1)[1].split("\n}\n", 1)[0]
    assert "#search-toggle:checked ~ .phone-search { display: block;" in phone
    assert "#nav-toggle:checked ~ .sidebar { display: flex; position: fixed;" in phone
    assert "height: var(--phone-bar-h);" in phone and "--phone-bar-h: 64px;" in css
    assert "body.app > #sandbox-banner { order: -2; top: var(--phone-bar-h) !important;" in phone


@pytest.mark.django_db
@pytest.mark.parametrize("role", [Role.VIEWER, Role.BILLING, Role.ADMIN])
def test_the_bottom_bar_and_the_more_sheet(role: Role) -> None:
    html = body(client_for(role).get("/"))
    bar = element(html, "tab-bar", "</nav>")
    items = re.findall(
        r'<(?:a|summary) class="tab-item"[^>]* id="(tab-[a-z]+)"[^>]*>'
        r'(<svg class="icon icon-([a-z-]+)" width="22").*?<span>([^<]+)</span>',
        bar,
        re.S,
    )
    assert [(item_id, name, label) for item_id, _, name, label in items] == [
        ("tab-dashboard", "dashboard", "Dashboard"),
        ("tab-stores", "stores", "Stores"),
        ("tab-grid", "monthly-rent", "Monthly rent"),
        ("tab-more", "more", "More"),
    ]
    sheet = element(html, "more-sheet", "</details>")
    assert sheet.startswith('<details class="more-sheet" id="more-sheet">')
    for label in ("Operators", "Reports", "Admin", "Preferences", "Help", "Messages from Stax"):
        assert re.search(rf">\s*(?:<svg[^>]*>.*?</svg>)?{label}<", sheet, re.S), label
    assert re.search(
        r'<form method="post" action="/account/logout/"><input type="hidden" '
        r'name="csrfmiddlewaretoken"',
        sheet,
    )
    assert 'id="more-sign-out"' in sheet and "Sign out</button></form>" in sheet
    assert ('href="/audit/"' in sheet) is (role == Role.ADMIN)
    assert ('href="/run/"' in sheet) is (role != Role.VIEWER)
    assert sheet.index('href="/settings/"') < sheet.index('href="/help/"')
    assert "aria-current" not in sheet.split("</summary>", 1)[1]


def test_the_phone_targets_are_44px_and_the_content_clears_the_bar() -> None:
    css = APP_CSS.read_text(encoding="utf-8")
    phone = css.split("@media (max-width: 768px) {", 1)[1].split("\n}\n", 1)[0]
    assert "--tap: 44px;" in css
    assert ".content { padding-bottom: 90px; }" in phone
    assert (
        ".phone-bar-button { display: inline-flex; align-items: center; justify-content: "
        "center; flex: 0 0 auto; width: var(--tap); height: var(--tap);" in phone
    )
    assert "min-width: var(--tap); min-height: var(--tap);" in phone  # the tab items
    assert (
        ".nav-item, .nav-sublist a { display: flex; align-items: center; min-height: var(--tap);"
        in phone
    )
    assert "min-height: var(--tap); padding: 0 var(--space-2);" in phone  # the More sheet
    assert ".tab-bar { display: flex; position: fixed; left: 0; right: 0; bottom: 0;" in phone
    assert '.tab-item[aria-current="page"] { color: var(--teal); font-weight: 600; }' in phone


@pytest.mark.parametrize(
    ("view_name", "namespaces", "tab"),
    [
        ("ledger:home", [], "dashboard"),
        ("ledger:stores:detail", ["ledger", "stores"], "stores"),
        ("ledger:grid:page", ["ledger", "grid"], "grid"),
        ("ledger:operators:list", ["ledger", "operators"], "more"),
        ("ledger:run:today", ["ledger", "run"], "more"),
        ("finance:help", ["finance"], "more"),
        ("finance:settings", ["finance"], "more"),
        ("ledger:search", ["ledger"], ""),
    ],
)
def test_the_bottom_bar_item_of_each_section(
    view_name: str, namespaces: list[str], tab: str
) -> None:
    class Match:
        pass

    match = Match()
    match.view_name = view_name  # type: ignore[attr-defined]
    match.namespaces = namespaces  # type: ignore[attr-defined]
    request = RequestFactory().get("/")
    request.resolver_match = match  # type: ignore[assignment]
    assert context_processors.navigation(request)["nav_tab"] == tab


# --- D5: the icons ---------------------------------------------------------------------------

ICON_NAMES = (
    "dashboard",
    "stores",
    "operators",
    "monthly-rent",
    "reports",
    "admin",
    "search",
    "menu",
    "more",
    "plus",
    "file",
    "image",
    "calendar",
    "user",
    "check",
    "circle",
    "circle-half",
    "dash",
    "chevron-down",
    "chevron-right",  # PLAN-012f-a D3: the list rows' chevron
    "arrow-right",
    "x",
    "banknote",  # PLAN-012f-a D4: the Payments card's empty state
    "trash",  # PLAN-012f-a D5: the builder's remove-line button
    "alert",  # PLAN-012f-b E-4: the warning flash message
    "dot",  # PLAN-012f-b E-4: the error flash message
    "info",  # PLAN-012f-b E-4: the information flash message
    "inbox",  # PLAN-012f-b E-4: the empty state's default icon
)


def test_the_icon_table_is_the_plans() -> None:
    assert tuple(ICONS) == ICON_NAMES


@pytest.mark.parametrize("name", ICON_NAMES)
def test_every_icon_renders_as_inline_stroke_svg(name: str) -> None:
    out = Template(f'{{% load icons %}}{{% icon "{name}" %}}|{{% icon "{name}" 22 %}}').render(
        Context()
    )
    default, large = out.split("|")
    assert default.startswith(
        f'<svg class="icon icon-{name}" width="18" height="18" viewBox="0 0 24 24" fill="none" '
        f'stroke="currentColor"'
    )
    assert 'aria-hidden="true" focusable="false">' in default and default.endswith("</svg>")
    assert 'width="22" height="22"' in large
    assert (
        "<script" not in default
        and "href" not in default
        and "http" not in default.split("viewBox", 1)[1]
    )


@override_settings(DEBUG=True)
@pytest.mark.parametrize(
    "call", ['{% icon "nope" %}', '{% icon "menu" 64 %}', '{% icon "menu" "18" %}']
)
def test_an_unknown_icon_or_size_raises_with_debug(call: str) -> None:
    with pytest.raises(TemplateSyntaxError):
        Template("{% load icons %}" + call).render(Context())


@override_settings(DEBUG=False)
def test_an_unknown_icon_renders_nothing_without_debug() -> None:
    assert Template('{% load icons %}[{% icon "nope" %}]').render(Context()) == "[]"
    assert icon("<script>") == ""

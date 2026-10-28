"""PLAN-012c Batch E, D7 (docs/qa/PLAN-012c-design-pass.md §3.4, §6 E): accessibility checked
by tests.

1. Contrast: every documented ``pair:`` line of ``tokens.css`` meets WCAG 2.1 AA (4.5:1 for
   text, 3:1 where the line marks it ``large`` or ``ui``), with no exception (the owner's
   values of 2026-10-22, PLAN-012f D1); every rule of ``app.css`` that sets both a text
   colour and a background from the tokens names a documented pair; and the check itself
   fails on a darkened token. A pair's background may be a gradient's first or last stop
   (``--sidebar-gradient[end]``) and a translucent background is composed over the colour
   named after ``over``.
2. Labels: every visible field of every form class has a label, and every control on the main
   pages, rendered as the real roles over one data set (``tests/visual/world.py``), has an
   associated label.
3. Focus: a ``:focus-visible`` ring from the focus tokens; no ``outline: none`` without a ring.
4. Status never by colour alone: the badges carry their icon and the grid cells name the
   status in words (PLAN-012f D6: the seven families, the cell blocks).
"""

from __future__ import annotations

import html
import importlib
import inspect
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

import pytest
from django import forms
from django.http import HttpResponse
from django.test import Client
from django.urls import reverse

import documents_storage
from accounts.roles import Role
from documents_storage.memory import MemoryDocumentStorage
from finance.templatetags.components import FAMILIES, GLYPHS, status_badge
from payments.fake import FakeProvider
from tests.auth_helpers import make_user, verified_client
from tests.visual import world as visual_world

BACKEND = Path(__file__).resolve().parents[1]
STATIC = BACKEND / "finance" / "static" / "finance"
TOKENS_CSS = STATIC / "tokens.css"
APP_CSS = STATIC / "app.css"
TEMPLATE_ROOTS = (
    BACKEND / "ledger" / "templates",
    BACKEND / "finance" / "templates",
    BACKEND / "accounts" / "templates",
)

TEXT_MINIMUM = 4.5
LARGE_OR_UI_MINIMUM = 3.0

TOKEN = re.compile(r"(--[a-z0-9-]+)\s*:\s*([^;]+);")
PAIR = re.compile(
    r"pair:\s*(--[a-z0-9-]+)\s+on\s+(--[a-z0-9-]+(?:\[(?:start|end)\])?)"
    r"(?:\s+over\s+(--[a-z0-9-]+(?:\[(?:start|end)\])?))?(?:\s+\((large|ui)\))?"
)
HEX = re.compile(r"#([0-9a-fA-F]{6})")
RGBA = re.compile(r"rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([0-9.]+)\s*\)")
STOP = re.compile(r"(--[a-z0-9-]+)\[(start|end)\]")
ALIAS = re.compile(r"var\((--[a-z0-9-]+)\)")
RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
TEXT_COLOUR = re.compile(r"(?<![\w-])color\s*:\s*var\((--[a-z0-9-]+)\)")
BACKGROUND = re.compile(r"(?<![\w-])background(?:-color)?\s*:\s*var\((--[a-z0-9-]+)\)")


# --- 1. Contrast -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Pair:
    foreground: str
    background: str
    kind: str  # "", "large" or "ui"
    over: str = ""  # what a translucent background sits on

    @property
    def minimum(self) -> float:
        return LARGE_OR_UI_MINIMUM if self.kind else TEXT_MINIMUM

    def __str__(self) -> str:
        over = f" over {self.over}" if self.over else ""
        suffix = f" ({self.kind})" if self.kind else ""
        return f"{self.foreground} on {self.background}{over}{suffix}"


def without_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def definitions(*sheets: str) -> dict[str, str]:
    """Every custom property of the sheets' rules, first definition wins (tokens.css first)."""
    found: dict[str, str] = {}
    for sheet in sheets:
        for name, value in TOKEN.findall(without_comments(sheet)):
            found.setdefault(name, value.strip())
    return found


def root_name(name: str, defined: dict[str, str]) -> str:
    """The token a ``var()`` alias chain ends on (``--status-unpaid-ink`` is ``--navy``)."""
    seen = {name}
    while (match := ALIAS.fullmatch(defined.get(name, ""))) is not None:
        name = match.group(1)
        assert name not in seen, f"alias loop at {name}"
        seen.add(name)
    return name


def colour_of(name: str, defined: dict[str, str], over: str = "") -> str:
    """The ``RRGGBB`` of a token: a ``#RRGGBB`` value; a gradient's first or last stop
    (``--name[start]``, ``--name[end]``); an ``rgba()`` composed over ``over``."""
    stop = STOP.fullmatch(name)
    if stop is not None:
        stops = HEX.findall(defined.get(root_name(stop.group(1), defined), ""))
        assert len(stops) >= 2, f"{name} is not a gradient of #RRGGBB stops"
        return str(stops[0] if stop.group(2) == "start" else stops[-1])
    value = defined.get(root_name(name, defined), "")
    translucent = RGBA.fullmatch(value)
    if translucent is not None:
        assert over, f"{name} is translucent: its pair must name what it sits over"
        base = colour_of(over, defined)
        alpha = float(translucent.group(4))
        channels = [int(translucent.group(i)) for i in (1, 2, 3)]
        return "".join(
            f"{round(c * alpha + int(base[2 * i : 2 * i + 2], 16) * (1 - alpha)):02X}"
            for i, c in enumerate(channels)
        )
    match = HEX.fullmatch(value)
    assert match is not None, f"{name} is not a #RRGGBB colour (it is {value!r})"
    return match.group(1)


def relative_luminance(rrggbb: str) -> float:
    """WCAG 2.1 relative luminance of an sRGB colour."""

    def linear(channel: int) -> float:
        c = channel / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(rrggbb[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b)


def contrast_ratio(first: str, second: str) -> float:
    lighter, darker = sorted((relative_luminance(first), relative_luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def documented_pairs(tokens: str) -> list[Pair]:
    return [Pair(fg, bg, kind, over) for fg, bg, over, kind in PAIR.findall(tokens)]


def pair_ratio(pair: Pair, defined: dict[str, str]) -> float:
    return contrast_ratio(
        colour_of(pair.foreground, defined), colour_of(pair.background, defined, pair.over)
    )


#: The one pair the owner accepted under AA (decision of 2026-10-22, PLAN-012f Batch A): the
#: primary button keeps the fill #0E8F86 with a dark border; white on it measures 3.97:1. Any
#: other pair under its threshold, or this pair moving, fails the test.
ACCEPTED_UNDER_AA: dict[str, tuple[float, float]] = {"--surface on --btn-primary-bg": (3.9, 4.1)}


def contrast_failures(
    tokens: str, accepted: dict[str, tuple[float, float]] | None = None
) -> list[str]:
    """Each documented pair under its threshold, named with its ratio; a pair in ``accepted``
    passes while its ratio stays inside the recorded range (an owner's decision, pinned)."""
    defined = definitions(tokens)
    failures = []
    for pair in documented_pairs(tokens):
        ratio = pair_ratio(pair, defined)
        low, high = (accepted or {}).get(str(pair), (pair.minimum, float("inf")))
        if ratio < low or ratio > high:
            failures.append(f"{pair}: {ratio:.2f}:1 is under {pair.minimum}:1")
    return failures


def test_every_documented_pair_meets_wcag_aa() -> None:
    tokens = TOKENS_CSS.read_text()
    pairs = documented_pairs(tokens)
    # The pairs PLAN-012f D1 names: text on bg and the surfaces, the secondary and muted text
    # and the link on the surface, the sidebar text on the gradient's ends and on navy, the
    # active item over both ends, each status, white on the teal and on the red.
    assert len(pairs) >= 25
    named = {str(pair) for pair in pairs}
    for expected in (
        "--text on --bg",
        "--text on --surface",
        "--text on --surface-2",
        "--text-2 on --surface",
        "--muted on --surface",
        "--link on --surface",
        "--sidebar-text on --sidebar-gradient[start]",
        "--sidebar-text on --sidebar-gradient[end]",
        "--sidebar-text on --navy",
        "--sidebar-active-text on --sidebar-active-bg over --sidebar-gradient[start]",
        "--sidebar-active-text on --sidebar-active-bg over --sidebar-gradient[end]",
        *(
            f"--{status}-text on --{status}-bg"
            for status in ("paid", "unpaid", "overdue", "pending", "partial", "waived", "neutral")
        ),
        "--surface on --btn-primary-bg",
        "--surface on --unpaid-text",
    ):
        assert expected in named, expected
    # Every token a pair names is defined as a colour (an alias resolves to one).
    defined = definitions(tokens)
    for pair in pairs:
        pair_ratio(pair, defined)
    assert contrast_failures(tokens, ACCEPTED_UNDER_AA) == []


def test_the_gradient_stops_and_the_translucent_background_resolve() -> None:
    defined = definitions(TOKENS_CSS.read_text())
    assert colour_of("--sidebar-gradient[start]", defined) == "0B2545"
    assert colour_of("--sidebar-gradient[end]", defined) == "0E4A5C"
    # rgba(46,196,166,.25) over the gradient's ends.
    assert colour_of("--sidebar-active-bg", defined, "--sidebar-gradient[start]") == "144D5D"
    assert colour_of("--sidebar-active-bg", defined, "--sidebar-gradient[end]") == "16686E"
    with pytest.raises(AssertionError):
        colour_of("--sidebar-active-bg", defined)


def rules_with_text_on_background(css: str) -> Iterator[tuple[str, str, str]]:
    """(selector, text colour token, background token) of each rule that sets both."""
    for selector, declarations in RULE.findall(without_comments(css)):
        colour = TEXT_COLOUR.search(declarations)
        background = BACKGROUND.search(declarations)
        if colour and background:
            yield " ".join(selector.split()), colour.group(1), background.group(1)


def test_every_text_colour_on_a_background_in_app_css_is_a_documented_pair() -> None:
    tokens = TOKENS_CSS.read_text()
    css = APP_CSS.read_text()
    defined = definitions(tokens, css)  # app.css's own aliases, if any, too
    # A rule's background is the token itself: a gradient pair counts for its token.
    documented = {
        (root_name(pair.foreground, defined), root_name(pair.background.split("[")[0], defined))
        for pair in documented_pairs(tokens)
    }
    checked = list(rules_with_text_on_background(css))
    assert len(checked) >= 20  # the parse found the sheet's rules
    undocumented = [
        f"{selector}: {fg} on {bg}"
        for selector, fg, bg in checked
        if (root_name(fg, defined), root_name(bg, defined)) not in documented
    ]
    assert undocumented == [], "pairings missing a 'pair:' line in tokens.css:\n" + "\n".join(
        undocumented
    )


def test_the_contrast_check_fails_on_a_darkened_foreground() -> None:
    tokens = TOKENS_CSS.read_text()
    # --text-2 on --surface-2 is a documented text pair; a mid grey on a light grey is not AA.
    darkened = re.sub(r"(--text-2:\s*)#[0-9A-Fa-f]{6}", r"\g<1>#9AA3AE", tokens, count=1)
    assert darkened != tokens
    failures = contrast_failures(darkened)
    assert any(line.startswith("--text-2 on --surface-2: ") for line in failures), failures
    assert any(line.startswith("--text-2 on --surface: ") for line in failures), failures
    # A UI pair is held to 3:1, not 4.5:1: --green on the gradient's end (4.44) passes as UI
    # and fails once darkened under 3.
    assert not any(
        line.startswith("--green on") for line in contrast_failures(tokens, ACCEPTED_UNDER_AA)
    )
    faded = re.sub(r"(--green:\s*)#[0-9A-Fa-f]{6}", r"\g<1>#1E7F70", tokens, count=1)
    assert any(
        line.startswith("--green on --sidebar-gradient[end] (ui): ")
        for line in contrast_failures(faded)
    )
    # The translucent background is composed: a paler active text over it fails.
    paled = re.sub(r"(--sidebar-active-text:\s*)#[0-9A-Fa-f]{6}", r"\g<1>#3F7F86", tokens, count=1)
    assert any(
        line.startswith("--sidebar-active-text on --sidebar-active-bg over --sidebar-gradient[end]")
        for line in contrast_failures(paled)
    )


def test_the_ratio_matches_the_wcag_reference_values() -> None:
    assert contrast_ratio("000000", "FFFFFF") == pytest.approx(21.0)
    assert contrast_ratio("FFFFFF", "FFFFFF") == pytest.approx(1.0)
    # #767676 on white is the well-known 4.54:1 grey.
    assert contrast_ratio("767676", "FFFFFF") == pytest.approx(4.54, abs=0.01)


# --- 2. Labels ---------------------------------------------------------------------------------

#: The form modules of the project. ``accounts`` has no forms module: its sign-in and
#: enrolment forms are django-two-factor-auth's, checked on the login page below.
FORM_MODULES = ("ledger.forms",)


def form_classes() -> list[type[forms.BaseForm]]:
    found: list[type[forms.BaseForm]] = []
    for module_name in FORM_MODULES:
        module = importlib.import_module(module_name)
        for name, value in vars(module).items():
            if (
                inspect.isclass(value)
                and issubclass(value, forms.BaseForm)
                and value.__module__ == module_name
                and not name.startswith("_")
            ):
                found.append(value)
    return found


#: Forms whose constructor needs a record or the page's state: they are checked rendered on
#: their page (``PAGES`` below) instead. Each name says which page.
NEEDS_ARGUMENTS = {
    # PLAN-012e-2 D3: the "How to collect" form takes the caller's choices; rendered on the
    # new-invoice and draft pages below.
    "CollectForm": "the new-invoice and draft pages",
    "InvoiceDraftForm": "the invoice builder",
    "ContactEntryForm": "the store and operator workspaces, contacts tab",
    "ContactOutcomeForm": "the store and operator workspaces, contacts tab",
    # PLAN-012f-b C-2: Add payment's form takes the debit card's reason and the balance.
    "AddPaymentForm": "the Add payment page of a rent charge",
}


@pytest.mark.django_db
def test_every_visible_field_of_every_form_class_has_a_label() -> None:
    classes = form_classes()
    assert len(classes) >= 40
    unlabelled: list[str] = []
    needs_arguments: list[str] = []
    for form_class in classes:
        try:
            form = form_class()
        except TypeError:
            needs_arguments.append(form_class.__name__)
            continue
        for bound in form:
            if bound.is_hidden:
                continue
            if not str(bound.label or "").strip():
                unlabelled.append(f"{form_class.__name__}.{bound.name}")
    assert unlabelled == []
    assert sorted(needs_arguments) == sorted(NEEDS_ARGUMENTS)


@dataclass
class Control:
    tag: str
    attributes: dict[str, str]
    wrapped: bool

    def describe(self) -> str:
        shown = {k: v for k, v in self.attributes.items() if k in ("id", "name", "type")}
        return f"<{self.tag} {shown}>"


@dataclass
class ControlsAndLabels(HTMLParser):
    controls: list[Control] = field(default_factory=list)
    label_for: set[str] = field(default_factory=set)
    ids: set[str] = field(default_factory=set)
    open_labels: int = 0

    def __post_init__(self) -> None:
        super().__init__(convert_charrefs=True)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: value or "" for name, value in attrs}
        if "id" in attributes:
            self.ids.add(attributes["id"])
        if tag == "label":
            self.open_labels += 1
            if attributes.get("for"):
                self.label_for.add(attributes["for"])
        elif tag in ("input", "select", "textarea"):
            self.controls.append(Control(tag, attributes, self.open_labels > 0))

    def handle_endtag(self, tag: str) -> None:
        if tag == "label" and self.open_labels:
            self.open_labels -= 1


#: An input type whose own value or kind names it (a button), or that is not shown.
NOT_LABELLED_BY_A_LABEL = {"hidden", "submit", "button", "reset", "image"}


def unlabelled_controls(page: str) -> list[str]:
    parser = ControlsAndLabels()
    parser.feed(page)
    parser.close()
    missing = []
    for control in parser.controls:
        attributes = control.attributes
        if "hidden" in attributes:
            continue  # not rendered, so not in the accessibility tree
        kind = attributes.get("type", "text").lower() if control.tag == "input" else ""
        if kind in NOT_LABELLED_BY_A_LABEL:
            if kind in ("submit", "button", "reset") and not attributes.get("value", "").strip():
                if not attributes.get("aria-label", "").strip():
                    missing.append(control.describe())
            continue
        if control.wrapped or attributes.get("aria-label", "").strip():
            continue
        if attributes.get("id") and attributes["id"] in parser.label_for:
            continue
        labelled_by = attributes.get("aria-labelledby", "").split()
        if labelled_by and all(ref in parser.ids for ref in labelled_by):
            continue
        missing.append(control.describe())
    return missing


def test_the_label_check_finds_a_control_without_a_label() -> None:
    page = (
        '<label for="a">A</label><input id="a" name="a">'
        '<label>B <select name="b"></select></label>'
        '<input name="c" aria-label="C"><span id="d-label">D</span>'
        '<textarea name="d" aria-labelledby="d-label"></textarea>'
        '<input type="hidden" name="e"><input type="submit" value="Save">'
        '<input type="submit" value="" hidden><input name="lonely">'
        '<input name="ghost" aria-labelledby="nowhere">'
        '<label for="other">Other</label><input id="mismatch" name="mismatch">'
    )
    assert unlabelled_controls(page) == [
        "<input {'name': 'lonely'}>",
        "<input {'name': 'ghost'}>",
        "<input {'id': 'mismatch', 'name': 'mismatch'}>",
    ]


@pytest.fixture
def seeded(monkeypatch: pytest.MonkeyPatch) -> visual_world.World:
    clock = visual_world.FrozenClock()
    monkeypatch.setattr("django.utils.timezone.now", clock)
    monkeypatch.setattr("payments.get_provider", FakeProvider)
    monkeypatch.setattr(documents_storage, "get_storage", MemoryDocumentStorage)
    built = visual_world.build()
    clock.stop()
    return built


def page_paths(seeded: visual_world.World) -> list[tuple[str, Role | None, str]]:
    """(page name, role, path) of the main pages; ``None``: signed out."""
    store, operator, lease = seeded.store.pk, seeded.operator.pk, seeded.lease.pk
    end = seeded.lease_end.pk
    pages: list[tuple[str, Role | None, str]] = [
        ("login", None, "/account/login/"),
        ("dashboard", Role.BILLING, "/"),
        ("stores list", Role.BILLING, "/stores/"),
    ]
    for tab in ("charges", "invoices", "payments", "schedules", "documents", "contacts"):
        pages.append((f"store workspace, {tab} tab", Role.BILLING, f"/stores/{store}/?tab={tab}"))
    pages.append(("store workspace, history tab", Role.ADMIN, f"/stores/{store}/?tab=history"))
    for tab in ("charges", "invoices", "payments", "documents", "contacts"):
        pages.append(
            (f"operator workspace, {tab} tab", Role.BILLING, f"/operators/{operator}/?tab={tab}")
        )
    pages += [
        ("lease form (new)", Role.ADMIN, "/leases/new/"),
        ("lease form (edit)", Role.ADMIN, f"/leases/{lease}/edit/"),
        ("operator form (new)", Role.ADMIN, "/operators/new/"),
        ("operator form (edit)", Role.ADMIN, f"/operators/{operator}/edit/"),
        ("grid", Role.BILLING, "/grid/"),
        ("arrears", Role.BILLING, "/arrears/"),
        ("statement", Role.BILLING, f"/operators/{operator}/statement/"),
        ("P&L", Role.ADMIN, "/reports/pnl/"),
        ("payments", Role.BILLING, "/payments/"),
        ("returns", Role.BILLING, "/returns/"),
        ("calendar", Role.BILLING, "/calendar/"),
        ("record a payment", Role.BILLING, f"/stores/{store}/payments/new/"),
        (
            "add payment (rent charge)",
            Role.BILLING,
            reverse(
                "ledger:charges:record_payment",
                kwargs={"pk": seeded.lease.rent_charges.order_by("period_month")[0].pk},
            ),
        ),
        ("invoice builder (new)", Role.BILLING, f"/invoices/new/store/{store}/"),
        (
            "invoice builder (draft)",
            Role.BILLING,
            reverse("ledger:invoices:draft", kwargs={"pk": seeded.draft.pk}),
        ),
        ("daily charges", Role.BILLING, "/run/"),
    ]
    for step in ("terms", "blockers", "statement", "settlement", "confirm"):
        pages.append(
            (
                f"lease-end wizard, {step} step",
                Role.ADMIN,
                reverse(f"ledger:lease_ends:{step}", kwargs={"pk": end}),
            )
        )
    return pages


def client_as(role: Role | None) -> Client:
    if role is None:
        return Client()
    user, device = make_user(f"a11y-{role.value}-{uuid.uuid4().hex[:6]}", role)
    assert device is not None
    return verified_client(user, device)


@pytest.mark.django_db
def test_every_control_on_the_main_pages_has_a_label(seeded: visual_world.World) -> None:
    clients: dict[Role | None, Client] = {}
    findings: list[str] = []
    opened = 0
    for name, role, path in page_paths(seeded):
        client = clients.setdefault(role, client_as(role))
        response = client.get(path, follow=True)
        assert response.status_code == 200, (name, path, response.status_code)
        assert isinstance(response, HttpResponse)
        opened += 1
        findings += [f"{name} ({path}): {c}" for c in unlabelled_controls(response.text)]
    assert opened >= 30
    assert findings == [], "controls without a label:\n" + "\n".join(findings)


#: The sign-in and error pages (PLAN-012f D10, D13): the two steps of the login wizard on
#: their two-panel layout, the 401 page, the second factor's 403, a role's 403 and a 404 in
#: the shell. (name, status, how to open it).
def sign_in_and_error_pages() -> list[tuple[str, int, HttpResponse]]:
    from tests.auth_helpers import TEST_PASSWORD

    make_user(f"a11y-login-{uuid.uuid4().hex[:6]}", Role.VIEWER)
    token_user, _ = make_user(f"a11y-token-{uuid.uuid4().hex[:6]}", Role.VIEWER)
    no_device, _ = make_user(f"a11y-nodev-{uuid.uuid4().hex[:6]}", Role.VIEWER, with_device=False)
    unverified = Client()
    unverified.force_login(no_device)
    viewer = client_as(Role.VIEWER)
    token_step = Client().post(
        "/account/login/",
        {
            "login_view-current_step": "auth",
            "auth-username": token_user.get_username(),
            "auth-password": TEST_PASSWORD,
        },
    )
    pages = [
        ("login, password step", 200, Client().get("/account/login/")),
        ("login, token step", 200, token_step),
        ("401", 401, Client().get("/stores/")),
        ("second factor required", 403, unverified.get("/stores/")),
        ("role refused", 403, viewer.get("/audit/")),
        ("not found", 404, viewer.get("/stores/00000000-0000-0000-0000-000000000000/")),
    ]
    return [(name, status, response) for name, status, response in pages]  # type: ignore[misc]


@pytest.mark.django_db
def test_every_control_on_the_sign_in_and_error_pages_has_a_label() -> None:
    findings: list[str] = []
    for name, status, response in sign_in_and_error_pages():
        assert response.status_code == status, (name, response.status_code)
        page = response.content.decode()
        assert '<html lang="en" data-theme="system">' in page, name  # PLAN-012g A2
        assert page.count("<h1") == 1, name  # one heading names the page
        findings += [f"{name}: {c}" for c in unlabelled_controls(page)]
    assert findings == [], "controls without a label:\n" + "\n".join(findings)
    # The token step has its field (the walk saw the second step, not the first again).
    assert 'id="id_token-otp_token"' in sign_in_and_error_pages()[1][2].content.decode()


# --- 3. Focus ----------------------------------------------------------------------------------

OUTLINE_REMOVED = re.compile(
    r"(?<![\w-])outline\s*:\s*(?:none|0)(?:px)?\s*(?:!important)?\s*(?:;|$)"
)
#: The focus ring (PLAN-012f D1): --link on light surfaces, --green on the gradient.
RING = re.compile(r"(?:box-shadow|outline)\s*:[^;]*var\(--(?:link|green)\)")


def style_sheets() -> list[tuple[str, str]]:
    """(name, CSS) of every style sheet of the staff pages and every template <style> block."""
    sheets = [(path.name, path.read_text()) for path in sorted(STATIC.glob("*.css"))]
    for root in TEMPLATE_ROOTS:
        for path in sorted(root.rglob("*.html")):
            for block in re.findall(r"<style[^>]*>(.*?)</style>", path.read_text(), flags=re.S):
                sheets.append((str(path.relative_to(BACKEND)), block))
    return sheets


def removed_outlines(css: str) -> list[str]:
    return [
        " ".join(selector.split())
        for selector, declarations in RULE.findall(without_comments(css))
        if OUTLINE_REMOVED.search(declarations) and not RING.search(declarations)
    ]


def test_focus_is_a_visible_ring_from_the_focus_tokens() -> None:
    css = without_comments(APP_CSS.read_text())
    rings = {
        " ".join(selector.split()): declarations
        for selector, declarations in RULE.findall(css)
        if ":focus-visible" in selector
    }
    assert ":focus-visible" in rings, sorted(rings)
    assert "0 0 0 2px var(--surface), 0 0 0 4px var(--link)" in rings[":focus-visible"]
    # On the sidebar and the phone bar (the gradient) the ring is --green, a documented UI pair.
    assert (
        "0 0 0 2px var(--navy), 0 0 0 4px var(--green)"
        in rings[".sidebar :focus-visible, .phone-bar :focus-visible"]
    )
    tokens = TOKENS_CSS.read_text()
    assert "pair: --green on --sidebar-gradient[end] (ui)" in tokens
    assert "pair: --link on --surface" in tokens


def test_no_rule_removes_the_outline_without_a_ring() -> None:
    sheets = style_sheets()
    assert any(name == "app.css" for name, _ in sheets)
    offenders = [
        f"{name}: {selector}" for name, css in sheets for selector in removed_outlines(css)
    ]
    assert offenders == []


def test_the_focus_check_catches_a_removed_outline() -> None:
    assert removed_outlines("a:focus { outline: none; }") == ["a:focus"]
    assert removed_outlines("button:focus{outline:0}") == ["button:focus"]
    assert (
        removed_outlines(
            ":focus-visible { outline: none; box-shadow: 0 0 0 2px var(--surface), "
            "0 0 0 4px var(--link); }"
        )
        == []
    )
    assert removed_outlines(".x { outline: 2px solid var(--navy); }") == []
    assert removed_outlines("a:focus { outline: none; box-shadow: 0 0 0 4px var(--green); }") == []


# --- 4. Status never by colour alone -----------------------------------------------------------

HAND_LAID_BADGE = re.compile(
    r'<span class="badge badge-([a-z-]+)"[^>]*>'
    r'(?:<span class="badge-icon" aria-hidden="true">([^<]*)</span> ([^<]+))?'
)


def templates() -> Iterator[tuple[Path, str]]:
    for root in TEMPLATE_ROOTS:
        for path in sorted(root.rglob("*.html")):
            yield path, path.read_text()


def test_every_status_badge_in_a_template_carries_its_glyph_and_its_words() -> None:
    """A template either calls ``{% status_badge %}`` (the glyph comes with it) or lays out
    the markup the tag writes: the status's glyph, hidden from screen readers, then the
    label in words. No other markup gives an element a ``badge-<status>`` class."""
    used_tag = 0
    for path, text in templates():
        used_tag += text.count("{% status_badge ")
        laid = []
        for match in HAND_LAID_BADGE.finditer(text):
            status, glyph, label = match.groups()
            line = text[: match.start()].count("\n") + 1
            where = f"{path.relative_to(BACKEND)}:{line}: badge-{status}"
            assert status in FAMILIES, where
            assert glyph is not None and label is not None, f"{where} has no glyph"
            assert html.unescape(glyph) == GLYPHS[status], where
            assert label.strip(), where
            laid.append(match.start())
        for found in re.finditer(r"\bbadge-(?!icon)[a-z{]", text):
            line = text[: found.start()].count("\n") + 1
            assert text.rfind("<span", 0, found.start()) in laid, (
                f"{path.relative_to(BACKEND)}:{line}"
            )
    assert used_tag >= 5
    # The tag itself: every family gets its icon and its words (a stored value of each).
    for family, value in (
        ("paid", "paid"),
        ("unpaid", "due"),
        ("overdue", "unpaid"),
        ("pending", "pending"),
        ("partial", "partial"),
        ("waived", "waived"),
        ("neutral", "voided"),
    ):
        badge = status_badge("Words", value)
        assert f'class="badge badge-{family}"' in badge and GLYPHS[family] in badge, value
        assert badge.endswith(" Words</span>")
    assert set(FAMILIES) == set(GLYPHS)


#: The status line's first words of each cell status (PLAN-012f D6: the amount comes first,
#: then the icon and these words; an overdue cell reads "Overdue N d").
GRID_WORDS = {
    "paid": "Paid",
    "unpaid": "Overdue",
    "partial": "Partly paid",
    "pending": "Pending",
    "waived": "Waived",
    "due": "Unpaid",  # PLAN-012e-4 D2: not yet overdue, the soft shade
    "not_due": "Not yet due",
}
GRID_CELL = re.compile(r'<td class="cell cell-([a-z_]+)"[^>]*>(.*?)</td>', re.S)


@pytest.mark.django_db
def test_every_grid_cell_names_its_status_in_words(seeded: visual_world.World) -> None:
    page = client_as(Role.VIEWER).get("/grid/")
    assert isinstance(page, HttpResponse)
    cells = GRID_CELL.findall(page.text)
    statuses = {status for status, _ in cells}
    assert {"paid", "unpaid", "partial", "pending", "waived"} <= statuses, statuses
    for status, inner in cells:
        line = re.search(r'<span class="cell-status">(.*)', inner, re.S)
        assert line is not None, (status, inner)
        # The icon is hidden from screen readers; the words follow it.
        shown = re.sub(r'<span aria-hidden="true">[^<]*</span>', "", line.group(1))
        words = html.unescape(re.sub(r"<[^>]+>", "", shown)).strip()
        assert words.startswith(GRID_WORDS[status]), (status, words)
        if status != "not_due":
            assert '<span aria-hidden="true">' in line.group(1), (status, inner)
        link = re.search(r'aria-label="([^"]+)"', inner)
        if link is not None:
            assert GRID_WORDS[status] in html.unescape(link.group(1)), (status, inner)

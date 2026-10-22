"""PLAN-012c Batch A (docs/qa/PLAN-012c-design-pass.md D1 to D4, §6 A): the design tokens, the
company settings row, the shell and the PWA icons.

The shell is checked on every main page as each role: the navy bar with the logo, the company
name and the product name, the navigation's items and groups with ``aria-current``, the tab
title "Monvera · <page>". No literal colour outside ``tokens.css`` (app.css and every template,
except the two named below). The company settings row is seeded, single, edited in the admin
by an admin only and audited; its provider name reaches the job banner.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from django.contrib.admin import site as admin_site
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.http import HttpResponse, HttpResponseBase
from django.test import Client, RequestFactory, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django_otp.plugins.otp_totp.models import TOTPDevice

from accounts.roles import Role
from finance import context_processors
from ledger.admin import CompanySettingsAdmin
from ledger.models import AuditLog, CompanySettings, Document
from payments.models import Job, JobKind, JobStatus
from tests.auth_helpers import make_user, verified_client
from tests.receivables_helpers import a_lease, a_store, an_operator

pytestmark = pytest.mark.django_db

BACKEND = Path(__file__).resolve().parents[1]
STATIC = BACKEND / "finance" / "static" / "finance"
TOKENS_CSS = STATIC / "tokens.css"
APP_CSS = STATIC / "app.css"
BRAND_LOGO = "/static/finance/brand/monvera_logo_dark.svg"
LITERAL_COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(")
#: Templates allowed a literal colour, and why.
COLOUR_EXEMPT = {
    # Injected without context into every HTML page, the Django admin's included, which
    # never loads tokens.css: it must carry its own colours (docs/CLAUDE.md Rule 6).
    "finance/templates/finance/banner.html",
    # The operator's public statement: out of scope of PLAN-012c (§2), its own page.
    "ledger/templates/ledger/reports/statement_public.html",
}


def client_for(role: Role) -> Client:
    user, device = make_user(f"shell-{uuid.uuid4().hex[:8]}", role)
    assert device is not None
    return verified_client(user, device)


def body(response: HttpResponseBase) -> str:
    assert response.status_code == 200, response
    assert isinstance(response, HttpResponse)
    return response.content.decode()


def title(html: str) -> str:
    return html.split("<title>", 1)[1].split("</title>", 1)[0]


def nav_of(html: str) -> str:
    return html[html.index('<nav aria-label="Main">') : html.index("</nav>")]


def bar_of(html: str) -> str:
    return html[html.index('<header class="topbar"') : html.index("</header>")]


# --- The shell on every main page ------------------------------------------------------------

#: (path, page title, the nav item marked current or None, its group or None).
MAIN_PAGES: list[tuple[str, str, str | None, str | None]] = [
    ("/", "Dashboard", "Dashboard", None),
    ("/stores/", "Stores", "Stores", None),
    ("/operators/", "Operators", "Operators", None),
    ("/grid/", "Monthly rent overview", "Monthly rent overview", None),
    ("/reports/", "Reports", "Reports", None),
    ("/arrears/", "Unpaid balances", "Unpaid balances", "nav-reports"),
    ("/reports/pnl/", "Profit and loss", "Profit and loss", "nav-reports"),
    ("/payments/", "Payments", "Payments", "nav-reports"),
    ("/returns/", "Returned payments", "Returned payments", "nav-reports"),
    ("/leases/", "Leases", "Leases", "nav-reports"),
    ("/disputes/", "Disputes", "Disputes", "nav-reports"),
    ("/calendar/", "Calendar", "Calendar", "nav-reports"),
    ("/webhooks/", "Messages from Stax", "Messages from Stax", "nav-admin"),
    ("/reconciliation/", "Nightly check", "Nightly check", "nav-admin"),
    ("/settings/", "Settings", "Settings", "user-menu-name"),
    ("/search/?q=zzz", "Search", None, None),
]
NAV_ITEMS = (
    "Dashboard",
    "Stores",
    "Operators",
    "Monthly rent overview",
    "Reports",
    "Unpaid balances",
    "Profit and loss",
    "Payments",
    "Returned payments",
    "Calendar",
    "Leases",
    "Admin",
    "Messages from Stax",
)
#: Pages billing and admin may open, and so only they see in the navigation.
BILLING_PAGES = {"/disputes/", "/reconciliation/"}
BILLING_ITEMS = ("Disputes", "Nightly check", "Daily charges")


@pytest.mark.parametrize("role", [Role.VIEWER, Role.BILLING, Role.ADMIN])
@pytest.mark.parametrize(("path", "page", "current", "group"), MAIN_PAGES, ids=lambda v: str(v))
def test_every_main_page_renders_in_the_shell(
    role: Role, path: str, page: str, current: str | None, group: str | None
) -> None:
    if role == Role.VIEWER and path in BILLING_PAGES:
        assert client_for(role).get(path).status_code == 403
        return
    html = body(client_for(role).get(path))
    bar = bar_of(html)
    # The brand logo carries the "Monvera" wordmark: the name is its text alternative and
    # is not repeated as text (PLAN-012c Batch B, carry-over a).
    assert (
        f'<img class="topbar-logo" id="topbar-logo" src="{BRAND_LOGO}" alt="Monvera" height="28">'
        in bar
    )
    assert 'id="topbar-company"' not in bar
    assert '<span class="topbar-product" id="topbar-product">Rent ledger</span>' in bar
    assert 'id="nav-search"' in bar and 'id="user-menu"' in bar
    assert ">Settings</a>" in bar and ">Sign out</button>" in bar
    nav = nav_of(html)
    for label in NAV_ITEMS:
        assert f">{label}<" in nav, label
    for label in BILLING_ITEMS:
        assert (f">{label}<" in nav) is (role != Role.VIEWER), label
    assert title(html).startswith(f"[SANDBOX] Monvera · {page}"), title(html)
    assert "| Finance" not in html
    marked = re.findall(r'aria-current="page">([^<]+)<', html)
    assert marked == ([current] if current else []), marked
    groups = re.findall(r'id="([a-z-]+)"[^>]* aria-current="true"', html)
    assert groups == ([group] if group else []), groups


@pytest.mark.parametrize(
    ("role", "shown", "hidden"),
    [
        (
            Role.VIEWER,
            ("Messages from Stax",),
            ("Daily charges", "Activity log", "Nightly check", "System settings"),
        ),
        (
            Role.BILLING,
            ("Messages from Stax", "Daily charges", "Nightly check"),
            ("Activity log", "System settings"),
        ),
        (
            Role.ADMIN,
            (
                "Messages from Stax",
                "Daily charges",
                "Nightly check",
                "Activity log",
                "System settings",
            ),
            (),
        ),
    ],
)
def test_the_admin_group_lists_what_the_role_may_open(
    role: Role, shown: tuple[str, ...], hidden: tuple[str, ...]
) -> None:
    nav = nav_of(body(client_for(role).get("/")))
    for label in shown:
        assert f">{label}</a>" in nav, label
    for label in hidden:
        assert f">{label}</a>" not in nav, label
    for old in (">Grid<", ">Arrears<", ">Rent run<", ">Audit<", ">Webhooks<", ">Django admin<"):
        assert old not in nav, old


@pytest.mark.parametrize(
    ("role", "path", "current", "group"),
    [
        (Role.BILLING, "/run/", "Daily charges", "nav-admin"),
        (Role.ADMIN, "/audit/", "Activity log", "nav-admin"),
    ],
)
def test_the_admin_pages_mark_their_item_and_group(
    role: Role, path: str, current: str, group: str
) -> None:
    html = body(client_for(role).get(path))
    assert re.findall(r'aria-current="page">([^<]+)<', html) == [current]
    assert re.findall(r'id="([a-z-]+)"[^>]* aria-current="true"', html) == [group]


def test_the_existing_link_ids_are_kept() -> None:
    nav = nav_of(body(client_for(Role.ADMIN).get("/")))
    for link_id in (
        "nav-home",
        "nav-grid",
        "nav-reports",
        "nav-arrears",
        "nav-calendar",
        "nav-disputes",
        "nav-run",
        "nav-audit",
        "nav-django-admin",
        "nav-admin-menu",
    ):
        assert f'id="{link_id}"' in nav, link_id


def test_the_user_menu_names_the_user() -> None:
    user, device = make_user(f"shell-{uuid.uuid4().hex[:8]}", Role.VIEWER)
    user.first_name, user.last_name = "Ada", "Lovelace"
    user.save()
    assert device is not None
    bar = bar_of(body(verified_client(user, device).get("/")))
    assert '<summary id="user-menu-name">Ada Lovelace</summary>' in bar


# --- The phone menu ---------------------------------------------------------------------------


def test_the_phone_menu_folds_the_nav_under_900px_without_script() -> None:
    nav = nav_of(body(client_for(Role.VIEWER).get("/")))
    toggle = '<input type="checkbox" id="nav-toggle" class="nav-toggle" aria-controls="nav-links">'
    label = '<label for="nav-toggle" class="nav-toggle-label" id="nav-menu">Menu</label>'
    assert nav.index(toggle) < nav.index(label) < nav.index('id="nav-links"')
    assert "<script" not in nav
    css = APP_CSS.read_text()
    folded = css.split("@media (max-width: 899.98px) {", 1)[1].split("\n}\n", 1)[0]
    assert ".nav-toggle-label { display: inline-block; }" in folded
    assert ".nav-links { display: none;" in folded
    assert ".nav-toggle:checked ~ .nav-links { display: block; }" in folded
    # On a wide screen the toggle is hidden and the left column shows.
    wide = css.split("@media", 1)[0]
    assert ".nav-toggle-label { display: none;" in wide
    assert ".shell { display: grid; grid-template-columns: var(--nav-w) minmax(0, 1fr);" in wide


def test_the_grid_keeps_the_folded_menu_at_every_width() -> None:
    # Twelve 80 px months and the 220 px store column fit at 1,280 px only without the left
    # column (PLAN-011b D2), so the grid folds the navigation behind "Menu" (PLAN-012c D3).
    assert '<div class="shell shell-folded">' in body(client_for(Role.VIEWER).get("/grid/"))
    assert '<div class="shell">' in body(client_for(Role.VIEWER).get("/"))
    wide = APP_CSS.read_text().split("@media", 1)[0]
    assert ".shell-folded { display: block;" in wide
    assert ".shell-folded .nav-toggle-label { display: inline-block; }" in wide
    assert ".shell-folded .nav-links { display: none;" in wide
    assert ".shell-folded .nav-toggle:checked ~ .nav-links { display: block; }" in wide


# --- Tokens ----------------------------------------------------------------------------------


def test_the_tokens_hold_the_brand_colours_and_the_named_scales() -> None:
    css = TOKENS_CSS.read_text()
    assert "--navy: #0B2545;" in css
    assert "--teal: #13A89E;" in css
    assert "--green: #2EC4A6;" in css
    for name in ("--ink", "--ink-2", "--line", "--paper", "--paper-2", "--danger", "--focus-ring"):
        assert f"{name}:" in css, name
    for status in ("paid", "unpaid", "pending", "partial", "waived"):
        assert f"--status-{status}:" in css, status
    # PLAN-012e-4 D1: "due" (unpaid, not yet overdue) takes the soft unpaid pair; no due token.
    assert "--status-unpaid-soft-bg:" in css and "--status-unpaid-soft-ink:" in css
    assert "--status-due" not in css
    for step in range(1, 9):
        assert f"--s-{step}: {4 * (step if step <= 6 else (8 if step == 7 else 12))}px;" in css
    for size in ("xs", "sm", "md", "lg", "xl", "2xl"):
        assert f"--t-{size}:" in css, size
    assert (
        '--font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, '
        "sans-serif;" in css
    )
    assert "url(" not in css and "@import" not in css


def test_every_documented_pair_names_defined_tokens() -> None:
    css = TOKENS_CSS.read_text()
    pairs = re.findall(r"pair: (--[a-z0-9-]+) on (--[a-z0-9-]+)", css)
    assert len(pairs) >= 20
    defined = set(re.findall(r"^\s*(--[a-z0-9-]+):", css, re.MULTILINE))
    for foreground, background in pairs:
        assert foreground in defined and background in defined, (foreground, background)


def _template_files() -> list[Path]:
    return sorted(
        path
        for path in BACKEND.rglob("templates/**/*.html")
        if "__pycache__" not in path.parts and "staticfiles" not in path.parts
    )


def test_no_literal_colour_outside_the_tokens() -> None:
    offenders = []
    for path in [APP_CSS, *_template_files()]:
        relative = str(path.relative_to(BACKEND))
        if relative in COLOUR_EXEMPT:
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if LITERAL_COLOUR.search(line):
                offenders.append(f"{relative}:{number}")
    assert offenders == []
    assert len(_template_files()) > 100  # the walk sees the templates
    for exempt in COLOUR_EXEMPT:
        assert (BACKEND / exempt).exists(), exempt


def test_every_staff_page_links_the_tokens_before_the_style_sheet() -> None:
    html = body(client_for(Role.VIEWER).get("/"))
    tokens = '<link rel="stylesheet" href="/static/finance/tokens.css">'
    app = '<link rel="stylesheet" href="/static/finance/app.css">'
    assert html.index(tokens) < html.index(app)
    login = body(Client().get("/account/login/"))
    assert login.index(tokens) < login.index(app)


# --- Titles --------------------------------------------------------------------------------


def test_the_title_follows_the_short_name() -> None:
    row = CompanySettings.current()
    row.short_name = "Acme"
    row.save()
    assert title(body(client_for(Role.VIEWER).get("/"))) == "[SANDBOX] Acme · Dashboard"


def test_the_sign_in_page_reads_the_short_name() -> None:
    html = body(Client().get("/account/login/"))
    assert title(html) == "[SANDBOX] Monvera · Login"
    assert "| Finance" not in html


def test_the_authenticator_app_name_reads_monvera_sandbox() -> None:
    user, _ = make_user(f"shell-{uuid.uuid4().hex[:8]}", Role.VIEWER, with_device=False)
    device = TOTPDevice.objects.create(user=user, name="default", confirmed=False)
    assert "issuer=Monvera%20%28SANDBOX%29" in device.config_url
    assert "otpauth://totp/Monvera%20%28SANDBOX%29%3A" in device.config_url


@override_settings(STAX_ENV="live")
def test_the_authenticator_app_name_drops_the_sandbox_mark_when_live() -> None:
    from django.conf import settings

    assert settings.OTP_TOTP_ISSUER(None) == "Monvera"


# --- The manifest and the icons ----------------------------------------------------------------


@override_settings(WHITENOISE_USE_FINDERS=True)
@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/static/finance/brand/monvera_icon_192.png", "image/png"),
        ("/static/finance/brand/monvera_icon_512.png", "image/png"),
        ("/static/finance/brand/monvera_icon.svg", "image/svg+xml"),
        ("/static/finance/brand/monvera_logo_dark.svg", "image/svg+xml"),
        ("/static/finance/tokens.css", "text/css"),
    ],
)
def test_the_brand_files_and_the_tokens_answer_with_their_types(
    path: str, content_type: str
) -> None:
    response = Client().get(path)
    response.close()
    assert response.status_code == 200, path
    assert response["Content-Type"].startswith(content_type), response["Content-Type"]


def test_the_manifest_answers_with_its_type_and_the_brand_icons() -> None:
    response = client_for(Role.VIEWER).get("/manifest.webmanifest")
    assert response.status_code == 200
    assert response["Content-Type"] == "application/manifest+json"
    data = response.json()
    assert data["theme_color"] == data["background_color"] == "#0b2545"
    assert all(icon["src"].startswith("/static/finance/brand/") for icon in data["icons"])


def test_the_brand_files_are_copies_of_the_repository_originals() -> None:
    originals = BACKEND.parent / "static" / "brand"
    for name in ("monvera_logo_dark.svg", "monvera_icon.svg"):
        assert (STATIC / "brand" / name).read_bytes() == (originals / name).read_bytes(), name


# --- Company settings -----------------------------------------------------------------------


def test_the_row_is_seeded_by_the_migration() -> None:
    row = CompanySettings.objects.get()
    assert (row.pk, row.name, row.short_name, row.support_email) == (1, "Monvera", "Monvera", "")
    assert (row.time_zone, row.logo, row.provider_name) == ("America/New_York", "", "Stax")


def test_the_table_holds_one_row_only() -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        CompanySettings.objects.bulk_create(
            [CompanySettings(id=2, name="Other", short_name="Other", processor_name="Other")]
        )
    assert CompanySettings.objects.count() == 1


def test_current_creates_the_row_when_it_is_missing() -> None:
    CompanySettings.objects.all().delete()
    row = CompanySettings.current()
    assert (row.pk, row.name, row.provider_name) == (1, "Monvera", "Stax")
    assert CompanySettings.current().pk == 1 and CompanySettings.objects.count() == 1


def test_a_live_image_document_named_by_the_row_becomes_the_logo() -> None:
    store = a_store("0102 LOGO", 102, "GA")
    document = Document.objects.create(
        store=store,
        kind="other",
        original_filename="logo.png",
        content_type="image/png",
        size_bytes=10,
        blob_path=f"documents/store/{store.pk}/{uuid.uuid4()}",
        sha256="0" * 64,
    )
    row = CompanySettings.current()
    row.logo = document.blob_path
    row.full_clean()
    row.save()
    preview = reverse("ledger:documents:preview", kwargs={"pk": document.pk})
    bar = bar_of(body(client_for(Role.VIEWER).get("/")))
    assert f'src="{preview}"' in bar
    # An override logo has no wordmark: the name shows as text beside it.
    assert '<span class="topbar-company" id="topbar-company">Monvera</span>' in bar
    # A deleted document falls back to the brand logo.
    Document.objects.filter(pk=document.pk).update(
        deleted_at="2026-10-16T12:00:00Z", delete_note="Replaced."
    )
    fallback = bar_of(body(client_for(Role.VIEWER).get("/")))
    assert f'src="{BRAND_LOGO}"' in fallback and 'id="topbar-company"' not in fallback


def test_the_processor_reads_the_row_once_a_request() -> None:
    request = RequestFactory().get("/")
    with CaptureQueriesContext(connection) as first:
        shown = context_processors.company(request)["company"]
    with CaptureQueriesContext(connection) as second:
        again = context_processors.company(request)["company"]
        row = context_processors.company_settings(request)
    assert isinstance(shown, context_processors.Company)
    assert len(first.captured_queries) == 1
    assert "SAVEPOINT" not in first.captured_queries[0]["sql"].upper()
    assert len(second.captured_queries) == 0
    assert again is shown and row.short_name == shown.short_name == "Monvera"
    # A new request reads the row again.
    with CaptureQueriesContext(connection) as other:
        context_processors.company(RequestFactory().get("/"))
    assert len(other.captured_queries) == 1


def test_the_logo_lookup_runs_only_when_the_row_names_a_logo() -> None:
    row = CompanySettings.current()
    row.logo = f"documents/store/{uuid.uuid4()}/{uuid.uuid4()}"
    row.save()
    request = RequestFactory().get("/")
    with CaptureQueriesContext(connection) as first:
        shown = context_processors.company(request)["company"]
    with CaptureQueriesContext(connection) as second:
        context_processors.company(request)
    assert (len(first.captured_queries), len(second.captured_queries)) == (2, 0)
    assert isinstance(shown, context_processors.Company)
    assert shown.logo_url == BRAND_LOGO  # no such document


def test_read_never_creates_the_row() -> None:
    CompanySettings.objects.all().delete()
    with CaptureQueriesContext(connection) as captured:
        row = CompanySettings.read()
    assert len(captured.captured_queries) == 1
    assert (row.pk, row.name, row.provider_name, row._state.adding) == (1, "Monvera", "Stax", True)
    assert CompanySettings.objects.count() == 0


def test_read_falls_back_to_the_seed_when_the_table_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise DatabaseError("no such table: company_settings")

    monkeypatch.setattr(CompanySettings.objects, "filter", broken)
    row = CompanySettings.read()
    assert (row.pk, row.short_name, row.provider_name) == (1, "Monvera", "Stax")


def test_the_manifest_reads_the_row_once() -> None:
    client = client_for(Role.VIEWER)
    client.get("/reports/")  # warm the session
    with CaptureQueriesContext(connection) as captured:
        response = client.get("/manifest.webmanifest")
    assert response.status_code == 200
    # One plain read: no get_or_create (its savepoint and its INSERT).
    reads = [q["sql"] for q in captured.captured_queries if '"company_settings"' in q["sql"]]
    assert len(reads) == 1 and reads[0].startswith("SELECT ")


def test_the_admin_registers_change_only() -> None:
    model_admin = admin_site._registry[CompanySettings]
    assert isinstance(model_admin, CompanySettingsAdmin)
    request = Client().request().wsgi_request
    assert model_admin.has_add_permission(request) is False
    assert model_admin.has_delete_permission(request) is False


def _admin_form(**overrides: str) -> dict[str, str]:
    form = {
        "name": "Monvera Holdings",
        "short_name": "Monvera",
        "support_email": "help@example.com",
        "time_zone": "America/Chicago",
        "logo": "",
        "processor_name": "Stax",
    }
    return {**form, **overrides}


def test_an_admin_edits_the_row_in_the_admin_and_it_is_audited() -> None:
    # The admin group's own permissions (ledger 0045), not a superuser's.
    user, device = make_user(f"shell-{uuid.uuid4().hex[:8]}", Role.ADMIN)
    assert device is not None
    client = verified_client(user, device)
    assert client.get("/admin/ledger/companysettings/1/change/").status_code == 200
    response = client.post("/admin/ledger/companysettings/1/change/", _admin_form())
    assert response.status_code == 302
    row = CompanySettings.objects.get()
    assert (row.name, row.time_zone, row.support_email) == (
        "Monvera Holdings",
        "America/Chicago",
        "help@example.com",
    )
    entry = AuditLog.objects.get(actor=user)
    assert entry.action == "company_settings.updated"
    assert (entry.entity_type, entry.entity_id) == ("company_settings", "1")
    assert entry.before is not None and entry.before["name"] == "Monvera"
    assert entry.after is not None and entry.after["name"] == "Monvera Holdings"
    assert client.get("/admin/ledger/companysettings/add/").status_code == 403
    delete = client.post("/admin/ledger/companysettings/1/delete/", {"post": "yes"})
    assert delete.status_code == 403
    assert CompanySettings.objects.count() == 1


def test_the_admin_refuses_a_logo_outside_the_documents_storage() -> None:
    user, device = make_user(f"shell-{uuid.uuid4().hex[:8]}", Role.ADMIN)
    assert device is not None
    client = verified_client(user, device)
    response = client.post(
        "/admin/ledger/companysettings/1/change/",
        _admin_form(logo="https://example.com/logo.svg"),
    )
    assert response.status_code == 200  # the form again, with the error
    assert "The logo must be the path of a stored document, or blank." in response.content.decode()
    assert CompanySettings.objects.get().logo == ""
    assert not AuditLog.objects.filter(action="company_settings.updated").exists()


@pytest.mark.parametrize("role", [Role.VIEWER, Role.BILLING])
def test_below_admin_the_row_cannot_be_changed(role: Role) -> None:
    response = client_for(role).post("/admin/ledger/companysettings/1/change/", _admin_form())
    assert response.status_code == 403
    assert CompanySettings.objects.get().name == "Monvera"


def test_the_names_reach_the_bar_and_the_nav() -> None:
    row = CompanySettings.current()
    row.name, row.provider_name = "Acme Rentals", "PayCo"
    row.save()
    html = body(client_for(Role.VIEWER).get("/"))
    assert (
        'id="topbar-logo" src="/static/finance/brand/monvera_logo_dark.svg" alt="Acme Rentals"'
        in html
    )
    assert ">Messages from PayCo</a>" in nav_of(html)


def test_the_provider_name_is_substituted_in_the_job_banner() -> None:
    operator = an_operator("Banner Ops")
    a_lease(a_store("0103 BANNER", 103, "GA"), operator)
    Job.objects.create(
        kind=JobKind.OPERATOR_SYNC,
        entity_type="operator",
        entity_id=str(operator.pk),
        status=JobStatus.QUEUED,
    )
    row = CompanySettings.current()
    row.provider_name = "PayCo"
    row.save()
    html = body(client_for(Role.BILLING).get(f"/operators/{operator.pk}/"))
    banner = html[
        html.index('<p id="job-status"') : html.index("</p>", html.index('id="job-status"'))
    ]
    assert "<strong>PayCo request</strong>" in banner
    assert "Payment provider request" not in html


# --- The company name once in the bar (PLAN-012c Batch B, carry-over a) ------------------------


def test_without_an_override_the_name_is_the_logos_text_only() -> None:
    bar = bar_of(body(client_for(Role.VIEWER).get("/")))
    assert bar.count("Monvera") == 1  # the alt text; the wordmark is in the image
    assert 'alt="Monvera"' in bar and 'id="topbar-company"' not in bar
    shown = context_processors.company(RequestFactory().get("/"))["company"]
    assert isinstance(shown, context_processors.Company) and shown.logo_is_override is False


def test_with_an_override_the_name_is_shown_as_text_too() -> None:
    store = a_store("0104 LOGO", 104, "GA")
    document = Document.objects.create(
        store=store,
        kind="other",
        original_filename="logo.png",
        content_type="image/png",
        size_bytes=10,
        blob_path=f"documents/store/{store.pk}/{uuid.uuid4()}",
        sha256="0" * 64,
    )
    row = CompanySettings.current()
    row.name, row.logo = "Acme Rentals", document.blob_path
    row.save()
    bar = bar_of(body(client_for(Role.VIEWER).get("/")))
    assert 'alt="Acme Rentals"' in bar
    assert '<span class="topbar-company" id="topbar-company">Acme Rentals</span>' in bar
    shown = context_processors.company(RequestFactory().get("/"))["company"]
    assert isinstance(shown, context_processors.Company) and shown.logo_is_override is True

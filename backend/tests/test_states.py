"""The States (PLAN-012f-b E-4; DESIGN-SPEC §4 "States", states-laptop.png).

The empty state partial (finance/_empty_state.html) and where the pages use it; the flash
messages' four pairs with their icons; the inline form error's style; the confirmation pages
in their dialog over the page they return to (GET writes nothing, one primary, "Keep …" back
to the parent, no id twice on the page); the 404 and 403 cards.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages import constants
from django.db import connection
from django.template import Context, Template
from django.template.loader import render_to_string
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext

from accounts.roles import Role
from ledger.models import CollectionMethod, ContactKind
from ledger.services import contacts, invoice_builder
from ledger.services.invoice_builder import LineInput
from tests.receivables_helpers import (
    a_lease,
    a_one_off_charge,
    a_payment,
    a_rent_charge,
    a_store,
    an_operator,
)
from tests.test_autopay_plans_pages import a_plan, a_plan_lease
from tests.test_design_components import _with_messages
from tests.test_installments import NOW, a_one_off, plan_for
from tests.test_operator_views import client_for
from tests.test_rent_run import health  # noqa: F401 (autouse fixture)

pytestmark = pytest.mark.django_db

D = Decimal
STATIC = Path(__file__).resolve().parents[1] / "finance" / "static" / "finance"
EMPTY = "finance/_empty_state.html"


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("django.utils.timezone.now", lambda: NOW)


def _css() -> str:
    return (STATIC / "app.css").read_text(encoding="utf-8")


def _rule(selector: str) -> str:
    """The declarations of the first rule whose selector list is exactly ``selector``."""
    found = re.search(r"(?:^|\n)" + re.escape(selector) + r" \{([^}]*)\}", _css())
    assert found, selector
    return found.group(1)


def _ids(html: str) -> Counter[str]:
    return Counter(re.findall(r'\sid="([^"]+)"', html))


def _dialog(html: str, dialog_id: str) -> str:
    start = html.index(f'<dialog class="dialog dialog-sm" id="{dialog_id}"')
    return html[start : html.index("</dialog>", start) + len("</dialog>")]


# --- The empty state partial ---------------------------------------------------------------------


def _empty(**values: Any) -> str:
    return render_to_string(EMPTY, values)


def test_the_empty_state_has_the_circle_icon_the_line_the_sentence_and_the_link() -> None:
    html = _empty(
        id="disputes-none",
        icon="file",
        title="No disputes",
        text="Disputed payments show here.",
        action_url="/help/disputes/",
        action_label="Learn how disputes work",
        action_id="disputes-help",
    )
    assert '<div class="empty-state" id="disputes-none">' in html
    assert '<span class="empty-state-icon"><svg class="icon icon-file" width="20"' in html
    assert '<p class="empty-state-title">No disputes</p>' in html
    assert '<p class="empty-state-text">Disputed payments show here.</p>' in html
    assert (
        '<a class="btn empty-state-action" href="/help/disputes/" id="disputes-help">'
        "Learn how disputes work</a>"
    ) in html
    assert "btn-primary" not in html  # the page's one primary is never the empty state's


def test_the_empty_state_without_a_sentence_or_link_shows_neither_and_falls_back_to_inbox() -> None:
    html = _empty(title="Nothing on this day.")
    assert '<div class="empty-state">' in html and "icon-inbox" in html
    assert "empty-state-text" not in html and "<a " not in html
    # A label without a URL (a role without the step) is no link either.
    assert "<a " not in _empty(title="No repeating debit.", action_label="New invoice")


def test_the_empty_state_escapes_every_value() -> None:
    markup = '<script>alert("x")</script>'
    html = _empty(
        id='x" onclick="y',
        title=markup,
        text=markup,
        action_url='/ok/" onmouseover="z',
        action_label=markup,
    )
    assert "<script>" not in html and 'onclick="y"' not in html and 'onmouseover="z"' not in html
    assert html.count("&lt;script&gt;") == 3


def test_included_with_only_the_page_s_own_title_and_text_never_leak_in() -> None:
    html = Template(
        '{% include "finance/_empty_state.html" with title="No store matches." only %}'
    ).render(Context({"text": "A page's own text", "icon": "nope", "action_url": "/x/"}))
    assert "A page's own text" not in html and "icon-inbox" in html and "<a " not in html


def test_the_empty_state_styles_are_the_spec_s() -> None:
    icon = _rule(".empty-state-icon")
    assert "width: 48px; height: 48px;" in icon and "border-radius: 50%;" in icon
    assert "background: var(--bg); color: var(--muted);" in icon
    title = _rule(".empty-state-title")
    assert "font-size: var(--fs-lg); font-weight: 600;" in title
    assert "color: var(--muted);" in _rule(".empty-state-text")


# --- Where the pages use it ---------------------------------------------------------------------


def test_the_dashboard_s_empty_cards_use_the_empty_state_and_keep_their_ids() -> None:
    html = client_for(Role.ADMIN)[0].get("/").content.decode()
    for block_id, title in (
        ("attention-none", "Nothing needs attention."),
        ("recent-payments-none", "No settled payments yet."),
        ("coming-up-none", "Nothing scheduled."),
        ("failed-attempts-none", "No failed attempts."),
        ("not-sent-none", "Nothing waiting to be sent."),
    ):
        block = html[html.index(f'<div class="empty-state" id="{block_id}">') :]
        assert f'<p class="empty-state-title">{title}</p>' in block.split("</div>", 1)[0]
    collection = html[html.index('<div class="empty-state" id="collection-none">') :]
    assert '<p class="empty-state-title">No rent billed for Oct 2026 yet.</p>' in collection
    failed = html[html.index('<div class="empty-state" id="failed-attempts-none">') :]
    assert "could not collect are listed here.</p>" in failed.split("</div>", 1)[0]
    # The dashboard's one primary is not an empty state's.
    assert "empty-state-action btn-primary" not in html


def test_the_search_page_s_no_match_escapes_the_query_in_the_empty_state() -> None:
    viewer = client_for(Role.VIEWER)[0]
    html = viewer.get("/search/", {"q": "<b>zzz</b>"}).content.decode()
    block = html[html.index('<div class="empty-state" id="search-no-match">') :]
    assert "No match for &#x27;&lt;b&gt;zzz&lt;/b&gt;&#x27;." in block.split("</div>", 1)[0]
    assert "<b>zzz</b>" not in html


def test_the_autopay_tab_without_a_plan_offers_new_invoice_only_to_a_role_with_it() -> None:
    lease = a_lease(a_store("2207 EMPTY", 2207), an_operator("Empty Ops"))
    url = f"/stores/{lease.store_id}/?tab=schedules"
    billing = client_for(Role.BILLING)[0].get(url).content.decode()
    block = billing[billing.index('<div class="empty-state" id="plan-rows-empty">') :]
    block = block.split("</div>", 1)[0]
    assert '<p class="empty-state-title">No repeating debit.</p>' in block
    assert 'class="btn empty-state-action" href="/invoices/new/store/' in block
    viewer = client_for(Role.VIEWER)[0].get(url).content.decode()
    block = viewer[viewer.index('id="plan-rows-empty"') :].split("</div>", 1)[0]
    assert "empty-state-action" not in block


def test_a_table_keeps_its_plain_empty_row() -> None:
    html = client_for(Role.ADMIN)[0].get("/stores/").content.decode()
    assert '<td colspan="7" class="muted empty">No stores match.' in html
    assert 'class="empty-state"' not in html[html.index("<tbody>") : html.index("</tbody>")]


# --- Flash messages ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("level", "tag", "icon"),
    [
        (constants.SUCCESS, "success", "check"),
        (constants.WARNING, "warning", "alert"),
        (constants.ERROR, "error", "dot"),
        (constants.INFO, "info", "info"),
    ],
)
def test_each_flash_message_carries_its_level_s_icon_before_the_text(
    level: int, tag: str, icon: str
) -> None:
    html = _with_messages((level, "Saved <b>x</b>."))
    item = html[html.index(f'<li class="toast {tag}" role="status">') :].split("</li>", 1)[0]
    assert item.index('<span class="toast-icon"><svg class="icon icon-' + icon + '" width="16"') < (
        item.index('<span class="toast-text">Saved &lt;b&gt;x&lt;/b&gt;.</span>')
    )
    assert 'aria-label="Dismiss this message">&times;</button>' in html


def test_the_flash_styles_are_the_spec_s_four_pairs() -> None:
    toast = _rule(".toast")
    assert "border-radius: 10px;" in toast
    assert "background: var(--neutral-bg); color: var(--navy);" in toast  # information
    assert _rule(".toast.success").strip() == "background: var(--paid-bg); color: var(--paid-text);"
    assert (
        _rule(".toast.warning").strip()
        == "background: var(--pending-bg); color: var(--pending-text);"
    )
    assert (
        _rule(".toast.error").strip() == "background: var(--unpaid-bg); color: var(--error-text);"
    )
    tokens = (STATIC / "tokens.css").read_text(encoding="utf-8")
    assert "--error-text: #8A1C14;" in tokens
    assert "pair: --error-text on --unpaid-bg" in tokens


def test_an_inline_form_error_is_a_one_pixel_red_border_and_a_twelve_pixel_red_line() -> None:
    css = _css()
    assert (
        ".field.has-error input:not([type=checkbox]):not([type=radio]), .field.has-error select, "
        ".field.has-error textarea { border: 1px solid var(--unpaid-text); }"
    ) in css
    errors = _rule(".field .errorlist")
    assert "color: var(--unpaid-text);" in errors and "font-size: var(--fs-sm);" in errors
    assert "2px solid var(--unpaid-text)" not in css


# --- 404 and 403 ---------------------------------------------------------------------------------


def test_a_signed_in_404_is_the_card_with_the_code_the_line_and_the_two_ways_on() -> None:
    response = client_for(Role.VIEWER)[0].get("/stores/00000000-0000-0000-0000-000000000000/")
    assert response.status_code == 404
    html = response.content.decode()
    card = html[html.index('<section class="card error-card" id="error-page">') :]
    card = card.split("</section>", 1)[0]
    assert '<p class="error-code" aria-hidden="true">404</p>' in card
    assert '<h2 class="error-line">That page is not here</h2>' in card
    assert "The link may be old, or the store or operator was removed." in card
    assert (
        '<a class="btn btn-primary" id="error-dashboard" href="/">Go to the dashboard</a>' in card
    )
    assert '<a class="btn" id="error-search" href="/search/">Search</a>' in card
    assert html.count("btn-primary") == 1
    assert "00000000-0000-0000-0000-000000000000" not in card  # nothing from the request


def test_a_signed_in_403_is_the_card_with_its_sentence() -> None:
    response = client_for(Role.VIEWER)[0].get("/audit/")
    assert response.status_code == 403
    html = response.content.decode()
    card = html[html.index('id="error-page">') :].split("</section>", 1)[0]
    assert '<p class="error-code" aria-hidden="true">403</p>' in card
    assert '<h2 class="error-line">Not open to your role</h2>' in card
    assert "Your role does not allow this action." in card
    assert 'id="error-dashboard"' in card and 'id="error-search"' in card


def test_a_signed_out_404_keeps_the_sign_in_base_and_offers_no_shell_link() -> None:
    request = RequestFactory().get("/nowhere/")
    request.user = AnonymousUser()
    html = render_to_string("404.html", request=request)
    assert '<body class="auth">' in html and 'id="sidebar"' not in html
    assert '<p class="error-code" aria-hidden="true">404</p>' in html
    assert 'id="error-dashboard"' not in html and 'id="error-search"' not in html


def test_the_large_code_is_decoration_never_the_faint_token() -> None:
    code = _rule(".error-card .error-code")
    assert "color: var(--input-border);" in code and "--faint" not in code


# --- The confirmation pages in their dialog ------------------------------------------------------

MONTH = date(2026, 9, 1)
#: The writes a signed-in GET makes whatever the page (the session and its last-seen time).
SESSION_TABLES = ("django_session", "accounts_usersession")


def _lease() -> Any:
    return a_lease(a_store("2301 DIALOG", 2301), an_operator("Dialog Ops"))


def _payment_case(name: str) -> dict[str, Any]:
    payment = a_payment(a_rent_charge(_lease(), MONTH), "100.00", MONTH)
    back = f"/payments/{payment.pk}/"
    return {"url": f"{back}{name}/", "back": back, "marker": 'id="payment-status"'}


def _one_off_case(name: str, collection: str = CollectionMethod.MANUAL) -> dict[str, Any]:
    lease = _lease()
    assert lease.operator is not None
    charge = a_one_off_charge(lease.operator, "250.00", MONTH, lease=lease, collection=collection)
    back = f"/one-off-charges/{charge.pk}/"
    return {"url": f"{back}{name}/", "back": back, "marker": 'id="charge-status"'}


def _draft_case(name: str) -> dict[str, Any]:
    lease = _lease()
    assert lease.operator is not None
    outcome = invoice_builder.create_draft(
        lease.operator,
        lease,
        "Move-in",
        None,
        [LineInput(item="Sign repair", description="", quantity=None, unit_amount=D("100.00"))],
        client_for(Role.BILLING)[1],
        today=NOW.date(),
    )
    assert outcome.draft is not None, outcome.message
    back = f"/invoices/drafts/{outcome.draft.pk}/"
    return {"url": f"{back}{name}/", "back": back, "marker": 'id="draft-status"'}


def _plan_case() -> dict[str, Any]:
    lease = _lease()
    assert lease.operator is not None
    plan = plan_for(a_one_off(lease.operator, lease=lease))
    back = f"/plans/{plan.pk}/"
    return {"url": f"{back}cancel/", "back": back, "marker": 'id="plan-status"'}


def _autopay_plan_case() -> dict[str, Any]:
    plan = a_plan(a_plan_lease())
    back = f"/stores/{plan.store_id}/?tab=schedules#tab-schedules"
    return {
        "url": f"/stores/{plan.store_id}/plans/{plan.pk}/stop/",
        "back": back,
        "marker": 'id="store-tabs"',
    }


def _stop_autopay_case() -> dict[str, Any]:
    lease = _lease()
    back = f"/leases/{lease.pk}/"
    return {"url": f"{back}stop-autopay/", "back": back, "marker": 'id="lease-status"'}


def _contact_case() -> dict[str, Any]:
    lease = _lease()
    assert lease.operator is not None
    outcome = contacts.add_entry(
        lease.operator.pk,
        contacts.ContactInput(
            kind=ContactKind.CALL,
            happened_on=NOW.date(),
            body="Called about rent.",
            promised_date=None,
            promised_amount=None,
            lease=lease.pk,
        ),
        actor=None,
    )
    assert outcome.entry is not None, outcome.message
    back = f"/operators/{lease.operator.pk}/#contact-{outcome.entry.pk}"
    return {
        "url": f"/contacts/{outcome.entry.pk}/delete/",
        "back": back,
        "marker": 'id="operator-title"',
    }


#: (case, its dialog's id, its "Keep …" words, whether its form always shows its button)
CASES: dict[str, tuple[Callable[[], dict[str, Any]], str, str, bool]] = {
    "payment-void": (lambda: _payment_case("void"), "void-dialog", "Keep the payment", False),
    "payment-refund": (lambda: _payment_case("refund"), "refund-dialog", "Keep the payment", False),
    "one-off-waive": (lambda: _one_off_case("waive"), "waive-dialog", "Keep the charge", True),
    "one-off-cancel": (lambda: _one_off_case("cancel"), "cancel-dialog", "Keep the charge", True),
    "one-off-charge-now": (
        lambda: _one_off_case("charge-now", CollectionMethod.CHARGE_NOW),
        "charge-now-dialog",
        "Keep the charge",
        False,
    ),
    "draft-cancel": (lambda: _draft_case("cancel"), "cancel-dialog", "Keep the draft", True),
    "draft-charge-now": (
        lambda: _draft_case("charge-now"),
        "charge-now-dialog",
        "Keep the invoice",
        False,
    ),
    "plan-stop": (_plan_case, "stop-plan-dialog", "Keep the plan", True),
    "autopay-plan-stop": (_autopay_plan_case, "plan-stop-dialog", "Keep the plan", False),
    "lease-stop-autopay": (_stop_autopay_case, "stop-autopay-dialog", "Keep autopay", False),
    "contact-delete": (_contact_case, "delete-contact-dialog", "Keep the entry", True),
}


def _writes(queries: CaptureQueriesContext) -> list[str]:
    found = []
    for query in queries.captured_queries:
        sql = query["sql"].lstrip().upper()
        if sql.startswith(("INSERT", "UPDATE", "DELETE")) and not any(
            f'"{table.upper()}"' in sql for table in SESSION_TABLES
        ):
            found.append(query["sql"][:120])
    return found


@pytest.mark.parametrize("name", list(CASES))
def test_a_confirmation_page_is_its_dialog_open_over_the_page_it_returns_to(name: str) -> None:
    build, dialog_id, keep, always = CASES[name]
    case = build()
    client = client_for(Role.ADMIN)[0]
    with CaptureQueriesContext(connection) as queries:
        response = client.get(case["url"])
    assert response.status_code == 200, (name, response.status_code)
    assert _writes(queries) == [], name  # GET writes nothing
    html = response.content.decode()
    dialog = _dialog(html, dialog_id)
    back = case["back"].replace("&", "&amp;")
    assert dialog.startswith(
        f'<dialog class="dialog dialog-sm" id="{dialog_id}" aria-labelledby="{dialog_id}-title" '
        f'open data-return-url="{back}" data-open>'
    ), dialog[:300]
    # The page behind is the record or the tab the dialog returns to.
    behind = html.replace(dialog, "")
    assert case["marker"] in behind, name
    # "Keep …" goes back to it, before the one primary (red when destructive), right-aligned.
    foot = dialog[dialog.index('<div class="dialog-foot">') :].split("</div>", 1)[0]
    assert f'href="{back}"' in foot and f">{keep}</a>" in foot, foot
    primaries = len(re.findall(r'class="btn btn-(?:primary|danger)"', dialog))
    assert primaries == (1 if always else primaries) and primaries <= 1, (name, primaries)
    if primaries:
        assert foot.index(f">{keep}</a>") < foot.index('type="submit"')
    # No id twice on the page (the dialog's and the page's own).
    assert [key for key, count in _ids(html).items() if count > 1] == [], name


def test_the_dialog_foot_is_right_aligned_and_the_confirm_card_loses_its_frame() -> None:
    assert "justify-content: flex-end;" in _rule(".dialog-foot")
    assert "border: 0;" in _rule(".dialog .confirm")


def test_a_wizard_s_confirm_step_keeps_its_wizard_frame() -> None:
    for name in ("lease_ends/confirm.html", "transfers/confirm.html"):
        text = (Path(__file__).resolve().parents[1] / "ledger/templates/ledger" / name).read_text(
            encoding="utf-8"
        )
        assert text.startswith('{% extends "ledger/lease_ends/_wizard.html" %}')
        assert "{% dialog" not in text

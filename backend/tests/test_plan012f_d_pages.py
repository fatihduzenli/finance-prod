"""PLAN-012f Batch D (docs/qa/PLAN-012f-approved-design.md D10, D13; DESIGN-SPEC §4 Login, §6):
every other page in the shell and the components, the sign-in layout, the phone rules.

Every page of ``ledger/urls.py`` and ``finance/urls.py`` that a GET renders opens with 200 for
the role that may open it, in the one document that carries both shells (the laptop's sidebar
and the phone's bars; the width picks one in CSS), with its ``#page-header``, no 12c class and at
most one primary button in the header; every other route of those files is listed with the
reason a GET renders no page (a POST-only action, a redirect, a file, a feed). The pages with a
parent carry the breadcrumb of the table below (the parents' titles, then the page's own). The
login page has the two panels, the "Username" label, no forgot or keep-me control, the banner
strip and the company's name; its phone variant collapses the brand panel. A form's primary
button sits in ``.form-actions``, sticky above the tab bar on a phone; a wizard step shows the
"Step N of M" line.
"""

from __future__ import annotations

import html as html_lib
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.template import Context, Template
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts import registry
from accounts.roles import Role
from ledger.models import (
    AutopayPlan,
    CalendarEntry,
    CompanySettings,
    ContactEntry,
    InstallmentCadence,
    InstallmentCollection,
    InstallmentPlan,
    InvoiceDraft,
    Lease,
    LeaseEnd,
    LeaseTransfer,
    MessageTemplate,
    Note,
    OneOffCharge,
    Operator,
    Payment,
    PaymentChannel,
    RentCharge,
    Store,
    StoreCost,
)
from ledger.services import notes as note_service
from payments.models import WebhookEvent, WebhookOutcome
from tests.auth_helpers import iter_routes
from tests.receivables_helpers import (
    a_lease,
    a_one_off_charge,
    a_payment,
    a_rent_charge,
    a_store,
    an_operator,
)
from tests.test_contact_log import promise
from tests.test_deposits import held_deposit
from tests.test_lease_end import OCTOBER, october, saved
from tests.test_lease_end import draft as lease_end_draft
from tests.test_operator_views import client_for
from tests.test_plan012f_components import OLD_COMPONENT
from tests.test_pnl import a_cost
from tests.test_store_workspace import a_draft
from tests.test_transfer import begin

pytestmark = pytest.mark.django_db

BACKEND = Path(__file__).resolve().parents[1]
APP_CSS = BACKEND / "finance" / "static" / "finance" / "app.css"
COMPANY = "Global Retail Group"


# --- The data every page is opened on ----------------------------------------------------------


@dataclass
class World:
    admin: User
    #: The admin who wrote the contact entry and the calendar note: their edit pages open.
    author: Client
    operator: Operator
    store: Store
    lease: Lease
    rent: RentCharge
    payment: Payment
    one_off: OneOffCharge
    draft: InvoiceDraft
    plan: InstallmentPlan
    autopay: AutopayPlan
    cost: StoreCost
    entry: ContactEntry
    #: A store note the admin wrote (PLAN-014 B5): its Edit and Delete dialogs open.
    note: Note
    calendar: CalendarEntry
    event: WebhookEvent
    deposit: OneOffCharge
    end: LeaseEnd
    transfer: LeaseTransfer

    def template_pk(self) -> Any:
        return MessageTemplate.objects.get(code="reminder_due").pk

    def pk_for(self, name: str) -> Any:
        """The record a route of ``name`` opens with its ``pk``."""
        special = {
            "ledger:invoices:new_store": self.store.pk,
            "ledger:invoices:new_operator": self.operator.pk,
            "ledger:leases:resolve_deposit": self.deposit.lease_id,
            "ledger:returns:resolve": self.payment.pk,
            "ledger:returns:reschedule": self.payment.pk,
            # PLAN-014 D4, D5: a message template's pages; "Send now" is POST-only.
            "ledger:settings:template_edit": self.template_pk(),
            "ledger:settings:template_preview": self.template_pk(),
            "ledger:outbox:send_now": uuid.UUID(int=1),
        }
        if name in special:
            return special[name]
        by_namespace = {
            "operators": self.operator.pk,
            "stores": self.store.pk,
            "contacts": self.entry.pk,
            "notes": self.note.pk,
            "leases": self.lease.pk,
            "lease_ends": self.end.pk,
            "transfers": self.transfer.pk,
            "charges": self.rent.pk,
            "one_off_charges": self.one_off.pk,
            "plans": self.plan.pk,
            "invoices": self.draft.pk,
            "payments": self.payment.pk,
            "webhooks": self.event.pk,
            "calendar": self.calendar.pk,
        }
        return by_namespace.get(name.split(":")[1], self.store.pk)


@pytest.fixture
def world() -> World:
    row = CompanySettings.current()
    row.name = COMPANY
    row.save()
    admin, admin_user = client_for(Role.ADMIN)
    operator = an_operator("Walk Co")
    store = a_store("0909 WALK", 909)
    lease = a_lease(store, operator, channel=PaymentChannel.WIRE)
    rent = a_rent_charge(lease, date(2026, 10, 1))
    payment = a_payment(rent, "100.00", date(2026, 10, 6))
    one_off = a_one_off_charge(operator, "250.00", date(2026, 10, 1), lease=lease)
    plan = InstallmentPlan.objects.create(
        rent_charge=rent,
        operator=operator,
        lease=lease,
        count=2,
        first_due=date(2026, 10, 20),
        cadence=InstallmentCadence.MONTHLY,
        collection=InstallmentCollection.MANUAL,
        confirmed_at=timezone.now(),
    )
    return World(
        admin=admin_user,
        author=admin,
        operator=operator,
        store=store,
        lease=lease,
        rent=rent,
        payment=payment,
        one_off=one_off,
        draft=a_draft(lease),
        plan=plan,
        # PLAN-012f-a Batch E: a repeating debit plan on the lease; its Stop page asks first.
        autopay=AutopayPlan.objects.create(
            lease=lease,
            operator=operator,
            store=store,
            item_name="Sign fee",
            description="",
            quantity=Decimal("1"),
            unit_amount=Decimal("100.00"),
            amount=Decimal("100.00"),
            cadence="monthly",
            first_on=date(2026, 11, 1),
            end_rule="never",
            next_on=date(2026, 11, 1),
        ),
        cost=a_cost(store),
        # Recorded by the admin, so its edit window is open to them.
        entry=promise(
            operator, lease=lease, actor=admin_user, by=date(2026, 10, 30), amount="100.00"
        ),
        calendar=CalendarEntry.objects.create(
            kind="note", on=date(2026, 10, 2), title="Walk note", store=store, author=admin_user
        ),
        event=WebhookEvent.objects.create(
            event_type="update_transaction",
            provider_object_id="t1",
            dedupe_hash="0" * 64,
            outcome=WebhookOutcome.DEAD_LETTER,
        ),
        deposit=held_deposit(),
        # The statement step saved, as the confirm page needs since PLAN-012f-a slice F.
        end=saved(
            lease_end_draft(
                a_lease(
                    a_store("0910 END", 910), an_operator("End Co"), channel=PaymentChannel.WIRE
                ),
                admin_user,
            ),
            admin_user,
        ),
        transfer=begin(moving_lease(), admin_user),
        note=note_service.add(store, "Walk store note.", admin_user),
    )


def moving_lease() -> Lease:
    """The transfer's outgoing lease: it starts in the end month and that month is paid in
    full, so no month is missing and nothing blocks the incoming step (the balance
    blockers count the months the statement would still create, PLAN-012f-a D)."""
    lease = a_lease(
        a_store("0911 MOVE", 911),
        an_operator("Move Co"),
        channel=PaymentChannel.WIRE,
        started=OCTOBER,
        debit_day=1,
        rent="2000.00",
    )
    october(lease, paid="2000.00")
    return lease


def path_of(world: World, name: str) -> str:
    raw = next(raw for route, raw, _ in iter_routes() if route == name)
    values: dict[str, Any] = {
        "pk": world.pk_for(name),
        "row": world.cost.pk,
        "charge": world.deposit.pk,
        "plan": world.autopay.pk,
        "outcome": "kept",
        "slug": "promises-to-pay",
        "rest": "no-such-token",
        # PLAN-014 I4: a sender name's id (none exists: the POST-only route answers 405 to a GET).
        "alias": "00000000-0000-0000-0000-000000000000",
    }
    kwargs: dict[str, Any] = {}
    for first, second in re.findall(r"<(?:\w+:)?(\w+)>|\(\?P<(\w+)>", raw):
        parameter = first or second
        kwargs[parameter] = values[parameter]
    return reverse(name, kwargs=kwargs)


# --- Which routes are pages ------------------------------------------------------------------

#: The routes a GET renders as a page, with the role that opens it where the registry's rule
#: (the role of an unsafe request to the route, which its form posts) does not say: a page
#: the view keeps to a role (PLAN-011 D2, PLAN-012 D3) or one every role reads.
PAGES: dict[str, Role] = {
    "finance:settings": Role.VIEWER,
    # PLAN-012f-a Batch E (E-4): Stop asks first; the page is the admin's, as its POST.
    "ledger:stores:plan_stop": Role.ADMIN,
    "finance:help": Role.VIEWER,
    "finance:help_topic": Role.VIEWER,
    "ledger:home": Role.VIEWER,
    "ledger:search": Role.VIEWER,
    "ledger:calendar:page": Role.VIEWER,
    "ledger:calendar:detail": Role.VIEWER,
    "ledger:grid:page": Role.VIEWER,
    "ledger:arrears:page": Role.VIEWER,
    "ledger:operators:list": Role.VIEWER,
    "ledger:operators:detail": Role.VIEWER,
    "ledger:operators:statement": Role.VIEWER,
    "ledger:operators:contacts": Role.VIEWER,
    "ledger:stores:list": Role.VIEWER,
    "ledger:stores:detail": Role.VIEWER,
    "ledger:stores:contacts": Role.VIEWER,
    "ledger:leases:list": Role.VIEWER,
    "ledger:leases:detail": Role.VIEWER,
    # PLAN-014 C7: the bank-entry dialog over the lease page (GET only; billing).
    "ledger:leases:bank": Role.BILLING,
    "ledger:lease_ends:blockers": Role.ADMIN,
    "ledger:transfers:blockers": Role.ADMIN,
    "ledger:run:today": Role.BILLING,
    "ledger:charges:detail": Role.VIEWER,
    "ledger:one_off_charges:detail": Role.VIEWER,
    "ledger:plans:detail": Role.VIEWER,
    "ledger:invoices:draft": Role.BILLING,
    "ledger:invoices:preview": Role.BILLING,
    # PLAN-012f-b D15: Save's "Invoice saved" prompt is the Invoices tab with its dialog open.
    "ledger:invoices:saved": Role.BILLING,
    "ledger:payments:list": Role.VIEWER,
    "ledger:payments:detail": Role.VIEWER,
    "ledger:disputes:page": Role.BILLING,
    "ledger:webhooks:list": Role.VIEWER,
    "ledger:webhooks:detail": Role.VIEWER,
    "ledger:returns:list": Role.VIEWER,
    "ledger:reconciliation:page": Role.BILLING,
    "ledger:reports:index": Role.VIEWER,
    "ledger:reports:pnl": Role.VIEWER,
    "ledger:reports:aging": Role.VIEWER,
    "ledger:reports:revenue": Role.VIEWER,
    "ledger:reports:payments_by_method": Role.VIEWER,
    "ledger:reports:deposits": Role.VIEWER,
    "ledger:audit:page": Role.ADMIN,
    # PLAN-014 B2: Admin › Settings.
    "ledger:settings:index": Role.ADMIN,
    # PLAN-014 D4, D5: the message templates and the Outbox (the views keep them to admin).
    "ledger:settings:templates": Role.ADMIN,
    "ledger:settings:template_edit": Role.ADMIN,
    "ledger:outbox:page": Role.ADMIN,
}
#: The routes whose GET renders a form, opened by the role the registry gives their POST.
FORM_PAGES = (
    "ledger:calendar:add",
    "ledger:calendar:edit",
    "ledger:operators:create",
    "ledger:operators:edit",
    "ledger:operators:contacts_add",
    "ledger:stores:create",
    "ledger:stores:edit",
    "ledger:stores:set_cost",
    "ledger:stores:correct_cost",
    "ledger:stores:contacts_add",
    "ledger:stores:end_lease",
    "ledger:stores:transfer",
    "ledger:stores:record_payment",
    "ledger:contacts:edit",
    "ledger:contacts:correct",
    "ledger:contacts:delete",
    "ledger:contacts:outcome",
    "ledger:leases:create",
    "ledger:leases:edit",
    "ledger:leases:enrol",
    "ledger:leases:stop_autopay",
    # PLAN-014 C3: Edit billing details, the lease page with its dialog open (it replaced the
    # operator's Connect to Stax page and its Try again).
    "ledger:leases:billing",
    "ledger:leases:resolve_deposit",
    "ledger:lease_ends:terms",
    "ledger:lease_ends:statement",
    "ledger:lease_ends:settlement",
    "ledger:lease_ends:confirm",
    "ledger:transfers:operator",
    "ledger:transfers:incoming",
    "ledger:charges:record_payment",
    "ledger:charges:link_invoice",
    "ledger:charges:split",
    # PLAN-012f-b §10 D19: today's bank debit of a rent charge, as the one-off's Charge now.
    "ledger:charges:charge_now",
    "ledger:one_off_charges:payment",
    "ledger:one_off_charges:link",
    "ledger:one_off_charges:edit",
    "ledger:one_off_charges:waive",
    "ledger:one_off_charges:cancel",
    "ledger:one_off_charges:charge_now",
    "ledger:one_off_charges:split",
    "ledger:plans:reschedule",
    "ledger:plans:cancel",
    "ledger:invoices:new_store",
    "ledger:invoices:new_operator",
    "ledger:invoices:charge_now",
    "ledger:invoices:cancel",
    "ledger:payments:refund",
    "ledger:payments:void",
    # PLAN-012f-c D5: over the payment page (the world's payment is a bank debit, so billing
    # sees its refusal; tests/test_void_manual_payment.py opens it on a manual payment).
    "ledger:payments:void_manual",
    # PLAN-014 B5: the Edit note and Delete note dialogs over the store page.
    "ledger:notes:edit",
    "ledger:notes:delete",
)

POST_ONLY = "a POST-only action (405 to a GET)"
#: Every other route of ledger/urls.py and finance/urls.py, with why a GET renders no page.
NOT_PAGES: dict[str, str] = {
    "finance:manifest": "the PWA manifest (JSON)",
    "finance:service_worker": "the service worker (JavaScript)",
    "finance:feed_create": POST_ONLY,
    "finance:theme": POST_ONLY,  # the appearance choice (PLAN-012g A3)
    "finance:payment_status_period": POST_ONLY,  # the Payment status card's period (PLAN-014 A3)
    "finance:feed_revoke": POST_ONLY,
    "ledger:calendar:feed": "the public calendar feed (iCalendar; its token is the credential)",
    "ledger:calendar:delete": POST_ONLY,
    "ledger:statement_link": "the operator's own signed statement page (out of scope, §2)",
    "ledger:invoices:rent_amount": "the builder's rent amount (JSON for its script)",
    "ledger:invoices:settlement_preview": (
        "Schedule settlement's preview sentence (JSON for the builder's script)"
    ),
    # PLAN-012f-c D13: opened without its charge it answers a refusal.
    "ledger:plans:split_preview": "Split into installments' preview (JSON for the page's script)",
    "ledger:search_stores": "the store picker's rows (JSON for the picker's script)",
    "ledger:invoices:print": "the invoice document's print page (its own layout)",
    "ledger:lease_ends:pdf": "the statement PDF",
    "ledger:documents:download": "a stored file",
    "ledger:documents:preview": "a stored file in a frame",
    "ledger:stores:schedule_payment": "a redirect to the builder with its choice (PLAN-012e-2 D3)",
    "ledger:stores:charge_now_pick": "a redirect to the builder with its choice (PLAN-012e-2 D3)",
    "ledger:leases:end": "a redirect to the lease-end wizard",
    "ledger:leases:add_setup_charge": "a redirect to the invoice builder (PLAN-012 D6)",
    "ledger:transfers:end": "a redirect to the lease-end wizard's steps",
    "ledger:transfers:confirm": "a redirect to the step to do until the incoming lease is set",
    "ledger:operators:new_charge": "410 Gone: replaced by the invoice builder (PLAN-012 D6)",
    "ledger:leases:new_charge": "410 Gone: replaced by the invoice builder (PLAN-012 D6)",
}
for _name in (
    "ledger:operators:archive",
    "ledger:operators:handled_personally",
    "ledger:operators:statement_links_revoke",
    # PLAN-014 H2: the PORTAL card's actions.
    "ledger:operators:portal_invite",
    "ledger:operators:portal_disable",
    "ledger:operators:portal_enable",
    "ledger:operators:portal_reset",
    "ledger:stores:close_cost",
    "ledger:stores:create_charges",
    "ledger:stores:check_address",
    "ledger:stores:plan_pause",
    "ledger:stores:plan_resume",
    "ledger:stores:plan_end",
    "ledger:plans:pause",
    "ledger:plans:resume",
    "ledger:leases:pause",
    "ledger:leases:resume",
    "ledger:lease_ends:cancel",
    "ledger:transfers:cancel",
    "ledger:transfers:override_debt",
    "ledger:run:create_charges",
    "ledger:run:match",
    "ledger:charges:retry_mark",
    "ledger:charges:retry_release",
    "ledger:charges:send_message",
    "ledger:one_off_charges:retry_mark",
    "ledger:one_off_charges:delete_invoice",
    "ledger:one_off_charges:retry",
    # PLAN-014 F3: Stop automatic retry, a POST from the charge page and the returns queue.
    "ledger:one_off_charges:stop_retry",
    "ledger:one_off_charges:send_message",
    "ledger:invoices:edit",
    "ledger:invoices:send",
    "ledger:invoices:delete_draft",
    "ledger:disputes:refresh",
    "ledger:disputes:evidence",
    "ledger:disputes:evidence_retry",
    "ledger:webhooks:replay",
    "ledger:returns:resolve",
    "ledger:returns:reschedule",
    "ledger:reconciliation:run",
    "ledger:reconciliation:resolve",
    "ledger:documents:upload_store",
    "ledger:documents:upload_lease",
    "ledger:documents:delete",
    # PLAN-014 B5: a store note is written and pinned by POST.
    "ledger:notes:add",
    "ledger:notes:pin",
    # PLAN-014 B2: Admin › Settings' Brands card.
    "ledger:settings:brand_add",
    "ledger:settings:brand_save",
    "ledger:settings:brand_toggle",
    "ledger:settings:brand_move",
    # PLAN-014 C7: the bank-entry page posts the new payment-method id here.
    "ledger:leases:bank_attach",
    # PLAN-014 C9: the LEASE card's "Switch to own account" posts here.
    "ledger:leases:switch_own",
    # PLAN-014 D1, D4, D5: the Mail card's save, a template's Preview and "Send now".
    "ledger:settings:mail_save",
    "ledger:settings:template_preview",
    "ledger:outbox:send_now",
    # PLAN-014 I2: the Bank deposits card's switches.
    "ledger:settings:bank_save",
    # PLAN-014 I4: the billing details dialog's sender names (add, remove).
    "ledger:leases:alias_add",
    "ledger:leases:alias_remove",
    # PLAN-014 I-b: the LEASE card's insurance "Bill now" posts here.
    "ledger:leases:insurance_bill",
):
    NOT_PAGES[_name] = POST_ONLY


def is_export(name: str) -> bool:
    return name.endswith(("export", "_export")) or name.startswith("ledger:documents:export")


#: The pages only the author of the record (or an admin) may open; opened as its author.
#: Pages only their author (or an admin) may open: the world's author wrote them all
#: (PLAN-014 B5 adds the note's Edit and Delete dialogs).
AUTHOR_PAGES = (
    "ledger:calendar:edit",
    "ledger:contacts:edit",
    "ledger:notes:edit",
    "ledger:notes:delete",
)


def client_of(world: World, name: str, clients: dict[Role, Client]) -> Client:
    if name in AUTHOR_PAGES:
        return world.author
    role = role_of(name)
    return clients.setdefault(role, client_for(role)[0])


def role_of(name: str) -> Role:
    if name in PAGES:
        return PAGES[name]
    rule = registry.rule_for(name)
    assert rule is not None and rule.min_role is not None, name
    return rule.min_role


def project_routes() -> list[str]:
    return [
        name
        for name, _, _ in iter_routes()
        if name and name.startswith(("ledger:", "finance:")) and not is_export(name)
    ]


def test_every_route_of_the_two_url_files_is_a_page_or_says_why_not() -> None:
    routes = set(project_routes())
    listed = set(PAGES) | set(FORM_PAGES) | set(NOT_PAGES)
    assert routes - listed == set(), "a route with no decision"
    assert listed - routes == set(), "a decision with no route"
    assert not set(PAGES) & set(FORM_PAGES) and not set(PAGES) & set(NOT_PAGES)


def test_the_routes_that_are_not_pages_answer_as_listed(world: World) -> None:
    admin = client_for(Role.ADMIN)[0]
    for name, reason in NOT_PAGES.items():
        if name == "ledger:statement_link":
            continue  # no token to open it with here; its own tests cover it
        response = admin.get(path_of(world, name))
        if reason == POST_ONLY:
            assert response.status_code == 405, name
        elif reason.startswith("410"):
            assert response.status_code == 410, name
        elif reason.startswith("a redirect"):
            assert response.status_code == 302, name
        else:
            # Not a page: a file, a feed or JSON (or the 404 page when opened without what
            # it needs, as here the rent amount without its lease and month).
            body = response.content.decode(errors="replace")
            assert response.status_code != 200 or 'id="page-header"' not in body, name


# --- Every page in the shell ------------------------------------------------------------------


def header_of(body: str) -> str:
    start = body.index('<header class="page-header-bar" id="page-header">')
    return body[start : body.index("</header>", start)]


def test_every_page_renders_in_the_shell_for_the_role_that_may_open_it(world: World) -> None:
    clients: dict[Role, Client] = {}
    opened = 0
    for name in [*PAGES, *FORM_PAGES]:
        client = client_of(world, name, clients)
        path = path_of(world, name)
        response = client.get(path)
        assert response.status_code == 200, (name, path, response.status_code)
        body = response.content.decode()
        opened += 1
        # One document for both widths: the laptop's sidebar and the phone's bars; CSS
        # picks one (app.css, 768 px), the viewport meta makes the phone use its width.
        assert '<meta name="viewport" content="width=device-width, initial-scale=1.0">' in body
        for shell in ('id="sidebar"', 'id="phone-bar"', 'id="tab-bar"', 'id="page-header"'):
            assert body.count(shell) == 1, (name, shell)
        assert body.count('id="sandbox-banner"') == 1, name
        offenders = [match.group(0) for match in OLD_COMPONENT.finditer(body)]
        assert offenders == [], (name, offenders)
        assert header_of(body).count("btn-primary") <= 1, name
    assert opened == len(PAGES) + len(FORM_PAGES) >= 85


def test_no_page_keeps_a_back_link_where_its_breadcrumb_is() -> None:
    """The "← Parent" links went to the header's breadcrumb (D10); the calendar's "← Previous"
    month is a pager, not a back link."""
    offenders = []
    for root in ("ledger", "finance", "accounts"):
        for path in sorted((BACKEND / root / "templates").rglob("*.html")):
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"&larr;", text):
                line = text[text.rfind("\n", 0, match.start()) + 1 : text.find("\n", match.end())]
                if 'id="calendar-previous"' in line or "invoices/preview.html" in str(path):
                    continue
                offenders.append(f"{path.relative_to(BACKEND)}: {line.strip()[:80]}")
    assert offenders == []


# --- Breadcrumbs -------------------------------------------------------------------------------


def crumbs(body: str) -> tuple[list[tuple[str, str]], str]:
    """The breadcrumb's links (href, text) and the current page's text."""
    header = header_of(body)
    match = re.search(
        r'<nav class="breadcrumb" aria-label="Breadcrumb" id="breadcrumb">(.*?)</nav>', header, re.S
    )
    assert match is not None, "no breadcrumb"
    trail = match.group(1)
    links = [
        (href, html_lib.unescape(re.sub(r"<[^>]+>", "", text)).strip())
        for href, text in re.findall(r'<a href="([^"]+)"[^>]*>(.*?)</a>', trail, re.S)
    ]
    current = re.search(r'<span aria-current="page">(.*?)</span>', trail, re.S)
    assert current is not None, "no current page in the breadcrumb"
    return links, html_lib.unescape(current.group(1)).strip()


def deposit_store(world: World) -> str:
    lease = world.deposit.lease
    assert lease is not None
    return str(lease.store.external_label)


def _store(world: World) -> list[str]:
    return ["Stores", "#909"]


#: Page → (its parents' titles in the breadcrumb, the page's own words in it).
BREADCRUMBS: dict[str, Callable[[World], tuple[list[str], str]]] = {
    # PLAN-012f-b E-4: a confirmation page is its dialog over the page it returns to, so it
    # carries that page's breadcrumb (the dialog's title names the question).
    # PLAN-012f-b Batch B: New / Edit store and operator are dialogs over the list or the
    # page (tests/test_form_modals.py); the page behind keeps its own header.
    "ledger:stores:set_cost": lambda w: (_store(w), "Set landlord rent"),
    "ledger:stores:correct_cost": lambda w: (_store(w), "Correct a cost row"),
    "ledger:stores:record_payment": lambda w: (_store(w), "Add payment"),
    "ledger:stores:end_lease": lambda w: (_store(w), "End lease"),
    "ledger:stores:transfer": lambda w: (_store(w), "Transfer"),
    "ledger:stores:contacts": lambda w: ([f"Store {w.store.external_label}"], "Notes & follow-ups"),
    "ledger:operators:statement": lambda w: (["Operators", "Walk Co"], "Statement"),
    # PLAN-014 D4: Admin › Settings › Message templates and one template.
    "ledger:settings:templates": lambda w: (["Settings"], "Message templates"),
    "ledger:settings:template_edit": lambda w: (
        ["Settings", "Message templates"],
        "B — Payment due",
    ),
    "ledger:operators:contacts": lambda w: (["Walk Co"], "Notes & follow-ups"),
    "ledger:operators:contacts_add": lambda w: (["Walk Co"], "Add entry: Walk Co"),
    "ledger:leases:create": lambda w: (["Leases"], "New lease"),
    "ledger:leases:detail": lambda w: (["Leases"], f"Lease {w.store.external_label}"),
    "ledger:leases:edit": lambda w: (
        ["Leases", f"Lease {w.store.external_label}"],
        f"Edit lease {w.store.external_label}",
    ),
    "ledger:leases:enrol": lambda w: (
        ["Leases", f"Lease {w.store.external_label}"],
        "Set up autopay",
    ),
    "ledger:leases:stop_autopay": lambda w: BREADCRUMBS["ledger:leases:detail"](w),
    "ledger:leases:billing": lambda w: BREADCRUMBS["ledger:leases:detail"](w),
    "ledger:leases:bank": lambda w: BREADCRUMBS["ledger:leases:detail"](w),
    "ledger:leases:resolve_deposit": lambda w: (
        ["Leases", f"Lease {deposit_store(w)}"],
        "Return deposit",
    ),
    "ledger:lease_ends:terms": lambda w: (["Stores", "#910"], "End lease"),
    "ledger:lease_ends:blockers": lambda w: (["Stores", "#910"], "End lease"),
    "ledger:lease_ends:statement": lambda w: (["Stores", "#910"], "End lease"),
    "ledger:lease_ends:settlement": lambda w: (["Stores", "#910"], "End lease"),
    "ledger:lease_ends:confirm": lambda w: (["Stores", "#910"], "End lease"),
    "ledger:transfers:operator": lambda w: (["Stores", "#911"], "Transfer"),
    "ledger:transfers:blockers": lambda w: (["Stores", "#911"], "Transfer"),
    "ledger:transfers:incoming": lambda w: (["Stores", "#911"], "Transfer"),
    # PLAN-012f-a D4: the detail family's breadcrumb names the store page's tab.
    "ledger:charges:detail": lambda w: ([*_store(w), "Charges"], "Rent Oct 2026, debit 1"),
    "ledger:charges:record_payment": lambda w: (
        [*_store(w), f"Rent Oct 2026, {w.store.external_label}"],
        "Add payment",
    ),
    "ledger:charges:link_invoice": lambda w: (
        [*_store(w), f"Rent Oct 2026, {w.store.external_label}"],
        "Attach Stax invoice",
    ),
    # PLAN-012f-c D13: the Add payment family's breadcrumb, Stores › store › charge.
    "ledger:charges:split": lambda w: ([*_store(w), "Rent Oct 2026"], "Split into installments"),
    "ledger:one_off_charges:detail": lambda w: ([*_store(w), "Charges"], w.one_off.label),
    "ledger:one_off_charges:payment": lambda w: ([*_store(w), w.one_off.label], "Add payment"),
    "ledger:one_off_charges:link": lambda w: (
        [*_store(w), w.one_off.label],
        "Attach Stax invoice",
    ),
    "ledger:one_off_charges:edit": lambda w: (
        [*_store(w), w.one_off.label],
        f"Edit {w.one_off.label}",
    ),
    "ledger:one_off_charges:waive": lambda w: BREADCRUMBS["ledger:one_off_charges:detail"](w),
    "ledger:one_off_charges:cancel": lambda w: BREADCRUMBS["ledger:one_off_charges:detail"](w),
    "ledger:one_off_charges:charge_now": lambda w: BREADCRUMBS["ledger:one_off_charges:detail"](w),
    "ledger:charges:charge_now": lambda w: BREADCRUMBS["ledger:charges:detail"](w),
    "ledger:one_off_charges:split": lambda w: (
        [*_store(w), w.one_off.label],
        "Split into installments",
    ),
    "ledger:plans:detail": lambda w: ([*_store(w), "Rent Oct 2026"], "Installment plan"),
    "ledger:plans:reschedule": lambda w: (
        [*_store(w), "Installment plan"],
        "Reschedule",
    ),
    "ledger:plans:cancel": lambda w: BREADCRUMBS["ledger:plans:detail"](w),
    "ledger:invoices:new_store": lambda w: (_store(w), "New invoice"),
    "ledger:invoices:new_operator": lambda w: (["Operators", "Walk Co"], "New invoice"),
    "ledger:invoices:draft": lambda w: (_store(w), f"Invoice draft {w.draft.number}"),
    "ledger:invoices:charge_now": lambda w: BREADCRUMBS["ledger:invoices:draft"](w),
    "ledger:invoices:cancel": lambda w: BREADCRUMBS["ledger:invoices:draft"](w),
    "ledger:payments:detail": lambda w: ([*_store(w), "Payments"], "Payment of $100.00"),
    "ledger:payments:refund": lambda w: BREADCRUMBS["ledger:payments:detail"](w),
    "ledger:payments:void": lambda w: BREADCRUMBS["ledger:payments:detail"](w),
    "ledger:payments:void_manual": lambda w: BREADCRUMBS["ledger:payments:detail"](w),
    "ledger:calendar:page": lambda w: (["Reports"], "Calendar"),
    "ledger:calendar:detail": lambda w: (["Calendar: Oct 2026"], "Walk note"),
    "ledger:calendar:add": lambda w: (["Calendar"], "New calendar entry"),
    "ledger:calendar:edit": lambda w: (["Calendar", "Walk note"], "Edit calendar entry"),
    "ledger:webhooks:detail": lambda w: (
        ["Messages from Stax"],
        w.event.event_type_words,
    ),
    "ledger:reports:aging": lambda w: (["Reports"], "Aging"),  # PLAN-014 G3
    "ledger:reports:revenue": lambda w: (["Reports"], "Revenue by month"),
    "ledger:reports:payments_by_method": lambda w: (["Reports"], "Payments by method"),
    "ledger:reports:deposits": lambda w: (["Reports"], "Bank account check"),
    # PLAN-012f-b E-1: the report family's header (DESIGN-SPEC §4 Profit and loss).
    "ledger:reports:pnl": lambda w: (["Reports"], "Profit and loss"),
    "ledger:contacts:edit": lambda w: (["Notes & follow-ups"], "Edit contact entry"),
    "ledger:contacts:correct": lambda w: (["Notes & follow-ups"], "Add a correction"),
    # PLAN-012f-b E-4: Delete contact entry is its dialog over the operator page, whose own
    # breadcrumb ends in a link (tests/test_states.py checks the page behind).
    "ledger:contacts:outcome": lambda w: (["Operators", "Walk Co"], "Kept"),
}
#: The pages with no parent: the lists, the dashboard, the pages of the navigation.
NO_PARENT = (
    "ledger:home",
    "ledger:stores:list",
    "ledger:operators:list",
    "ledger:leases:list",
    "ledger:payments:list",
    "ledger:returns:list",
    "ledger:arrears:page",
    "ledger:reports:index",
    "ledger:webhooks:list",
    "ledger:disputes:page",
    "ledger:reconciliation:page",
    "ledger:audit:page",
    "ledger:settings:index",
    "ledger:outbox:page",
    "ledger:search",
    "finance:settings",
    "finance:help",
    # PLAN-012f-b E-3: a help topic's header holds the title only (DESIGN-SPEC §4 Help); the
    # topic card beside the article links the other topics.
    "finance:help_topic",
)


def test_the_pages_with_a_parent_carry_its_breadcrumb(world: World) -> None:
    clients: dict[Role, Client] = {}
    for name, expected in BREADCRUMBS.items():
        client = client_of(world, name, clients)
        body = client.get(path_of(world, name)).content.decode()
        links, current = crumbs(body)
        parents, own = expected(world)
        assert [text for _, text in links] == parents, name
        assert current == own, name
        # Each parent is a link to a page that opens.
        for href, _ in links:
            assert client.get(href).status_code == 200, (name, href)


def test_the_pages_without_a_parent_have_no_breadcrumb(world: World) -> None:
    client = client_for(Role.ADMIN)[0]
    for name in NO_PARENT:
        body = client.get(path_of(world, name)).content.decode()
        assert 'class="breadcrumb"' not in header_of(body), name


def test_daily_charges_names_its_section_without_a_link(world: World) -> None:
    # PLAN-012f-b E-2: the spec draws "Admin ›" before the title; Admin is a group of the
    # navigation, not a page, so the crumb is text.
    client = client_for(Role.ADMIN)[0]
    header = header_of(client.get(path_of(world, "ledger:run:today")).content.decode())
    match = re.search(
        r'<nav class="breadcrumb" aria-label="Breadcrumb" id="breadcrumb">(.*?)</nav>', header, re.S
    )
    assert match is not None
    assert "<a " not in match.group(1)
    assert re.sub(r"<[^>]+>", "", match.group(1)).split() == ["Admin", "›"]


def test_a_store_parent_is_named_by_its_number_or_its_label() -> None:
    lease = a_lease(a_store("NO NUMBER PLAZA"), an_operator("Plain Co"))
    charge = a_rent_charge(lease, date(2026, 10, 1))
    links, _ = crumbs(client_for(Role.VIEWER)[0].get(f"/charges/{charge.pk}/").content.decode())
    assert [text for _, text in links] == ["Stores", lease.store.external_label, "Charges"]
    assert links[1][0] == f"/stores/{lease.store_id}/"
    assert links[2][0] == f"/stores/{lease.store_id}/?tab=charges#tab-charges"


# --- Forms and wizards on a phone --------------------------------------------------------------


def phone_block(css: str) -> str:
    return css.split("@media (max-width: 768px) {", 1)[1].split("\n}\n", 1)[0]


def test_a_forms_primary_button_is_sticky_above_the_tab_bar_on_a_phone() -> None:
    block = phone_block(APP_CSS.read_text(encoding="utf-8"))
    assert (
        ".form-actions { position: sticky; bottom: calc(90px + env(safe-area-inset-bottom));"
    ) in block
    # One field per row: the form card's controls take the width.
    assert ".form-card select, .form-card textarea { width: 100%; }" in block


def test_every_form_card_ends_in_one_primary_button(world: World) -> None:
    clients: dict[Role, Client] = {}
    found = 0
    for name in FORM_PAGES:
        client = client_of(world, name, clients)
        body = client.get(path_of(world, name)).content.decode()
        # PLAN-012f-a D5: the form family's primary is in the summary card beside the cards.
        blocks = re.findall(r'<section class="card form-card">(.*?)</section>', body, re.S)
        blocks += re.findall(r'<aside class="form-summary"[^>]*>(.*?)</aside>', body, re.S)
        for card in blocks:
            actions = re.findall(r'<div class="form-actions">(.*?)</div>', card, re.S)
            for block in actions:
                found += 1
                assert block.count("btn-primary") == 1, (name, block)
        # PLAN-012f-b Batch B: a form dialog's primary is in its foot. E-4: a confirmation's
        # primary is red when it ends or removes something; a refusal's foot is "Keep …" alone.
        for foot in re.findall(r'<div class="dialog-foot">(.*?)</div>', body, re.S):
            found += 1
            primaries = len(re.findall(r'class="btn btn-(?:primary|danger)"', foot))
            assert primaries == (1 if 'type="submit"' in foot else 0), (name, foot)
    assert found >= 20


def test_a_wizard_step_shows_step_n_of_m_on_a_phone(world: World) -> None:
    admin = client_for(Role.ADMIN)[0]
    body = admin.get(reverse("ledger:lease_ends:statement", kwargs={"pk": world.end.pk})).text
    steps = body[
        body.index('id="wizard-steps"') : body.index("</ol>", body.index('id="wizard-steps"'))
    ]
    count = steps.count("<li")
    current = re.findall(r"<li[^>]*>(.*?)</li>", steps, re.S)
    number = next(i for i, item in enumerate(current, 1) if "<strong>" in item)
    label = re.sub(r"<[^>]+>", "", current[number - 1]).strip()
    assert (
        f'<p class="steps-line" id="wizard-step-line">Step {number} of {count} '
        f'<span class="steps-name">· {label}</span></p>'
    ) in body
    assert f'<progress class="steps-progress" value="{number}" max="{count}"' in body
    block = phone_block(APP_CSS.read_text(encoding="utf-8"))
    assert ".steps { display: none; }" in block and ".steps-line { display: block; }" in block
    css = APP_CSS.read_text(encoding="utf-8")
    assert ".steps-line { display: none;" in css.split("@media", 1)[0]  # laptops: the pills


def test_the_step_line_escapes_its_label_and_is_empty_without_a_current_step() -> None:
    template = Template("{% load components %}{% step_line steps %}")
    rendered = template.render(
        Context(
            {"steps": [("Terms", "/a/", False), ("<b>Bad</b>", None, True), ("End", None, False)]}
        )
    )
    assert "Step 2 of 3" in rendered and "&lt;b&gt;Bad&lt;/b&gt;" in rendered
    assert "<b>" not in rendered
    assert template.render(Context({"steps": [("Terms", "/a/", False)]})) == ""
    assert template.render(Context({"steps": None})) == ""


def test_the_dashboards_attention_rows_are_cards_with_a_full_width_button_on_a_phone() -> None:
    block = phone_block(APP_CSS.read_text(encoding="utf-8"))
    assert ".kpi-row { grid-template-columns: repeat(2, minmax(0, 1fr));" in block
    # PLAN-012g C5: the row's button sits at the right of the why badge, its own width.
    button = ".attention-table td.attention-action > .btn { display: inline-flex; width: auto; }"
    assert button in block
    # PLAN-012f-b A-3: the dashboard's two-thirds rows are .dashboard-row-thirds.
    thirds = ".dashboard-row-thirds, .workspace-grid { grid-template-columns: minmax(0, 1fr);"
    assert thirds in block


# --- The login page (D13) ---------------------------------------------------------------------


@pytest.fixture
def login_page() -> str:
    row = CompanySettings.current()
    row.name = COMPANY
    row.save()
    response = Client().get("/account/login/")
    assert response.status_code == 200
    return response.content.decode()


def test_the_login_page_has_the_two_panels_of_the_reference(login_page: str) -> None:
    # Re-pinned for PLAN-014 I-b (the owner's redesign; tests/test_login_page.py holds the rest).
    assert '<body class="auth">' in login_page
    brand = login_page[login_page.index('id="auth-brand"') : login_page.index('id="auth-main"')]
    assert 'class="auth-mosaic" id="auth-mosaic" aria-hidden="true"' in brand
    assert "auth-shape" not in login_page
    assert 'alt="Monvera" height="36"' in brand
    assert ">Rent collection for every store, in one place.</p>" in brand
    cards = re.findall(r"<strong>([^<]+)</strong>", brand)
    assert cards == ["Monthly overview", "Bank debits", "Reminders", "Promises"]
    assert "Autopay and one-off debits through Stax." in brand
    assert f'<p class="auth-company" id="auth-company">{COMPANY} · Monvera</p>' in brand
    main = login_page[login_page.index('id="auth-main"') :]
    assert '<h1 id="sign-in-heading">Sign in to Monvera</h1>' in main
    assert f'<p class="auth-sub" id="auth-sub">Staff of {COMPANY} only.</p>' in main
    assert (
        '<p class="auth-note" id="auth-note">Access is by invitation. Ask an admin if you need '
        "an account.</p>"
    ) in main
    # The tab title stays the package's word.
    assert "<title>[SANDBOX] Monvera · Login</title>" in login_page


def test_the_login_form_keeps_its_fields_names_and_flow(login_page: str) -> None:
    assert '<label for="id_auth-username">Username</label>' in login_page
    assert '<label for="id_auth-password">Password</label>' in login_page
    assert 'name="auth-username"' in login_page and 'autocomplete="username"' in login_page
    assert 'name="auth-password"' in login_page and 'autocomplete="current-password"' in login_page
    assert 'name="login_view-current_step" value="auth"' in login_page
    assert '<input type="submit" value="" hidden />' in login_page  # the Enter key
    actions = login_page.split('<div class="auth-actions">', 1)[1].split("</div>", 1)[0]
    # PLAN-014 I-b: the password step's one button reads "Sign in"; no Next, no Cancel.
    assert (
        actions
        == '<button type="submit" class="btn btn-primary" id="sign-in-button">Sign in</button>'
    )
    assert ">Cancel<" not in login_page and ">Next<" not in login_page
    lowered = login_page.lower()
    assert "<script" in lowered and "http://" not in lowered.replace("http://www.w3.org", "")


def test_the_company_name_on_the_login_page_is_escaped() -> None:
    row = CompanySettings.current()
    row.name = '<script>alert("x")</script> Co'
    row.save()
    body = Client().get("/account/login/").content.decode()
    assert '<script>alert("x")</script>' not in body
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; Co · Monvera" in body


def test_the_login_layout_rules() -> None:
    # Re-pinned for PLAN-014 I-b: the navy panel the same in both themes, the mosaic, the cards,
    # the 440 px form area, the credentials box and the pill banner.
    css = APP_CSS.read_text(encoding="utf-8")
    assert ".auth-brand { grid-column: 1; grid-row: 1 / -1;" in css
    assert "background: var(--navy); color: var(--login-text); }" in css
    assert "transform: rotate(-8deg);" in css
    assert ".auth-logo img { display: block; height: 36px;" in css
    headline = css.split(".auth-headline {", 1)[1].split("}", 1)[0]
    assert "font-size: 36px; font-weight: 700;" in headline
    feature = css.split(".auth-feature {", 1)[1].split("}", 1)[0]
    assert "border: 1px solid var(--login-card-border); border-radius: 14px;" in feature
    assert "backdrop-filter: blur(6px);" in feature
    main = css.split(".auth-main {", 1)[1].split("}", 1)[0]
    assert "grid-row: 2;" in main and "width: min(440px, calc(100% - 32px));" in main
    assert ".auth-main .btn { padding: 13px; border-radius: 12px;" in css
    banner = css.split("body.auth > #sandbox-banner {", 1)[1].split("}", 1)[0]
    assert "position: static !important;" in banner
    assert "border-radius: var(--radius-pill);" in banner
    tokens = (BACKEND / "finance" / "static" / "finance" / "tokens.css").read_text()
    assert "--login-gradient" not in tokens.split("*/", 1)[1]
    assert "auth-shape" not in css


def test_the_login_page_on_a_phone_is_a_short_header_over_the_form() -> None:
    block = phone_block(APP_CSS.read_text(encoding="utf-8"))
    assert "body.auth { grid-template-columns: minmax(0, 1fr) auto;" in block
    assert ".auth-brand { grid-column: 1 / -1; grid-row: 1; min-height: 0;" in block
    assert ".auth-sentence, .auth-features, .auth-company { display: none; }" in block
    assert ".auth-headline { margin: 0; font-size: 22px; }" in block
    assert ".auth-main { grid-column: 1 / -1; grid-row: 3;" in block


def test_the_token_step_takes_the_same_right_panel() -> None:
    from tests.auth_helpers import TEST_PASSWORD, make_user

    make_user("panel-user", Role.VIEWER)
    body = (
        Client()
        .post(
            "/account/login/",
            {
                "login_view-current_step": "auth",
                "auth-username": "panel-user",
                "auth-password": TEST_PASSWORD,
            },
        )
        .content.decode()
    )
    assert 'id="id_token-otp_token"' in body and 'autocomplete="one-time-code"' in body
    assert '<body class="auth">' in body and 'id="auth-brand"' in body
    assert '<h1 id="sign-in-heading">Sign in to Monvera</h1>' in body
    actions = body.split('<div class="auth-actions">', 1)[1].split("</div>", 1)[0]
    assert 'value="auth"' in actions and ">Back</button>" in actions  # the way back to step 1
    assert '<button type="submit" class="btn btn-primary">Next</button>' in actions
    assert ">Cancel</a>" in actions  # PLAN-014 I-b: the token step keeps Next and Cancel


# --- The error pages wear the shell -------------------------------------------------------------


def test_a_signed_in_users_404_and_403_are_shell_pages_and_an_anonymous_404_is_not(
    world: World,
) -> None:
    viewer = client_for(Role.VIEWER)[0]
    missing = viewer.get("/stores/00000000-0000-0000-0000-000000000000/")
    assert missing.status_code == 404
    body = missing.content.decode()
    assert 'id="sidebar"' in body and 'id="tab-bar"' in body
    assert '<div class="page-header"><h1>Not found</h1></div>' in header_of(body)
    assert '<section class="card error-card" id="error-page">' in body
    refused = viewer.get("/audit/")
    assert refused.status_code == 403
    body = refused.content.decode()
    assert '<div class="page-header"><h1>Not allowed</h1></div>' in header_of(body)
    assert "Your role does not allow this action." in body
    # A 404 with nobody signed in (a public route's own 404 never renders a page; Django's
    # handler renders this template) shows no navigation.
    from django.contrib.auth.models import AnonymousUser
    from django.template.loader import render_to_string
    from django.test import RequestFactory

    request = RequestFactory().get("/nowhere/")
    request.user = AnonymousUser()
    anonymous = render_to_string("404.html", request=request)
    assert 'id="sidebar"' not in anonymous and '<body class="auth">' in anonymous
    assert "<h1>Not found</h1>" in anonymous

"""PLAN-012e-2 Batch C (docs/qa/PLAN-012e2-fewer-actions.md D7 to D10, test plan §6 C): the
store's details (one Address line, the status as a badge, the Closed date only when set),
the Store cost block (rent, lease end, other cost only when set, Change for billing and admin, the
History behind a <details>, no margin and no rule paragraph), the New store form's first
cost row (from the open date, audited as "Change" audits it; the Edit form has no cost
field), the importer's columns unchanged, and the help pages (every role, 401 anonymous,
the user menu's Help link, the moved paragraphs on their pages)."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from django.test import Client
from django.utils import timezone

from accounts import registry
from accounts.roles import Role
from finance import help as help_pages
from ledger.forms import StoreCreateForm, StoreForm
from ledger.models import AuditLog, Store, StoreCost, StoreCostSource, StoreStatus
from ledger.services import store_costs
from ledger.services import stores as service
from tests.test_operator_views import client_for
from tests.test_store_views import form_data

pytestmark = pytest.mark.django_db

NEW = "/stores/new/"


def a_store(**fields: Any) -> Store:
    values: dict[str, Any] = {"external_label": "Store 2620", "status": StoreStatus.ACTIVE}
    return Store.objects.create(**{**values, **fields})


def page(client: Client, store: Store) -> str:
    return client.get(f"/stores/{store.pk}/").content.decode()


def block(html: str, marker: str, end: str = "</section>") -> str:
    return html.split(marker, 1)[1].split(end, 1)[0]


# --- D7: the address line -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("parts", "line"),
    [
        (
            ("12 Main Street", "Springfield", "IL", "62701"),
            "12 Main Street, Springfield, Illinois 62701",
        ),
        (("", "Springfield", "IL", "62701"), "Springfield, Illinois 62701"),
        (("12 Main Street", "", "", "62701"), "12 Main Street, 62701"),
        (("12 Main Street", "Springfield", "", ""), "12 Main Street, Springfield"),
        (("", "", "IL", ""), "Illinois"),
        (("  12   Main St ", " Springfield ", "IL", ""), "12 Main St, Springfield, Illinois"),
        (("", "", "", ""), "—"),
    ],
)
def test_the_address_line_joins_the_parts_it_has(parts: tuple[str, ...], line: str) -> None:
    street, city, state, postal = parts
    store = Store(address_line1=street, city=city, state=state, postal_code=postal)
    assert service.address_line(store) == line


def test_the_page_shows_one_address_line_escaped_and_no_city_or_state_rows() -> None:
    store = a_store(
        address_line1="12 <b>Main</b> Street", city="Springfield", state="IL", postal_code="62701"
    )
    html = page(client_for(Role.VIEWER)[0], store)
    assert (
        '<dd id="store-address">12 &lt;b&gt;Main&lt;/b&gt; Street, Springfield, Illinois 62701</dd>'
        in (html)
    )
    details = block(html, 'id="store-details-card"')
    assert "<dt>City</dt>" not in details and "<dt>State</dt>" not in details
    assert "<dt>Name</dt>" not in details
    empty = page(client_for(Role.VIEWER)[0], a_store(external_label="Store 2621"))
    assert '<dd id="store-address">—</dd>' in empty


def test_the_store_form_asks_street_city_state_and_zip_and_saves_them() -> None:
    admin, _ = client_for(Role.ADMIN)
    form = admin.get(NEW).content.decode()
    for name, label in (
        ("address_line1", "Street"),
        ("city", "City"),
        ("state", "State"),
        ("postal_code", "ZIP"),
    ):
        assert f'name="{name}"' in form and f">{label}" in form, name
    response = admin.post(
        NEW, form_data(address_line1="12 Main Street", postal_code="62701", landlord_rent="10.00")
    )
    assert response.status_code == 302
    store = Store.objects.get()
    assert (store.address_line1, store.postal_code) == ("12 Main Street", "62701")
    created = AuditLog.objects.get(action="store.created")
    assert created.after is not None
    assert (created.after["address_line1"], created.after["postal_code"]) == (
        "12 Main Street",
        "62701",
    )


def test_an_edit_of_the_address_is_audited_before_and_after() -> None:
    store = a_store(open_date=date(2025, 3, 1), address_line1="1 Old Road")
    admin, _ = client_for(Role.ADMIN)
    data = {
        "store_number": "",
        "external_label": store.external_label,
        "address_line1": "2 New Road",
        "city": "",
        "state": "",
        "postal_code": "62702",
        "open_date": "2025-03-01",
        "closed_at": "",
        "status": StoreStatus.ACTIVE,
    }
    assert admin.post(f"/stores/{store.pk}/edit/", data).status_code == 302
    entry = AuditLog.objects.get(action="store.updated")
    assert entry.before is not None and entry.after is not None
    assert entry.before["address_line1"] == "1 Old Road"
    assert (entry.after["address_line1"], entry.after["postal_code"]) == ("2 New Road", "62702")


# --- D7: the status badge and the dates -------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "markup"),
    [
        # PLAN-012f D6: "attention" is the unpaid family.
        (StoreStatus.ACTIVE, '<span class="badge badge-paid">'),
        (StoreStatus.CLOSED, '<span class="badge badge-unpaid">'),
        (StoreStatus.TERMINATED, '<span class="badge badge-unpaid">'),
        (StoreStatus.PROSPECTIVE, '<span class="badge">Prospective</span>'),
    ],
)
def test_the_status_is_a_badge_per_status(status: str, markup: str) -> None:
    store = a_store(status=status)
    status_cell = block(page(client_for(Role.VIEWER)[0], store), 'id="store-status">', "</dd>")
    assert status_cell.startswith(markup), status_cell
    assert StoreStatus(status).label in status_cell


def test_every_store_status_has_a_badge_mapping_or_is_plain() -> None:
    assert set(service.STATUS_BADGES) <= set(StoreStatus.values)
    assert service.status_badge(Store(status=StoreStatus.PROSPECTIVE)) == ""


def test_the_closed_date_shows_only_when_set_and_needs_open_date_stays() -> None:
    open_store = a_store()
    html = page(client_for(Role.VIEWER)[0], open_store)
    assert "Closed date" not in block(html, 'id="store-details-card"')
    assert "Needs open date" in block(html, 'id="store-details-card"')
    closed = a_store(
        external_label="Store 2622",
        status=StoreStatus.CLOSED,
        open_date=date(2025, 1, 1),
        closed_at=date(2026, 6, 30),
    )
    html = page(client_for(Role.VIEWER)[0], closed)
    assert '<dt>Closed date</dt><dd id="store-closed-date">Jun 30, 2026</dd>' in html


def test_edit_stays_in_the_details_block_and_set_landlord_rent_left_it() -> None:
    store = a_store()
    html = page(client_for(Role.ADMIN)[0], store)
    details = block(html, 'id="store-details-card"')
    assert f'href="/stores/{store.pk}/edit/"' in details
    assert "Set landlord rent" not in html and 'id="set-landlord-rent"' not in html


# --- D8: the Store cost block -----------------------------------------------------------------


def set_cost(store: Store, rent: str, **fields: Any) -> None:
    outcome = store_costs.set_cost(store, Decimal(rent), date(2026, 1, 1), **fields)
    assert outcome.ok, outcome.message


def test_the_store_cost_block_lines_and_no_margin_or_rule_paragraph() -> None:
    store = a_store()
    set_cost(store, "3500.00", landlord_lease_end=date(2030, 12, 31))
    html = page(client_for(Role.VIEWER)[0], store)
    cost = block(html, '<section class="card" id="store-cost"')
    assert "<h2" in cost and ">Store cost</h2>" in cost
    assert '<dd id="landlord-rent">$3,500.00 a month from Jan 1, 2026</dd>' in cost
    assert '<dd id="landlord-lease-end">Dec 31, 2030</dd>' in cost
    assert "Other monthly cost" not in cost
    assert cost.count('<p class="help">') == 1
    assert (
        '<p class="help">Learn more: <a href="/help/store-costs/">Store costs and margin</a></p>'
        in (cost)
    )
    for gone in (
        "Costs and margin",
        "Margin",
        "Operator rent",
        'id="cost-rules"',
        "Months after a closed row",
        "less the landlord rent",
    ):
        assert gone not in html, gone


def test_the_other_cost_shows_only_when_set_with_its_description() -> None:
    store = a_store()
    set_cost(store, "3500.00", other_monthly_cost=Decimal("50.00"), other_cost_description="bins")
    cost = block(page(client_for(Role.VIEWER)[0], store), 'id="store-cost"')
    assert '<dt>Other monthly cost</dt><dd id="other-cost">$50.00 (bins)</dd>' in cost


def test_not_entered_without_a_row_and_no_cost_at_zero() -> None:
    store = a_store()
    cost = block(page(client_for(Role.VIEWER)[0], store), 'id="store-cost"')
    # PLAN-012f-b D12: no row reads "Not entered" (was "unknown").
    assert '<dd id="landlord-rent">Not entered</dd>' in cost
    assert '<dd id="landlord-lease-end">unknown</dd>' in cost
    set_cost(store, "0.00")
    cost = block(page(client_for(Role.VIEWER)[0], store), 'id="store-cost"')
    assert "$0.00 a month (no cost) from Jan 1, 2026" in cost


@pytest.mark.parametrize(
    ("role", "shown"), [(Role.ADMIN, True), (Role.BILLING, True), (Role.VIEWER, False)]
)
def test_change_is_one_link_to_the_cost_form_for_billing_and_admin(role: Role, shown: bool) -> None:
    store = a_store()
    cost = block(page(client_for(role)[0], store), 'id="store-cost"')
    # PLAN-012f D8: Change is the STORE COST card's right link.
    link = f'<a class="card-link" href="/stores/{store.pk}/cost/" id="change-cost">Change</a>'
    assert (link in cost) is shown
    assert cost.count("<a class=") == (1 if shown else 0)


def test_the_history_is_behind_a_details_below_the_block() -> None:
    store = a_store()
    set_cost(store, "3500.00")
    html = page(client_for(Role.ADMIN)[0], store)
    after = html.split('id="store-cost"', 1)[1].split("</section>", 1)[1]
    details = block(after, '<details id="cost-history">', "</details>")
    assert "<summary>History</summary>" in details
    assert 'id="cost-history-table"' in details and 'id="close-cost-form"' in details
    assert 'id="cost-history"' not in block(html, 'id="store-cost"')


def test_the_store_view_passes_no_margin() -> None:
    store = a_store()
    response = client_for(Role.VIEWER)[0].get(f"/stores/{store.pk}/")
    assert response.context is not None
    assert "margin" not in response.context and "operator_rent" not in response.context


# --- D9: the New store form's first cost row ------------------------------------------------------


def test_the_new_store_form_creates_the_first_cost_row_from_the_open_date() -> None:
    admin, user = client_for(Role.ADMIN)
    data = form_data(
        open_date="2025-03-01",
        landlord_rent="3500.00",
        landlord_lease_end="2030-12-31",
        other_monthly_cost="50.00",
        other_cost_description="bins",
    )
    response = admin.post(NEW, data)
    assert response.status_code == 302
    store = Store.objects.get()
    [row] = StoreCost.objects.filter(store=store)
    assert row.effective_from == date(2025, 3, 1) and row.effective_to is None
    assert row.landlord_rent == Decimal("3500.00")
    assert row.landlord_lease_end == date(2030, 12, 31)
    assert (row.other_monthly_cost, row.other_cost_description) == (Decimal("50.00"), "bins")
    assert row.source == StoreCostSource.STAFF
    [entry] = AuditLog.objects.filter(entity_type="store_cost")
    assert entry.action == "store_cost.created" and entry.actor == user
    assert entry.entity_id == str(row.pk) and entry.before is None
    assert entry.after is not None and entry.after["effective_from"] == "2025-03-01"
    cost = block(admin.get(response["Location"]).content.decode(), 'id="store-cost"')
    assert "$3,500.00 a month from Mar 1, 2025" in cost


def test_the_audit_row_is_the_one_change_writes() -> None:
    admin, _ = client_for(Role.ADMIN)
    admin.post(NEW, form_data(landlord_rent="10.00"))
    created = AuditLog.objects.get(entity_type="store_cost")
    other = a_store(external_label="Store 2623")
    admin.post(
        f"/stores/{other.pk}/cost/",
        {"landlord_rent": "10.00", "effective_from": "2025-03-01", "notes": ""},
    )
    changed = AuditLog.objects.filter(entity_type="store_cost").exclude(pk=created.pk).get()
    assert created.action == changed.action
    assert created.after is not None and changed.after is not None
    assert set(created.after) == set(changed.after)


def test_without_an_open_date_the_first_row_starts_today() -> None:
    store, created = service.create_store(
        {"external_label": "Store 2624", "status": StoreStatus.ACTIVE, "landlord_rent": Decimal(0)},
        creation_key=uuid.uuid4(),
        actor=None,
    )
    assert created and store.open_date is None
    [row] = StoreCost.objects.filter(store=store)
    assert row.effective_from == timezone.localdate()


def test_a_cost_row_the_service_refuses_saves_no_store() -> None:
    with pytest.raises(service.CostRefused) as refused:
        service.create_store(
            {
                "external_label": "Store 2625",
                "status": StoreStatus.ACTIVE,
                "landlord_rent": Decimal("-1.00"),
            },
            creation_key=uuid.uuid4(),
            actor=None,
        )
    assert refused.value.outcome.message == store_costs.RENT_NEGATIVE
    assert not Store.objects.exists() and not StoreCost.objects.exists()
    assert not AuditLog.objects.filter(entity_type__in=("store", "store_cost")).exists()


def test_the_landlord_rent_is_optional_on_the_new_store_form() -> None:
    # PLAN-012f-b D12 (was required, PLAN-012e-2 D9): empty saves the store, no cost row.
    assert StoreCreateForm(data=form_data(landlord_rent="")).is_valid()
    assert StoreCreateForm(data=form_data(landlord_rent="0.00")).is_valid()
    assert not StoreCreateForm(data=form_data(landlord_rent="-1.00")).is_valid()
    admin, _ = client_for(Role.ADMIN)
    response = admin.post(NEW, form_data(landlord_rent=""))
    store = Store.objects.get()
    assert (response.status_code, response["Location"]) == (302, f"/stores/{store.pk}/")
    assert not StoreCost.objects.exists()


def test_the_new_store_form_groups_the_cost_fields_and_the_edit_form_has_none() -> None:
    admin, _ = client_for(Role.ADMIN)
    new = admin.get(NEW).content.decode()
    # PLAN-012f-b Batch B: the group is its own fieldset in the New store dialog.
    fieldset = block(
        new, '<fieldset id="store-cost-fields" class="dialog-fieldset">', "</fieldset>"
    )
    assert "<legend>Store cost</legend>" in fieldset
    for name in StoreCreateForm.COST_FIELDS:
        assert f'name="{name}"' in fieldset, name
    store = a_store(open_date=date(2025, 3, 1))
    edit = admin.get(f"/stores/{store.pk}/edit/").content.decode()
    assert 'id="store-cost-fields"' not in edit
    for name in StoreCreateForm.COST_FIELDS:
        assert f'name="{name}"' not in edit, name
        assert name not in StoreForm.base_fields, name


def test_an_edit_never_touches_the_cost_rows() -> None:
    store = a_store(open_date=date(2025, 3, 1))
    set_cost(store, "3500.00")
    admin, _ = client_for(Role.ADMIN)
    data = {
        "store_number": "",
        "external_label": store.external_label,
        "address_line1": "",
        "city": "X",
        "state": "",
        "postal_code": "",
        "open_date": "2025-03-01",
        "closed_at": "",
        "status": StoreStatus.ACTIVE,
        "landlord_rent": "1.00",
    }
    assert admin.post(f"/stores/{store.pk}/edit/", data).status_code == 302
    [row] = StoreCost.objects.filter(store=store)
    assert row.landlord_rent == Decimal("3500.00")


def test_the_importer_keeps_its_columns() -> None:
    assert store_costs.REQUIRED_COLUMNS == (
        "store_no",
        "walmart_monthly_rent",
        "walmart_rent_effective_from",
        "walmart_lease_end",
        "other_monthly_cost",
        "other_cost_description",
        "notes",
    )
    assert store_costs.REFERENCE_COLUMNS == (
        "location",
        "state",
        "operator (ref only)",
        "operator rent (ref only)",
    )


# --- D10: the help pages ----------------------------------------------------------------------

TOPIC_PATHS = [f"/help/{topic.slug}/" for topic in help_pages.TOPICS]


@pytest.mark.parametrize("role", [Role.VIEWER, Role.BILLING, Role.ADMIN])
def test_the_help_index_and_every_topic_answer_every_role(role: Role) -> None:
    client, _ = client_for(role)
    index = client.get("/help/")
    assert index.status_code == 200
    html = index.content.decode()
    assert "Monvera · Help</title>" in html
    for topic in help_pages.TOPICS:
        assert f'href="/help/{topic.slug}/"' in html and topic.title in html
    for path in TOPIC_PATHS:
        response = client.get(path)
        assert response.status_code == 200, path
        text = response.content.decode()
        assert "Monvera · Help</title>" in text, path
        # The topic itself has no form (the shell's Sign out, after the page since PLAN-012f
        # D4's bottom bar, is not the topic's).
        article = text.split('id="help-topic"', 1)[1].split("</article>", 1)[0]
        assert 'method="post"' not in article, path


@pytest.mark.parametrize("path", ["/help/", *TOPIC_PATHS])
def test_the_help_pages_answer_401_anonymous(path: str) -> None:
    assert Client().get(path).status_code == 401


def test_a_user_without_a_role_is_refused_and_a_post_is_refused() -> None:
    client, _ = client_for(None)
    assert client.get("/help/").status_code == 403
    viewer, _ = client_for(Role.VIEWER)
    assert viewer.post("/help/").status_code in (403, 405)
    assert viewer.post("/help/store-costs/").status_code in (403, 405)


def test_an_unknown_topic_is_404() -> None:
    viewer, _ = client_for(Role.VIEWER)
    assert viewer.get("/help/no-such-topic/").status_code == 404
    assert viewer.get("/help/Store-Costs/").status_code == 404


def test_the_routes_are_read_only_in_the_registry() -> None:
    for name in ("finance:help", "finance:help_topic"):
        assert name in registry.READ_ONLY_ROUTE_NAMES
        assert registry.rule_for(name) is None


def test_the_user_menu_links_help() -> None:
    # PLAN-012f D3: the sidebar's user card (name, role, Help, Sign out); D4: the More sheet.
    html = client_for(Role.VIEWER)[0].get("/").content.decode()
    menu = block(html, 'id="user-card"', "</form>")
    assert '<a href="/help/" id="nav-help">Help</a>' in menu
    assert (
        menu.index('id="user-menu-name"')
        < menu.index('id="nav-help"')
        < menu.index('action="/account/logout/"')
    )
    more = block(html, 'id="more-sheet"', "</details>")
    assert 'href="/help/"' in more
    on_help = client_for(Role.VIEWER)[0].get("/help/store-costs/").content.decode()
    assert '<a href="/help/" id="nav-help" aria-current="page">Help</a>' in on_help


@pytest.mark.parametrize(
    ("slug", "texts"),
    [
        (
            "store-costs",
            (
                "Margin is the operator rent of the lease active today less the landlord rent",
                'Months after a closed row have no cost row and read "unknown".',
                'that reads "no cost", a known zero.',
                "The same effective date as the current row corrects it in place.",
            ),
        ),
        (
            "collecting-an-invoice",
            (
                "Save draft",
                "Send by e-mail",
                "Charge now",
                "Schedule for a date",
                "Record payment now",
                "Set up autopay",
                "The plan appears on the store's Autopay tab",
                "Rent is never set up this way.",
                "A payment on this invoice pays the lines in order",
            ),
        ),
        ("invoice-lines", ("Rent: choose Rent in the Item box", "Existing open charge: the line")),
        (
            "autopay-and-daily-charges",
            (
                "Creating charges also catches up",
                "autopay invoices only",
                "marks an amount above",
                "Debit again takes the date the operator agreed",
            ),
        ),
        ("promises-to-pay", ("Still open records this entry",)),
        ("lease-ends-and-transfers", ("The wizard hands the store to another operator",)),
        ("needs-review", ("Needs review",)),
    ],
)
def test_the_moved_paragraphs_are_on_their_help_page(slug: str, texts: tuple[str, ...]) -> None:
    html = client_for(Role.VIEWER)[0].get(f"/help/{slug}/").content.decode()
    html = html.replace("&quot;", '"').replace("&#x27;", "'")
    for text in texts:
        assert text in html, text


def test_the_moved_paragraphs_left_their_pages() -> None:
    billing, _ = client_for(Role.BILLING)
    run = billing.get("/run/").content.decode()
    assert 'id="catch-up-scope"' not in run and 'id="match-scope"' not in run
    assert 'id="large-debit-note"' not in run
    assert 'href="/help/autopay-and-daily-charges/"' in run
    store = a_store()
    cost_form = billing.get(f"/stores/{store.pk}/cost/").content.decode()
    assert "corrects it in place" not in cost_form
    assert 'href="/help/store-costs/"' in cost_form

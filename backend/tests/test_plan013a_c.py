"""PLAN-013a §3 C (Batch C, the cleanup): the unit and page cases of C1–C7.

C1: every "by" cell shows the staff member through ``|staff`` (the full name, the e-mail
as its ``title``), never the username. C2: the Refunds card is hidden on a payment
recorded outside the processor. C3: the invoice builder's greyed bank-debit tile. C4: the
today's-debit note leaves the session on the next GET of the charge or Add payment page.
C5: one ``statement_first``. C6: the list family's phone meta line (the stylesheet rule).
C7: the statement step's script is loaded (the live tile is the e2e test's).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client, override_settings
from django.urls import reverse
from django.utils.html import escape

import documents_storage
from accounts.roles import Role
from documents_storage.memory import MemoryDocumentStorage
from ledger.models import (
    AuditLog,
    ChargeNote,
    Note,
    NoteEntityType,
    OneOffCharge,
    PaymentChannel,
    RentCharge,
)
from ledger.services import invoice_builder, store_costs
from ledger.services import one_off_charges as one_off_service
from payments.fake import FakeProvider
from tests import test_builder_save_prompt as builder
from tests import test_collections as tc
from tests import test_lease_end as le
from tests import test_rent_debit as rd
from tests import test_transfer as tt
from tests.document_files import PNG
from tests.receivables_helpers import a_lease, a_store
from tests.receivables_helpers import an_operator as a_plain_operator
from tests.test_attention import balances
from tests.test_collections import clock  # noqa: F401 (fixture)
from tests.test_documents import do_upload
from tests.test_how_to_collect import a_draft, one_line
from tests.test_lease_services import make_lease
from tests.test_ledger_models import new_lease, new_rent_charge, new_store, saved
from tests.test_one_off_charges import an_operator
from tests.test_operator_views import client_for
from tests.test_plan012fc_e import a_zelle_payment, name_provider, payment_page
from tests.test_refunds_voids import ach
from tests.test_rent_run import health  # noqa: F401 (autouse fixture)
from tests.test_store_costs import set_cost
from tests.test_store_costs import store as cost_store
from tests.test_webhook_handlers import make_world

pytestmark = pytest.mark.django_db

BACKEND = Path(__file__).resolve().parents[1]
VIEWS = BACKEND / "ledger" / "views"
APP_CSS = BACKEND / "finance" / "static" / "finance" / "app.css"

FIRST, LAST, EMAIL = "Pat", "Lee", "pat.lee@example.com"
NAME = f"{FIRST} {LAST}"
TITLE = f'<span title="{EMAIL}">{NAME}</span>'


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch) -> MemoryDocumentStorage:
    memory = MemoryDocumentStorage()
    monkeypatch.setattr(documents_storage, "get_storage", lambda: memory)
    return memory


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider(pay_invoice_enabled=True)
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


@pytest.fixture
def enabled() -> Iterator[None]:
    with override_settings(STAX_CALLER="worker", STAX_PAY_INVOICE_ENABLED=True):
        yield


@pytest.fixture
def builder_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("django.utils.timezone.now", lambda: builder.NOW)


def named(role: Role) -> tuple[Client, User]:
    """A signed-in user with a full name and an e-mail that does not hold the username."""
    client, user = client_for(role)
    user.first_name, user.last_name, user.email = FIRST, LAST, EMAIL
    user.save(update_fields=["first_name", "last_name", "email"])
    return client, user


def body(client: Client, url: str) -> str:
    response = client.get(url)
    assert response.status_code == 200
    return str(response.content.decode())


def between(page: str, start: str, end: str) -> str:
    part = page[page.index(start) :]
    return part[: part.index(end)]


def shows_the_name_not_the_username(cell: str, user: User) -> None:
    assert TITLE in cell
    assert user.get_username() not in cell


# --- C1: the "by" cells -------------------------------------------------------------------


def test_c1_the_cost_history_names_the_admin_who_superseded_a_row() -> None:
    target = cost_store()
    outcome = set_cost(target, "2500.00", date(2026, 1, 1))
    assert outcome.cost is not None
    old = outcome.cost
    client, user = named(Role.ADMIN)
    response = client.post(
        reverse("ledger:stores:correct_cost", kwargs={"pk": target.pk, "row": old.pk}),
        {
            "landlord_rent": "2600.00",
            "effective_from": "2026-01-01",
            "effective_to": "",
            "other_monthly_cost": "",
            "other_cost_description": "",
            "landlord_lease_end": "",
            "notes": "",
            "change_note": "The lease says 2,600.",
        },
    )
    assert response.status_code == 302
    page = body(client, reverse("ledger:stores:detail", kwargs={"pk": target.pk}))
    row = between(page, f'id="cost-row-{old.pk}"', "</tr>")
    assert "Superseded" in row
    shows_the_name_not_the_username(row, user)
    [entry] = [e for e in store_costs.history_entries(target) if e.row.pk == old.pk]
    assert entry.superseded_by_user == user and entry.closed_by_user is None


def test_c1_the_cost_history_names_the_admin_who_closed_a_row() -> None:
    target = cost_store()
    outcome = set_cost(target, "2500.00", date(2026, 1, 1))
    assert outcome.cost is not None
    client, user = named(Role.ADMIN)
    response = client.post(
        reverse("ledger:stores:close_cost", kwargs={"pk": target.pk}),
        {"effective_to": "2026-03-31", "change_note": "Landlord ended it."},
    )
    assert response.status_code == 302
    [entry] = store_costs.history_entries(target)
    assert entry.closed_by_user == user
    page = body(client, reverse("ledger:stores:detail", kwargs={"pk": target.pk}))
    row = between(page, f'id="cost-row-{entry.row.pk}"', "</tr>")
    assert "Closed" in row
    shows_the_name_not_the_username(row, user)


def test_c1_the_message_log_names_who_sent_by_hand_and_keeps_ladder(
    clock: Any,  # noqa: F811
) -> None:
    clock(tc.at(date(2026, 10, 6)))
    charge = tc.rent_charge()
    client, user = named(Role.BILLING)
    step10 = tc.step(10)
    client.post(tc.send_url(charge), {"step": str(step10.pk)})
    client.post(tc.send_url(charge), {"step": str(step10.pk), "confirm": "on"})
    page = body(client, f"/charges/{charge.pk}/")
    log = between(page, 'id="message-log"', "</table>")
    rows = log.split("<tr")[2:]  # past the header row
    assert len(rows) == 2
    manual = next(row for row in rows if "Manual" in row)
    shows_the_name_not_the_username(manual, user)


def test_c1_the_activity_log_user_column_shows_the_name() -> None:
    client, user = named(Role.ADMIN)
    # The row's `at` is set on create; the audit table is append-only on PostgreSQL (a trigger
    # refuses UPDATE), so nothing rewrites it.
    row = AuditLog.objects.create(
        action="operator.updated", entity_type="operator", entity_id=str(uuid.uuid4()), actor=user
    )
    today = datetime.now(UTC).date()
    page = body(
        client,
        f"/audit/?from={(today - timedelta(days=1)).isoformat()}&to={today.isoformat()}",
    )
    cell = between(page, f'id="audit-{row.pk}"', "</tr>")
    cell = between(cell, 'class="audit-user"', "</td>")
    shows_the_name_not_the_username(cell, user)


def test_c1_the_store_history_tab_user_column_shows_the_name() -> None:
    target = saved(new_store())
    client, user = named(Role.ADMIN)
    AuditLog.objects.create(
        action="store.updated", entity_type="store", entity_id=str(target.pk), actor=user
    )
    page = body(client, f"/stores/{target.pk}/?tab=history")
    table = between(page, 'id="store-history"', "</table>")
    row = between(table, 'class="history-row"', "</tr>")
    shows_the_name_not_the_username(row, user)


def test_c1_the_documents_activity_user_column_shows_the_name(
    storage: MemoryDocumentStorage,
) -> None:
    target = saved(new_store())
    client, user = named(Role.BILLING)
    assert do_upload(target, user, "plan.png", PNG).ok
    page = body(client, f"/stores/{target.pk}/?tab=documents")
    row = between(page, 'class="document-activity"', "</tr>")
    shows_the_name_not_the_username(row, user)


def test_c1_a_note_without_a_label_names_its_author() -> None:
    client, user = named(Role.ADMIN)
    lease = saved(new_lease())
    assert lease.operator is not None
    Note.objects.create(
        entity_type=NoteEntityType.OPERATOR,
        entity_id=lease.operator.pk,
        body="Op note.",
        author=user,
    )
    Note.objects.create(
        entity_type=NoteEntityType.LEASE, entity_id=lease.pk, body="Lease note.", author=user
    )
    charge = saved(new_rent_charge(lease=lease))
    ChargeNote.objects.create(rent_charge=charge, body="Charge note.", author=user)
    for url, text in (
        (f"/operators/{lease.operator.pk}/", "Op note."),
        (f"/leases/{lease.pk}/", "Lease note."),
        (f"/charges/{charge.pk}/", "Charge note."),
    ):
        note = between(body(client, url), text, "</p>")
        shows_the_name_not_the_username(note, user)


def test_c1_a_note_with_a_label_keeps_the_label() -> None:
    client, user = named(Role.ADMIN)
    lease = saved(new_lease())
    Note.objects.create(
        entity_type=NoteEntityType.LEASE,
        entity_id=lease.pk,
        body="Imported note.",
        author=user,
        author_label="Sheet import",
    )
    note = between(body(client, f"/leases/{lease.pk}/"), "Imported note.", "</p>")
    assert "Sheet import" in note and NAME not in note


def test_c1_the_allowed_transfer_balance_names_the_admin() -> None:
    _, user = named(Role.ADMIN)
    row = tt.owing(user)
    assert tt.allow(row, user).ok
    assert tt.confirm(row, user).ok
    le.fresh(row.lease_end)
    [item] = balances()
    assert item.text.endswith(f" (transfer, allowed by {NAME})")
    assert user.get_username() not in item.text


# --- C2: the Refunds card ------------------------------------------------------------------


def test_c2_a_payment_recorded_outside_the_processor_has_no_refunds_card() -> None:
    name_provider()
    html = payment_page(a_zelle_payment("500.00"))
    assert 'id="tile-applied"' in html and 'id="paid-outside"' in html
    assert 'id="refunds-card"' not in html and 'id="refunds"' not in html


def test_c2_a_processor_payment_keeps_its_refunds_card(fake: FakeProvider, enabled: None) -> None:
    make_world(fake)
    html = payment_page(ach(fake, "1500.00"))
    assert 'id="tile-applied"' not in html
    assert 'id="refunds-card"' in html and "No refunds." in html


# --- C3: the builder's no-bank tiles ---------------------------------------------------------


def no_bank_draft(fake: FakeProvider, name: str = "") -> tuple[Any, str]:
    operator = an_operator(fake, customer="cus-nb", bank=None)
    if name:
        type(operator).objects.filter(pk=operator.pk).update(display_name=name)
        operator.refresh_from_db()
    lease = make_lease(operator=operator)
    return operator, builder.draft_html(a_draft(operator, lease, one_line()), Role.ADMIN)


BANK_TILES = ("charge_now", "schedule", "autopay", "settlement")


def test_c3_bank_unavailable_reads_the_add_payment_sentence_with_the_operators_name(
    fake: FakeProvider, enabled: None, builder_now: None
) -> None:
    operator = an_operator(fake, customer="cus-nb", bank=None)
    line = f"No bank account on file for {operator.display_name}."
    tiles = invoice_builder.bank_tiles(operator)
    assert invoice_builder.bank_unavailable(operator) == line == tiles.bank_why
    assert invoice_builder.charge_now_unavailable(operator) == line
    assert tiles.bank_url == reverse("ledger:operators:detail", kwargs={"pk": operator.pk})
    assert tiles.charge_now_more == ""
    nobody = invoice_builder.bank_tiles(None)
    assert nobody.bank_why == "No bank account on file for the operator." and nobody.bank_url == ""


def test_c3_every_greyed_bank_tile_links_the_operator_page_under_its_line(
    fake: FakeProvider, enabled: None, builder_now: None
) -> None:
    operator, html = no_bank_draft(fake)
    url = reverse("ledger:operators:detail", kwargs={"pk": operator.pk})
    plain = f"No bank account on file for {operator.display_name}."
    line = escape(plain)
    for value in BANK_TILES:
        found = builder.tile(html, value)
        assert builder.greyed(html, value) == plain
        assert (
            f'<span class="collect-note unavailable-reason" id="{value}-reason">{line}</span>'
            f'<a class="collect-link" id="{value}-reason-link" href="{url}">Operator page</a>'
        ) in found, value
        assert f'id="{value}-reason-more"' not in found, value
    for value in ("draft", "record"):
        assert 'class="collect-link"' not in builder.tile(html, value), value
    assert "Update from" not in html.split('id="collect-card"')[1].split("</section>")[0]


def test_c3_with_the_flag_off_too_charge_now_adds_the_processor_line_under_the_link(
    fake: FakeProvider, builder_now: None
) -> None:
    with override_settings(STAX_CALLER="worker", STAX_PAY_INVOICE_ENABLED=False):
        operator, html = no_bank_draft(fake)
        tiles = invoice_builder.bank_tiles(operator)
    also_off = str(one_off_service.DEBIT_ALSO_OFF)
    assert tiles.charge_now_more == also_off
    assert tiles.charge_now_why == f"No bank account on file for {operator.display_name}."
    found = builder.tile(html, "charge_now")
    link = found.index('id="charge_now-reason-link"')
    more = f'<span class="collect-note" id="charge_now-reason-more">{escape(also_off)}</span>'
    assert more in found and found.index(more) > link
    # The flag gates Charge now's call alone: the schedules' tiles carry no such line.
    for value in ("schedule", "autopay", "settlement"):
        found = builder.tile(html, value)
        assert 'id="' + value + '-reason-link"' in found and "reason-more" not in found, value


def test_c3_a_bank_on_file_with_the_flag_off_reads_the_flag_line_without_a_link(
    fake: FakeProvider, builder_now: None
) -> None:
    operator, lease = builder.banked(fake)
    with override_settings(STAX_CALLER="worker", STAX_PAY_INVOICE_ENABLED=False):
        html = builder.draft_html(a_draft(operator, lease, one_line()), Role.ADMIN)
        tiles = invoice_builder.bank_tiles(operator)
    assert tiles == invoice_builder.BankTiles(
        bank_why="", bank_url="", charge_now_why=str(one_off_service.DEBIT_OFF), charge_now_more=""
    )
    assert builder.greyed(html, "charge_now") == str(one_off_service.DEBIT_OFF)
    assert 'class="collect-link"' not in html.split('id="collect-card"')[1].split("</section>")[0]


def test_c3_a_billing_user_sees_charge_now_needs_an_admin_without_a_link(
    fake: FakeProvider, enabled: None, builder_now: None
) -> None:
    operator = an_operator(fake, customer="cus-nb", bank=None)
    lease = make_lease(operator=operator)
    html = builder.draft_html(a_draft(operator, lease, one_line()), Role.BILLING)
    found = builder.tile(html, "charge_now")
    assert 'id="charge_now-reason-link"' not in found
    assert 'id="schedule-reason-link"' in builder.tile(html, "schedule")


def test_c3_a_hostile_operator_name_is_escaped_on_the_tile(
    fake: FakeProvider, enabled: None, builder_now: None
) -> None:
    _, html = no_bank_draft(fake, name='<img src=x onerror="alert(1)">')
    found = builder.tile(html, "schedule")
    assert "<img" not in found and "&lt;img" in found


# --- C4: the today's-debit note lives for one request ---------------------------------------


NOTE = "Agreed on the phone."


def notes_of(client: Client, charge: RentCharge) -> list[str]:
    prefix = f"rent-debit-note:{charge.pk}:"
    return [name for name in client.session.keys() if name.startswith(prefix)]


def a_held_note(fake: FakeProvider) -> tuple[Client, RentCharge, dict[str, str]]:
    """Add payment's today's debit posted with a note: the note waits in the session."""
    charge = rd.a_rent(fake, "1500.00")
    client = client_for(Role.ADMIN)[0]
    data = rd.debit_post(charge, rd.today(), note=NOTE)
    response = client.post(rd.page(charge), data)
    assert response["Location"] == f"{rd.confirm_page(charge)}?key={data['creation_key']}"
    assert notes_of(client, charge) == [f"rent-debit-note:{charge.pk}:{data['creation_key']}"]
    return client, charge, data


@pytest.mark.parametrize("left_to", ["charge", "add payment"])
def test_c4_an_abandoned_confirmation_leaves_no_note_behind(
    fake: FakeProvider, enabled: None, left_to: str
) -> None:
    client, charge, data = a_held_note(fake)
    # The confirmation's GET keeps it (the POST still needs it); leaving drops it.
    body(client, f"{rd.confirm_page(charge)}?key={data['creation_key']}")
    assert notes_of(client, charge)
    body(client, rd.detail(charge) if left_to == "charge" else rd.page(charge))
    assert notes_of(client, charge) == []
    # The confirmation posted later debits without the dropped note.
    client.post(rd.confirm_page(charge), {"key": data["creation_key"]})
    assert OneOffCharge.objects.get().note == ""


def test_c4_every_note_of_the_charge_goes_and_another_charges_stays(
    fake: FakeProvider, enabled: None
) -> None:
    client, charge, _ = a_held_note(fake)
    client.post(rd.page(charge), rd.debit_post(charge, rd.today(), note="A second try."))
    assert len(notes_of(client, charge)) == 2
    other = rd.a_rent(fake, "900.00", operator=charge.lease.operator)
    client.post(rd.page(other), rd.debit_post(other, rd.today(), note="Other charge."))
    body(client, rd.detail(charge))
    assert notes_of(client, charge) == [] and len(notes_of(client, other)) == 1


def test_c4_the_happy_path_still_carries_the_note_to_the_confirmation_post(
    fake: FakeProvider, enabled: None
) -> None:
    client, charge, data = a_held_note(fake)
    confirm = f"{rd.confirm_page(charge)}?key={data['creation_key']}"
    body(client, confirm)
    done = client.post(confirm, {"key": data["creation_key"]})
    assert done["Location"] == rd.detail(charge)
    assert OneOffCharge.objects.get().note == NOTE
    assert notes_of(client, charge) == []


# --- C5: one statement_first ---------------------------------------------------------------


def test_c5_statement_first_lives_once_in_the_wizard_frame() -> None:
    frame = (VIEWS / "wizard_frame.py").read_text(encoding="utf-8")
    assert frame.count("def statement_first(") == 1
    for name in ("lease_ends.py", "transfers.py"):
        source = (VIEWS / name).read_text(encoding="utf-8")
        assert "def _statement_first" not in source and "_statement_first(" not in source, name
        assert "wizard_frame.statement_first(request, " in source, name


# --- C6: the list family's phone meta line --------------------------------------------------


def phone_cards_block() -> str:
    css = APP_CSS.read_text(encoding="utf-8")
    return css.split("@media (max-width: 719.98px) {", 1)[1].split("\n}\n", 1)[0]


def test_c6_the_meta_lines_links_are_text_height_and_the_row_stays_44_px() -> None:
    block = phone_cards_block()
    rows = ".list-card > .table-cards > table.role-cards > tbody > tr"
    assert f"{rows} {{ min-height: var(--tap); }}" in block
    assert f"{rows} > td[data-role^=meta] a {{ display: inline; min-height: 0; }}" in block
    # More specific than the coarse pointer's 44 px cell links, which it overrides.
    css = APP_CSS.read_text(encoding="utf-8")
    assert "td a, li a, .pagination a, .pager a, .segmented a, .tab { display: inline-flex;" in css


def test_c6_the_stores_lists_two_badges_lead_line_two_together() -> None:
    block = phone_cards_block()
    cells = ".table-cards > table#stores.role-cards > tbody > tr > td"
    assert f"{cells}[data-role=status] {{ order: 3;" in block
    assert f"{cells}[data-role=meta-badge] {{ order: 3; }}" in block
    lease = a_lease(a_store("0915 META", 915), a_plain_operator("Meta Co"))
    page = body(client_for(Role.VIEWER)[0], "/stores/")
    row = between(page, "0915 META", "</tr>")
    assert re.search(r'<td class="store-balance" [^>]*data-role="status"', row)
    assert re.search(r'<td class="store-status" [^>]*data-role="meta-badge"', row)
    link = f'href="/operators/{lease.operator_id}/"'
    operator_cell = row[: row.index(link)]
    assert operator_cell[operator_cell.rindex("<td") :].count('data-role="meta"') == 1


# --- C7: the statement step's Deductions tile ---------------------------------------------


def test_c7_the_statement_step_loads_the_total_script_for_its_deductions_tile(
    storage: MemoryDocumentStorage,
) -> None:
    lease = a_lease(
        a_store("0916 TILE", 916), a_plain_operator("Tile Co"), channel=PaymentChannel.WIRE
    )
    end = le.draft(lease)
    client = client_for(Role.ADMIN)[0]
    page = body(client, reverse("ledger:lease_ends:statement", kwargs={"pk": end.pk}))
    assert '<script src="/static/ledger/table_total.js" defer></script>' in page
    tile = between(page, '<span class="kpi-label">Deductions</span>', "</div>")
    assert (
        '<span class="kpi-value js-total-out" data-total-for="deduction-rows" '
        'data-tone-above-zero="tone-unpaid">$0.00</span>'
    ) in tile
    assert 'id="deduction-rows"' in page
    script = (BACKEND / "ledger" / "static" / "ledger" / "table_total.js").read_text()
    assert "out.classList.toggle(out.dataset.toneAboveZero, sum > 0);" in script


def test_c7_the_kpi_component_is_byte_identical_without_the_new_keywords() -> None:
    from finance.templatetags.components import kpi

    # The output every other caller gets, as before PLAN-013a C7.
    assert str(kpi("Paid", "$5.00")) == (
        '<div class="kpi"><span class="kpi-label">Paid</span>'
        '<span class="kpi-value">$5.00</span></div>'
    )
    assert str(kpi("Due", "$9.00", "2 lines", "unpaid")) == (
        '<div class="kpi"><span class="kpi-label">Due</span>'
        '<span class="kpi-value tone-unpaid">$9.00</span>'
        '<span class="kpi-note">2 lines</span></div>'
    )
    assert str(kpi("x", "1", "", "bogus")) == (
        '<div class="kpi"><span class="kpi-label">x</span><span class="kpi-value">1</span></div>'
    )
    live = kpi(
        "Deductions",
        "$0.00",
        "0 lines",
        "",
        value_class="js-total-out",
        value_attrs='data-total-for="deduction-rows" data-tone-above-zero="tone-unpaid"',
    )
    assert str(live) == (
        '<div class="kpi"><span class="kpi-label">Deductions</span>'
        '<span class="kpi-value js-total-out" data-total-for="deduction-rows" '
        'data-tone-above-zero="tone-unpaid">$0.00</span><span class="kpi-note">0 lines</span></div>'
    )


@pytest.mark.parametrize(
    ("value_class", "value_attrs"),
    [
        ('a" onclick="x', ""),
        ("", 'onclick="alert(1)"'),
        ("", 'data-x="a b"'),
        ("", 'data-x="<b>"'),
        ("", 'data-x="1"><script>'),
    ],
)
def test_c7_the_kpi_component_refuses_anything_but_classes_and_plain_data_attributes(
    value_class: str, value_attrs: str
) -> None:
    from finance.templatetags.components import kpi

    with pytest.raises(ValueError):
        kpi("x", "1", value_class=value_class, value_attrs=value_attrs)

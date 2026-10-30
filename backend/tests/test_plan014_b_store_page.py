"""PLAN-014 Batch B (Fable): Brand everywhere with exactly the three options and the retired
types cleared into Needs review (migration 0058 forward and backward); Admin › Settings with
the Brands card (admin only, add, rename, activate, order, refusals, audit); the LEASE card's
rows (no "Since", the open balance's tone, Sign fee and Insurance in four states, the previous
year's note); "Notes & follow-ups" everywhere; store notes (author or admin, "edited", pinning,
the NOTES card, deletions audited, payment data refused); the timeline's four-way filter — and
the adversarial cases of plan §5 B (a 2,001-character note, a duplicate brand name in another
case, a viewer's POSTs, a deleted note's trail)."""

from __future__ import annotations

import importlib
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts.roles import Role
from ledger.models import (
    AuditLog,
    Brand,
    FeeType,
    Note,
    OneOffCategory,
    Store,
)
from ledger.services import attention, notes, timeline
from ledger.services.contacts import ContactInput, add_entry
from tests.auth_helpers import make_user, verified_client
from tests.receivables_helpers import (
    a_lease,
    a_one_off_charge,
    a_rent_charge,
    a_store,
    an_insurance_charge,
    an_operator,
)
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

TODAY = date(2026, 10, 6)
TEMPLATES = Path(__file__).resolve().parents[1]
BRANDS = ["iFixandRepair", "Techy", "Talk&Fix"]
MIGRATIONS = Path(__file__).resolve().parents[1] / "ledger" / "migrations"
MIGRATION_0058 = next(MIGRATIONS.glob("0058_*.py")).stem


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    return TODAY


def page(client: Client, path: str, expect: int = 200) -> str:
    response = client.get(path)
    assert response.status_code == expect, (path, response.status_code)
    return response.content.decode()


def section(html: str, element_id: str, end: str = "</section>") -> str:
    start = html.index(f'id="{element_id}"')
    return html[start : html.index(end, start)]


def a_user(role: Role) -> tuple[Client, User]:
    user, device = make_user(f"b-{uuid.uuid4().hex[:8]}", role)
    assert device is not None
    return verified_client(user, device), user


# --- B1: Brand -------------------------------------------------------------------------------


def test_exactly_the_three_brands_are_active_in_order_and_the_old_types_are_not() -> None:
    assert (
        list(
            Brand.objects.filter(active=True).order_by("sort_order").values_list("name", flat=True)
        )
        == BRANDS
    )
    retired = set(Brand.objects.filter(active=False).values_list("name", flat=True))
    assert {"Inline store", "Kiosk", "Cart", "Pop-up", "Warehouse"} <= retired


def test_store_type_is_gone_from_the_code_and_the_pages() -> None:
    offenders = []
    for path in list(TEMPLATES.rglob("*.html")) + list(TEMPLATES.rglob("*.py")):
        if "migrations" in path.parts or path.parts[-2:][0] == "tests" or "/tests/" in str(path):
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"store[ _]type|StoreType", text):
            offenders.append(str(path.relative_to(TEMPLATES)))
    assert not offenders, offenders
    assert not hasattr(importlib.import_module("ledger.models"), "StoreType")


def test_the_form_the_filter_and_the_csv_say_brand() -> None:
    techy = Brand.objects.get(name="Techy")
    store = a_store("1501", number=1501)
    store.brand = techy
    store.save()
    client, _ = client_for(Role.ADMIN)
    assert re.search(r">Brand</label>", page(client, "/stores/new/"))
    html = page(client, f"/stores/?brand={techy.pk}")
    assert "Brand: Techy" in html and "1501" in section(html, "results", "</table>")
    header = client.get("/stores/export.csv")
    first = b"".join(header.streaming_content).decode().splitlines()[0]  # type: ignore[attr-defined]
    assert "Brand" in first and "type" not in first.lower()
    assert '<span class="pill pill-type">Techy</span>' in page(client, f"/stores/{store.pk}/")


def test_a_retired_type_is_not_offered_but_a_store_keeps_its_own() -> None:
    from ledger.forms import StoreForm

    kiosk = Brand.objects.get(name="Kiosk")
    labels = [label for _, label in StoreForm().fields["brand"].choices]  # type: ignore[attr-defined]
    assert labels == ["—", *BRANDS]
    store = a_store("1502")
    store.brand = kiosk
    store.save()
    labels = [label for _, label in StoreForm(instance=store).fields["brand"].choices]  # type: ignore[attr-defined]
    assert "Kiosk" in labels


# --- B1: migration 0058 forward and backward --------------------------------------------------

LEDGER_BEFORE = ("ledger", "0057_payment_void_fields")


def _migrate(*targets: tuple[str, str]) -> Any:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(list(targets))
    return MigrationExecutor(connection).loader.project_state(list(targets)).apps


@pytest.mark.django_db(transaction=True)
def test_0058_clears_retired_types_into_needs_review_and_refuses_to_reverse_over_them() -> None:
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    old = _migrate(LEDGER_BEFORE)
    try:
        # A transaction test before this one may have flushed 0051's rows: write the kind.
        kiosk, _ = old.get_model("ledger", "StoreType").objects.get_or_create(
            name="Kiosk", defaults={"sort_order": 20, "active": True}
        )
        old_store = old.get_model("ledger", "Store").objects.create(
            external_label="Kiosk 0801", status="active", store_type=kiosk
        )
        untyped = old.get_model("ledger", "Store").objects.create(
            external_label="Plain 0802", status="active"
        )
        new = _migrate(("ledger", MIGRATION_0058))
        brand = new.get_model("ledger", "Brand")
        assert (
            list(
                brand.objects.filter(active=True)
                .order_by("sort_order")
                .values_list("name", flat=True)
            )
            == BRANDS
        )
        assert brand.objects.get(name="Kiosk").active is False
        store = new.get_model("ledger", "Store")
        cleared = store.objects.get(pk=old_store.pk)
        assert cleared.brand_id is None and cleared.needs_review is True
        plain = store.objects.get(pk=untyped.pk)
        assert plain.needs_review is False
        audit = new.get_model("ledger", "AuditLog")
        rows = audit.objects.filter(action="store.brand_cleared", entity_id=str(old_store.pk))
        assert rows.count() == 1 and rows.get().before.get("brand") == "Kiosk"
        with pytest.raises(RuntimeError):
            _migrate(LEDGER_BEFORE)
    finally:
        _migrate(*leaves)


@pytest.mark.django_db(transaction=True)
def test_0058_reverses_when_no_store_was_cleared() -> None:
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        old = _migrate(LEDGER_BEFORE)
        types = old.get_model("ledger", "StoreType")
        types.objects.get_or_create(name="Kiosk", defaults={"sort_order": 20, "active": True})
        types.objects.filter(name="Kiosk").update(active=True)
        new = _migrate(("ledger", MIGRATION_0058))
        assert new.get_model("ledger", "Brand").objects.get(name="Kiosk").active is False
        assert new.get_model("ledger", "Brand").objects.filter(name="Techy", active=True).exists()
        back = _migrate(LEDGER_BEFORE)
        types = back.get_model("ledger", "StoreType")
        assert types.objects.filter(name="Kiosk", active=True).exists()
    finally:
        _migrate(*leaves)


def test_a_cleared_store_is_an_attention_item_until_a_brand_is_chosen() -> None:
    store = a_store("1503", number=1503)
    store.needs_review = True
    store.save()
    AuditLog.objects.create(
        action="store.brand_cleared",
        entity_type="store",
        entity_id=str(store.pk),
        before={"brand": "Cart"},
    )
    items = [
        item for item in attention.attention(TODAY).items if item.kind == attention.BRAND_NEEDED
    ]
    assert (
        len(items) == 1
        and "Brand needs choosing" in items[0].text
        and str(store.pk) in items[0].url
    )
    fresh = a_store("1504", number=1504)  # a new store without a brand is not flagged
    assert fresh.brand_id is None
    assert (
        sum(1 for item in attention.attention(TODAY).items if item.kind == attention.BRAND_NEEDED)
        == 1
    )
    store.brand = Brand.objects.get(name="Techy")
    store.needs_review = False
    store.save()
    assert not [
        item for item in attention.attention(TODAY).items if item.kind == attention.BRAND_NEEDED
    ]


# --- B2: Admin › Settings ------------------------------------------------------------------


def test_settings_is_for_admins_and_lists_the_brands() -> None:
    billing, _ = client_for(Role.BILLING)
    assert billing.get("/admin-settings/").status_code in (302, 403)
    admin, _ = client_for(Role.ADMIN)
    html = page(admin, "/admin-settings/")
    for name in BRANDS:
        assert name.replace("&", "&amp;") in html, name
    assert 'id="brands">Brands<' in html
    nav = page(admin, "/")
    assert 'id="nav-settings"' in nav and ">Preferences<" in nav
    assert 'id="nav-settings"' not in page(billing, "/")


def test_brand_add_rename_toggle_and_order_are_audited_and_refuse_bad_names() -> None:
    admin, _ = client_for(Role.ADMIN)
    before = AuditLog.objects.filter(action__startswith="brand.").count()
    assert (
        admin.post(reverse("ledger:settings:brand_add"), {"name": "Phone Hub"}).status_code == 302
    )
    hub = Brand.objects.get(name="Phone Hub")
    assert hub.active and hub.sort_order > 3
    # A refusal goes back to the page with its sentence as a message; nothing is created.
    refused = (200, 302, 400)
    response = admin.post(reverse("ledger:settings:brand_add"), {"name": "techy"})
    assert response.status_code in refused and not Brand.objects.filter(name="techy").exists()
    assert admin.post(reverse("ledger:settings:brand_add"), {"name": "   "}).status_code in refused
    assert (
        admin.post(reverse("ledger:settings:brand_add"), {"name": "x" * 61}).status_code in refused
    )
    assert Brand.objects.count() == 9  # the three, the five old kinds, Phone Hub
    assert (
        admin.post(
            reverse("ledger:settings:brand_save", kwargs={"pk": hub.pk}), {"name": "Phone Hub 2"}
        ).status_code
        == 302
    )
    hub.refresh_from_db()
    assert hub.name == "Phone Hub 2"
    assert (
        admin.post(reverse("ledger:settings:brand_toggle", kwargs={"pk": hub.pk}), {}).status_code
        == 302
    )
    hub = Brand.objects.get(pk=hub.pk)
    assert hub.active is False
    assert (
        admin.post(
            reverse("ledger:settings:brand_move", kwargs={"pk": hub.pk}), {"direction": "up"}
        ).status_code
        == 302
    )
    assert AuditLog.objects.filter(action__startswith="brand.").count() >= before + 4
    assert Brand.objects.filter(name="Phone Hub 2").exists()  # never deleted


def test_a_billing_user_cannot_change_brands() -> None:
    billing, _ = client_for(Role.BILLING)
    techy = Brand.objects.get(name="Techy")
    assert billing.post(reverse("ledger:settings:brand_add"), {"name": "Nope"}).status_code in (
        302,
        403,
    )
    assert billing.post(
        reverse("ledger:settings:brand_toggle", kwargs={"pk": techy.pk}), {}
    ).status_code in (302, 403)
    assert not Brand.objects.filter(name="Nope").exists()
    techy.refresh_from_db()
    assert techy.active


# --- B3: the LEASE card ----------------------------------------------------------------------


def a_store_with_lease(open_date: date = date(2025, 7, 1)) -> tuple[Store, Any, Any]:
    store = a_store(f"15{uuid.uuid4().hex[:2]}")
    store.open_date = open_date
    store.save()
    operator = an_operator("Card Co")
    lease = a_lease(store, operator, started=date(2025, 7, 1))
    return store, lease, operator


def lease_card(store: Store) -> str:
    html = page(client_for(Role.BILLING)[0], f"/stores/{store.pk}/")
    return section(html, "lease-card", "</div>\n")


def sign_fee_type() -> FeeType:
    return FeeType.objects.get(code="sign_fee")


def test_the_card_has_no_since_and_the_open_balance_tone() -> None:
    store, lease, _ = a_store_with_lease()
    card = lease_card(store)
    assert 'id="header-since"' not in card and "Since" not in card
    assert "$0.00 ✓" in card and "side-paid" in card
    a_rent_charge(lease, date(2026, 9, 1), due="1000.00")
    card = lease_card(store)
    assert (
        "side-unpaid" in card
        and "$1,000.00" in card
        and "✓" not in section(card, "header-open-balance", "</dd>")
    )


@pytest.mark.parametrize(
    ("price", "paid", "expected"),
    [
        ("500.00", "500.00", "✓ collected"),
        ("500.00", "200.00", "$200.00 of $500.00 · partly collected"),
        ("500.00", "0.00", "not collected"),
    ],
)
def test_the_sign_fee_row_states(price: str, paid: str, expected: str) -> None:
    store, lease, operator = a_store_with_lease()
    charge = a_one_off_charge(
        operator,
        price,
        date(2025, 7, 15),
        lease=lease,
        paid=paid,
        fee_type=sign_fee_type(),
        category=OneOffCategory.SETUP,
        description="Sign fee",
        status="paid" if paid == price else "owed",
    )
    card = lease_card(store)
    row = section(card, "header-sign-fee", "</dd>")
    assert expected in row and f"/{charge.pk}/" in row
    assert "Bill now" not in row


def test_the_sign_fee_not_billed_offers_bill_now_to_billing() -> None:
    store, _, _ = a_store_with_lease()
    row = section(lease_card(store), "header-sign-fee", "</dd>")
    assert "not billed" in row and 'id="sign-fee-bill"' in row and "Bill now" in row


@pytest.mark.parametrize(
    ("amount", "paid", "expected"),
    [
        ("695.00", "695.00", "✓ collected"),
        ("695.00", "95.00", "$95.00 of $695.00 · partly collected"),
        ("695.00", "0.00", "not collected"),
    ],
)
def test_the_insurance_row_states_for_the_current_policy_year(
    amount: str, paid: str, expected: str
) -> None:
    store, lease, _ = a_store_with_lease()
    charge = an_insurance_charge(lease, 2026, date(2026, 7, 1), amount)
    if paid != "0.00":
        from ledger.services import insurance

        outcome = insurance.record_manual_payment(
            charge.pk,
            method="wire",
            amount=Decimal(paid),
            paid_on=date(2026, 7, 2),
            note="Wire.",
            actor=None,
        )
        assert outcome.ok, outcome.message
    card = lease_card(store)
    assert "Insurance 2026–27" in card
    row = section(card, "header-insurance-state", "</dd>")
    assert expected in row
    assert charge.pk  # an insurance charge has no page of its own: the row is not linked


def test_the_insurance_row_not_billed_and_the_previous_year_notes() -> None:
    store, lease, _ = a_store_with_lease()
    card = lease_card(store)
    row = section(card, "header-insurance-state", "</dd>")
    assert "not billed" in row  # no per-lease insurance creation route: no "Bill now" (QA-014)
    assert "Insurance 2025–26 never billed" in card
    an_insurance_charge(lease, 2025, date(2025, 7, 1), "300.00")
    card = lease_card(store)
    assert "Insurance 2025–26 ($300.00) not collected" in card and "never billed" not in card


def test_no_previous_year_note_when_the_lease_did_not_hold_the_store_then() -> None:
    store = a_store("1588")
    store.open_date = date(2020, 3, 1)
    store.save()
    a_lease(store, an_operator("New Co"), started=date(2026, 8, 1))
    card = lease_card(store)
    assert "Insurance 2025–26" not in card


# --- B4: the rename -------------------------------------------------------------------------


def test_contact_log_is_renamed_everywhere() -> None:
    offenders = []
    for path in list(TEMPLATES.rglob("*.html")):
        if "Contact log" in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(TEMPLATES)))
    assert not offenders, offenders
    store, _, _ = a_store_with_lease()
    html = page(client_for(Role.VIEWER)[0], f"/stores/{store.pk}/?tab=contacts")
    assert "Notes &amp; follow-ups" in html and "Contact log" not in html


# --- B5: store notes -----------------------------------------------------------------------


def note_tab(client: Client, store: Store) -> str:
    return page(client, f"/stores/{store.pk}/?tab=contacts")


def test_billing_adds_a_note_and_the_page_shows_author_and_time() -> None:
    store, _, _ = a_store_with_lease()
    client, user = a_user(Role.BILLING)
    user.first_name, user.last_name = "Ayşe", "Kaya"
    user.save()
    assert 'id="note-form"' in note_tab(client, store)
    response = client.post(f"/stores/{store.pk}/notes/", {"body": "Called about the sign."})
    assert response.status_code == 302
    note = Note.objects.get(entity_id=store.pk)
    assert note.author_id == user.pk and note.lease_id is not None and not note.pinned
    html = note_tab(client, store)
    entry = section(html, f"note-{note.pk}", "</li>")
    assert "Called about the sign." in entry and "Ayşe Kaya" in entry and "edited" not in entry
    assert AuditLog.objects.filter(action="note.created", entity_id=str(note.pk)).exists()


def test_a_viewer_sees_no_form_and_cannot_post() -> None:
    store, _, _ = a_store_with_lease()
    viewer, _ = client_for(Role.VIEWER)
    assert 'id="note-form"' not in note_tab(viewer, store)
    assert viewer.post(f"/stores/{store.pk}/notes/", {"body": "Nope"}).status_code in (302, 403)
    assert not Note.objects.filter(entity_id=store.pk).exists()


def test_only_the_author_or_an_admin_edits_and_the_note_says_edited() -> None:
    store, _, _ = a_store_with_lease()
    author, author_user = a_user(Role.BILLING)
    other, _ = a_user(Role.BILLING)
    admin, _ = client_for(Role.ADMIN)
    author.post(f"/stores/{store.pk}/notes/", {"body": "First version."})
    note = Note.objects.get(entity_id=store.pk)
    assert other.post(f"/notes/{note.pk}/edit/", {"body": "Hijacked."}).status_code in (302, 403)
    note.refresh_from_db()
    assert note.body == "First version." and note.edited_at is None
    assert author.post(f"/notes/{note.pk}/edit/", {"body": "Second version."}).status_code == 302
    note = Note.objects.get(pk=note.pk)
    assert note.body == "Second version." and note.edited_at is not None
    assert "edited" in section(note_tab(author, store), f"note-{note.pk}", "</li>")
    assert admin.post(f"/notes/{note.pk}/edit/", {"body": "Admin version."}).status_code == 302
    note.refresh_from_db()
    assert note.body == "Admin version."
    audit = AuditLog.objects.filter(action="note.edited", entity_id=str(note.pk)).order_by("at")
    first = audit.first()
    assert audit.count() == 2 and first is not None
    assert first.before.get("body") == "First version."
    assert note_tab(other, store).count(
        'href="/notes/'
    ) == 0 or f"/notes/{note.pk}/edit/" not in note_tab(other, store)
    assert note_tab(author, store).count(f"/notes/{note.pk}/edit/") >= 1


def test_deleting_needs_a_reason_keeps_the_trail_and_hides_the_note() -> None:
    store, _, _ = a_store_with_lease()
    author, _ = a_user(Role.BILLING)
    author.post(f"/stores/{store.pk}/notes/", {"body": "To be removed."})
    note = Note.objects.get(entity_id=store.pk)
    assert author.post(f"/notes/{note.pk}/delete/", {"reason": ""}).status_code in (200, 400)
    note.refresh_from_db()
    assert note.deleted_at is None
    assert author.post(f"/notes/{note.pk}/delete/", {"reason": "Wrong store."}).status_code == 302
    note = Note.objects.get(pk=note.pk)
    assert note.deleted_at is not None and note.delete_reason == "Wrong store."
    assert Note.objects.filter(pk=note.pk).exists()  # soft
    assert f'id="note-{note.pk}"' not in note_tab(author, store)
    row = AuditLog.objects.get(action="note.deleted", entity_id=str(note.pk))
    assert row.before.get("body") == "To be removed."
    assert notes.for_store(store) == []


def test_pinned_notes_fill_the_notes_card_newest_pin_first_up_to_five() -> None:
    store, _, _ = a_store_with_lease()
    client, user = a_user(Role.BILLING)
    assert 'id="notes-card"' not in page(client, f"/stores/{store.pk}/")
    made = []
    for n in range(7):
        client.post(f"/stores/{store.pk}/notes/", {"body": f"Note number {n}."})
    for n, note in enumerate(Note.objects.filter(entity_id=store.pk).order_by("created_at")):
        if n < 6:
            notes.pin(note, user)
            Note.objects.filter(pk=note.pk).update(pinned_at=timezone.now() + timedelta(minutes=n))
            made.append(note)
    html = page(client, f"/stores/{store.pk}/")
    card = section(html, "notes-card", "</section>")
    assert card.count('class="pinned-note"') == 5
    assert card.index("Note number 5.") < card.index("Note number 1.")
    assert "Note number 0." not in card  # the sixth pin, the oldest, is beyond the cap
    assert "Note number 6." not in card  # never pinned
    notes.unpin(made[5], user)
    assert "Note number 5." not in section(
        page(client, f"/stores/{store.pk}/"), "notes-card", "</section>"
    )


def test_pinning_is_for_billing_and_a_viewer_is_refused() -> None:
    store, _, _ = a_store_with_lease()
    client, _ = a_user(Role.BILLING)
    client.post(f"/stores/{store.pk}/notes/", {"body": "Pin me."})
    note = Note.objects.get(entity_id=store.pk)
    assert client.post(f"/notes/{note.pk}/pin/", {"pinned": "1"}).status_code == 302
    note.refresh_from_db()
    assert note.pinned and note.pinned_at is not None
    viewer, _ = client_for(Role.VIEWER)
    assert viewer.post(f"/notes/{note.pk}/pin/", {"pinned": "0"}).status_code in (302, 403)
    note.refresh_from_db()
    assert note.pinned
    assert client.post(f"/notes/{note.pk}/pin/", {"pinned": "0"}).status_code == 302
    note.refresh_from_db()
    assert not note.pinned


@pytest.mark.parametrize(
    "body",
    ["Card 4111 1111 1111 1111 on file", "Routing 021000021 acct 123456789", "x" * 2001, "", "   "],
)
def test_a_note_with_payment_data_or_a_bad_length_is_refused(body: str) -> None:
    store, _, _ = a_store_with_lease()
    client, _ = a_user(Role.BILLING)
    response = client.post(f"/stores/{store.pk}/notes/", {"body": body})
    assert response.status_code in (200, 400)
    assert not Note.objects.filter(entity_id=store.pk).exists()
    if "4111" in body:
        assert "4111" not in response.content.decode()


def test_a_note_of_2000_characters_is_accepted() -> None:
    store, _, _ = a_store_with_lease()
    client, _ = a_user(Role.BILLING)
    assert client.post(f"/stores/{store.pk}/notes/", {"body": "y" * 2000}).status_code == 302


# --- B6: the timeline -------------------------------------------------------------------------


def test_the_timeline_unions_notes_and_contacts_newest_first_and_filters() -> None:
    store, lease, operator = a_store_with_lease()
    client, user = a_user(Role.BILLING)
    outcome = add_entry(
        operator.pk,
        ContactInput(
            kind="call", happened_on=TODAY - timedelta(days=2), body="Called twice.", lease=lease.pk
        ),
        actor=user,
    )
    assert outcome.ok, outcome.message
    client.post(f"/stores/{store.pk}/notes/", {"body": "A fresh note."})
    entries = timeline.entries(store, "all")
    assert [entry.kind for entry in entries] == ["note", "contact"]
    assert [entry.kind for entry in timeline.entries(store, "notes")] == ["note"]
    assert [entry.kind for entry in timeline.entries(store, "contacts")] == ["contact"]
    assert timeline.entries(store, "emails") == []
    assert len(timeline.entries(store, "bogus")) == 2
    html = page(client, f"/stores/{store.pk}/?tab=contacts&show=emails")
    assert "No e-mails yet." in html and 'id="timeline-filter"' in html
    for label in ("All", "Notes", "Calls &amp; promises", "E-mails"):
        assert label in section(html, "timeline-filter", "</nav>") or label in section(
            html, "timeline-filter", "</div>"
        )
    html = page(client, f"/stores/{store.pk}/?tab=contacts&show=notes")
    assert "A fresh note." in html and "Called twice." not in html
    html = page(client, f"/stores/{store.pk}/?tab=contacts")
    assert html.index("A fresh note.") < html.index("Called twice.")


def test_the_timeline_time_is_new_york_and_the_filter_keeps_the_tab() -> None:
    store, _, _ = a_store_with_lease()
    client, _ = a_user(Role.BILLING)
    client.post(f"/stores/{store.pk}/notes/", {"body": "Timed."})
    note = Note.objects.get(entity_id=store.pk)
    Note.objects.filter(pk=note.pk).update(created_at=datetime(2026, 10, 6, 18, 30, tzinfo=UTC))
    html = page(client, f"/stores/{store.pk}/?tab=contacts")
    entry = section(html, f"note-{note.pk}", "</li>")
    assert "14:30" in entry  # 18:30 UTC is 14:30 New York in October
    links = re.findall(
        r'href="([^"]*show=[a-z]+[^"]*)"',
        section(html, "timeline-filter", "</nav>") if 'id="timeline-filter"' in html else "",
    )
    assert links and all("tab=contacts" in link for link in links)

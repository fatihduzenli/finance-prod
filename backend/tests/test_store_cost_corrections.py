"""Store-cost corrections (docs/qa/PLAN-010c-documents-and-cost-corrections.md D10, Batch
C): the superseding correction, closing the open row, live rows only everywhere (the
current cost, the margin, the open-row and overlap rules, the importer, the P&L), the
store page's history, Correct and Close forms, the role gates and the audit.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from accounts import registry
from accounts.roles import Role
from ledger.models import AuditLog, Store, StoreCost, StoreCostSource
from ledger.services import pnl
from ledger.services import store_costs as service
from ledger.validators import PAYMENT_DATA_MESSAGE
from tests.auth_helpers import make_user
from tests.test_operator_views import client_for
from tests.test_store_costs import leased, row, run_import, set_cost, store, write_csv

pytestmark = pytest.mark.django_db

D = Decimal
JAN = date(2026, 1, 1)
CARD = "4111 1111 1111 1111"
INVISIBLE_NOTES = ("", "   ", "​", "‌‍", "⁠﻿", " ", " ​ \t\n")


@pytest.fixture
def admin() -> User:
    return make_user(f"admin-{uuid.uuid4().hex[:8]}", Role.ADMIN)[0]


def values(rent: str = "2600.00", **fields: Any) -> service.CostValues:
    base: dict[str, Any] = {
        "landlord_rent": D(rent),
        "other_monthly_cost": None,
        "other_cost_description": "",
        "landlord_lease_end": None,
        "notes": "",
    }
    base.update(fields)
    return service.CostValues(**base)


def opened(rent: str = "2500.00", starts: date = JAN, **fields: Any) -> tuple[Store, StoreCost]:
    target = store()
    outcome = set_cost(target, rent, starts, **fields)
    assert outcome.ok and outcome.cost is not None
    return target, outcome.cost


def correct(
    target: Store,
    old: StoreCost,
    actor: User,
    rent: str = "2600.00",
    starts: date = JAN,
    ends: date | None = None,
    note: str = "The lease says 2,600.",
    **fields: Any,
) -> service.CostOutcome:
    return service.correct_cost(target, old, values(rent, **fields), starts, ends, note, actor)


def live(target: Store) -> list[tuple[Decimal, date, date | None]]:
    return [
        (cost.landlord_rent, cost.effective_from, cost.effective_to)
        for cost in service.live_costs(target).order_by("effective_from")
    ]


# --- correct_cost --------------------------------------------------------------------------


def test_a_correction_writes_a_new_row_and_supersedes_the_old_one(admin: User) -> None:
    target, old = opened()
    outcome = correct(target, old, admin)
    assert (outcome.ok, outcome.action, outcome.message) == (
        True,
        service.ACTION_CREATED,
        service.CORRECTED,
    )
    new = outcome.cost
    assert new is not None
    old.refresh_from_db()
    assert old.superseded_at is not None
    assert old.superseded_by_id == new.pk
    assert old.change_note == "The lease says 2,600."
    assert (old.landlord_rent, old.effective_from, old.effective_to) == (D("2500.00"), JAN, None)
    assert (new.landlord_rent, new.effective_from, new.effective_to) == (D("2600.00"), JAN, None)
    assert (new.superseded_at, new.change_note, new.source) == (
        None,
        "The lease says 2,600.",
        StoreCostSource.STAFF,
    )
    assert live(target) == [(D("2600.00"), JAN, None)]
    assert StoreCost.objects.filter(store=target).count() == 2


def test_a_correction_is_audited_with_before_and_after(admin: User) -> None:
    target, old = opened()
    new = correct(target, old, admin).cost
    assert new is not None
    superseded = AuditLog.objects.get(action=service.ACTION_AUDIT_SUPERSEDED)
    assert (superseded.actor, superseded.entity_type, superseded.entity_id) == (
        admin,
        "store_cost",
        str(old.pk),
    )
    assert superseded.before is not None and superseded.after is not None
    assert superseded.before["superseded_at"] is None
    assert superseded.after["superseded_by"] == str(new.pk)
    assert superseded.after["change_note"] == "The lease says 2,600."
    created = AuditLog.objects.get(action=service.ACTION_AUDIT_CORRECTION)
    assert (created.entity_id, created.before) == (str(new.pk), None)
    assert created.after is not None and created.after["landlord_rent"] == "2600.00"


def test_the_current_cost_margin_and_pnl_use_the_correction_only(admin: User) -> None:
    lease = leased("3500.00")
    target = lease.store
    first = set_cost(target, "2500.00").cost
    assert first is not None
    assert service.margin(target, date(2026, 2, 1)) == D("1000.00")
    correct(target, first, admin, rent="3000.00")
    current = service.current_cost(target, date(2026, 2, 1))
    assert current is not None and current.landlord_rent == D("3000.00")
    assert service.margin(target, date(2026, 2, 1)) == D("500.00")
    [month] = [
        found
        for found in pnl.rows(pnl.PnlQuery(from_month=date(2026, 2, 1), to_month=date(2026, 2, 1)))
        if found.store_id == target.pk
    ]
    assert month.landlord_rent == D("3000.00")


def test_a_superseded_row_never_counts_in_the_pnl_even_where_it_alone_covered(
    admin: User,
) -> None:
    target, old = opened(starts=JAN)
    # Moved to start in March: January and February have no live row.
    correct(target, old, admin, rent="2500.00", starts=date(2026, 3, 1))
    found = pnl.rows(pnl.PnlQuery(from_month=JAN, to_month=date(2026, 3, 1)))
    by_month = {item.month: item for item in found if item.store_id == target.pk}
    assert JAN not in by_month and date(2026, 2, 1) not in by_month
    assert by_month[date(2026, 3, 1)].landlord_rent == D("2500.00")
    assert service.current_cost(target, date(2026, 2, 1)) is None


def test_a_correction_may_move_the_dates_and_close_the_row(admin: User) -> None:
    target, old = opened()
    outcome = correct(target, old, admin, starts=date(2026, 2, 1), ends=date(2026, 6, 30))
    assert outcome.ok
    assert live(target) == [(D("2600.00"), date(2026, 2, 1), date(2026, 6, 30))]
    assert service.open_cost(target) is None


def test_a_correction_that_would_overlap_another_live_row_is_refused(admin: User) -> None:
    target, first = opened(starts=JAN)
    second = set_cost(target, "2700.00", date(2026, 4, 1)).cost
    assert second is not None
    first.refresh_from_db()
    before = live(target)
    for starts, ends in (
        (JAN, date(2026, 4, 1)),
        (JAN, None),
        (date(2026, 3, 1), date(2026, 5, 1)),
    ):
        outcome = correct(target, first, admin, starts=starts, ends=ends)
        assert (outcome.ok, outcome.message) == (
            False,
            service.OVERLAPS.format(start="2026-04-01", end="open"),
        )
    # Moving the open row before the first one ends is refused as well.
    outcome = correct(target, second, admin, starts=date(2026, 3, 31))
    assert outcome.message == service.OVERLAPS.format(start="2026-01-01", end="2026-03-31")
    assert live(target) == before
    assert not AuditLog.objects.filter(action=service.ACTION_AUDIT_SUPERSEDED).exists()


def test_a_correction_up_to_the_day_before_the_next_row_is_accepted(admin: User) -> None:
    target, first = opened(starts=JAN)
    set_cost(target, "2700.00", date(2026, 4, 1))
    first.refresh_from_db()
    assert correct(target, first, admin, starts=date(2026, 2, 1), ends=date(2026, 3, 31)).ok
    assert live(target) == [
        (D("2600.00"), date(2026, 2, 1), date(2026, 3, 31)),
        (D("2700.00"), date(2026, 4, 1), None),
    ]


def test_an_end_before_the_start_is_refused(admin: User) -> None:
    target, old = opened()
    outcome = correct(target, old, admin, starts=date(2026, 2, 1), ends=date(2026, 1, 31))
    assert (outcome.ok, outcome.message, outcome.field) == (
        False,
        service.END_BEFORE_START,
        "effective_to",
    )
    assert StoreCost.objects.filter(store=target).count() == 1


@pytest.mark.parametrize("note", INVISIBLE_NOTES)
def test_a_correction_needs_a_visible_note(note: str, admin: User) -> None:
    target, old = opened()
    outcome = correct(target, old, admin, note=note)
    assert (outcome.ok, outcome.message) == (False, service.CHANGE_NOTE_REQUIRED)
    assert StoreCost.objects.filter(store=target).count() == 1


def test_the_note_is_stored_without_invisible_characters(admin: User) -> None:
    target, old = opened()
    new = correct(target, old, admin, note="​ Typo ﻿").cost
    assert new is not None
    old.refresh_from_db()
    assert old.change_note == new.change_note == "Typo"


def test_a_note_or_value_with_payment_data_is_refused(admin: User) -> None:
    target, old = opened()
    assert correct(target, old, admin, note=f"card {CARD}").message == PAYMENT_DATA_MESSAGE
    assert correct(target, old, admin, notes=f"card {CARD}").message == PAYMENT_DATA_MESSAGE
    assert StoreCost.objects.filter(store=target).count() == 1


def test_a_note_longer_than_2000_characters_is_refused(admin: User) -> None:
    target, old = opened()
    assert correct(target, old, admin, note="x" * 2001).message == service.CHANGE_NOTE_TOO_LONG


def test_bad_values_are_refused(admin: User) -> None:
    target, old = opened()
    assert correct(target, old, admin, rent="-1.00").message == service.RENT_NEGATIVE
    assert correct(target, old, admin, rent="1.001").message == service.NOT_CENTS


@pytest.mark.parametrize("role", [Role.BILLING, Role.VIEWER, None])
def test_only_an_admin_corrects_or_closes(role: Role | None) -> None:
    target, old = opened()
    user = make_user(f"user-{uuid.uuid4().hex[:8]}", role)[0]
    with pytest.raises(PermissionDenied):
        correct(target, old, user)
    with pytest.raises(PermissionDenied):
        service.close_cost(target, date(2026, 6, 30), "Lease ended.", user)
    with pytest.raises(PermissionDenied):
        service.close_cost(target, date(2026, 6, 30), "Lease ended.", None)
    assert StoreCost.objects.filter(store=target).count() == 1


def test_a_superseded_row_or_another_stores_row_cannot_be_corrected(admin: User) -> None:
    target, old = opened()
    correct(target, old, admin)
    assert correct(target, old, admin, rent="2700.00").message == service.ALREADY_SUPERSEDED
    other, foreign = opened()
    assert correct(target, foreign, admin).message == service.ROW_NOT_FOUND
    assert StoreCost.objects.filter(store=other).count() == 1


def test_the_same_values_and_dates_change_nothing(admin: User) -> None:
    target, old = opened()
    outcome = correct(target, old, admin, rent="2500.00")
    assert (outcome.ok, outcome.action) == (True, service.ACTION_UNCHANGED)
    assert StoreCost.objects.filter(store=target).count() == 1


def test_history_survives_a_correction_of_a_correction(admin: User) -> None:
    target, first = opened()
    second = correct(target, first, admin, rent="2600.00", note="First fix.").cost
    assert second is not None
    third = correct(target, second, admin, rent="2650.00", note="Second fix.").cost
    assert third is not None
    rows = service.history(target)
    assert [cost.pk for cost in rows][0] == third.pk
    assert {cost.pk for cost in rows} == {first.pk, second.pk, third.pk}
    assert [cost.superseded_at is None for cost in rows] == [True, False, False]
    first.refresh_from_db()
    second.refresh_from_db()
    assert (first.superseded_by_id, second.superseded_by_id) == (second.pk, third.pk)
    assert (first.change_note, second.change_note) == ("First fix.", "Second fix.")
    assert live(target) == [(D("2650.00"), JAN, None)]


def test_history_lists_live_rows_first_then_superseded_latest_first(admin: User) -> None:
    target, first = opened(starts=JAN)
    later = set_cost(target, "2700.00", date(2026, 4, 1)).cost
    assert later is not None
    first.refresh_from_db()
    fixed = correct(target, first, admin, ends=date(2026, 3, 31)).cost
    assert fixed is not None
    assert [cost.pk for cost in service.history(target)] == [later.pk, fixed.pk, first.pk]


# --- The database: live rows only (D10) --------------------------------------------------


def test_two_live_open_rows_are_refused_by_the_database_but_a_superseded_one_is_not(
    admin: User,
) -> None:
    target, old = opened()
    correct(target, old, admin)
    # The superseded row shares the start date and is open: allowed.
    assert StoreCost.objects.filter(store=target, effective_from=JAN).count() == 2
    with pytest.raises(IntegrityError), transaction.atomic():
        StoreCost.objects.bulk_create(
            [
                StoreCost(
                    store=target,
                    landlord_rent=D("1.00"),
                    effective_from=date(2026, 5, 1),
                    source=StoreCostSource.STAFF,
                )
            ]
        )


# --- close_cost ----------------------------------------------------------------------------


def test_close_ends_the_open_row_and_later_months_are_unknown(admin: User) -> None:
    lease = leased("3500.00")
    target = lease.store
    row_ = set_cost(target, "2500.00").cost
    assert row_ is not None
    outcome = service.close_cost(target, date(2026, 3, 31), "​Landlord lease ended.", admin)
    assert (outcome.ok, outcome.message) == (True, service.CLOSED.format(end="2026-03-31"))
    row_.refresh_from_db()
    assert (row_.effective_to, row_.change_note, row_.superseded_at) == (
        date(2026, 3, 31),
        "Landlord lease ended.",
        None,
    )
    assert service.open_cost(target) is None
    assert service.current_cost(target, date(2026, 3, 31)) is not None
    assert service.current_cost(target, date(2026, 4, 1)) is None
    assert service.margin(target, date(2026, 4, 1)) is None
    found = pnl.rows(pnl.PnlQuery(from_month=date(2026, 3, 1), to_month=date(2026, 4, 1)))
    by_month = {item.month: item for item in found if item.store_id == target.pk}
    assert by_month[date(2026, 3, 1)].landlord_rent == D("2500.00")
    assert by_month[date(2026, 4, 1)].landlord_rent is None
    entry = AuditLog.objects.get(action=service.ACTION_AUDIT_CLOSED_BY_ADMIN)
    assert entry.actor == admin and entry.entity_id == str(row_.pk)
    assert entry.before is not None and entry.before["effective_to"] is None
    assert entry.after is not None and entry.after["effective_to"] == "2026-03-31"
    assert entry.after["change_note"] == "Landlord lease ended."


def test_a_zero_row_after_a_close_states_no_cost(admin: User) -> None:
    lease = leased("3500.00")
    target = lease.store
    set_cost(target, "2500.00")
    service.close_cost(target, date(2026, 3, 31), "Ended.", admin)
    assert set_cost(target, "0.00", date(2026, 4, 1)).ok
    zero = service.current_cost(target, date(2026, 4, 1))
    assert zero is not None and zero.landlord_rent == D("0.00")
    assert service.margin(target, date(2026, 4, 1)) == D("3500.00")


def test_close_on_the_start_day_is_allowed_and_before_it_refused(admin: User) -> None:
    target, cost = opened(starts=date(2026, 2, 1))
    refused = service.close_cost(target, date(2026, 1, 31), "Too early.", admin)
    assert (refused.ok, refused.message) == (
        False,
        service.CLOSE_BEFORE_START.format(start="2026-02-01"),
    )
    cost.refresh_from_db()
    assert cost.effective_to is None
    assert service.close_cost(target, date(2026, 2, 1), "One day.", admin).ok


def test_close_needs_an_open_row_and_a_visible_note(admin: User) -> None:
    target = store()
    assert service.close_cost(target, JAN, "Nothing.", admin).message == service.NO_OPEN_ROW
    target, cost = opened()
    for note in INVISIBLE_NOTES:
        outcome = service.close_cost(target, date(2026, 6, 30), note, admin)
        assert outcome.message == service.CLOSE_NOTE_REQUIRED, repr(note)
    assert (
        service.close_cost(target, date(2026, 6, 30), f"card {CARD}", admin).message
        == PAYMENT_DATA_MESSAGE
    )
    cost.refresh_from_db()
    assert cost.effective_to is None


def test_after_a_close_a_new_row_must_start_after_the_end(admin: User) -> None:
    target, _ = opened()
    service.close_cost(target, date(2026, 3, 31), "Ended.", admin)
    for starts in (date(2026, 3, 31), JAN, date(2025, 12, 1)):
        refused = set_cost(target, "2600.00", starts)
        assert (refused.ok, refused.message) == (
            False,
            service.AFTER_CLOSED.format(end="2026-03-31"),
        )
    created = set_cost(target, "2600.00", date(2026, 4, 1))
    assert created.ok and created.closed is None
    assert live(target) == [
        (D("2500.00"), JAN, date(2026, 3, 31)),
        (D("2600.00"), date(2026, 4, 1), None),
    ]


# --- The importer compares with live rows only ---------------------------------------------


def test_the_importer_compares_with_the_live_row_not_the_superseded_one(
    tmp_path: Path, admin: User
) -> None:
    target = store(4321)
    first = set_cost(target, "2500.00").cost
    assert first is not None
    correct(target, first, admin, rent="2600.00")
    path = write_csv(tmp_path / "costs.csv", [row("4321", "2600.00")])
    report = run_import(path)
    assert (report.unchanged, report.created, report.updated, report.refused) == (1, 0, 0, 0)
    path = write_csv(tmp_path / "costs.csv", [row("4321", "2500.00")])
    report = run_import(path)
    assert (report.updated, report.refused) == (1, 0)
    assert live(target) == [(D("2500.00"), JAN, None)]
    assert StoreCost.objects.filter(store=target).count() == 2


def test_the_importer_after_a_close_refuses_an_overlapping_row(tmp_path: Path, admin: User) -> None:
    target = store(4322)
    set_cost(target, "2500.00")
    service.close_cost(target, date(2026, 3, 31), "Ended.", admin)
    path = write_csv(tmp_path / "costs.csv", [row("4322", "2600.00", "03/01/2026")])
    assert run_import(path, dry_run=True).refused == 1
    report = run_import(path)
    assert report.refused == 1
    assert "2026-03-31" in report.refusals[0][1]
    path = write_csv(tmp_path / "costs.csv", [row("4322", "2600.00", "04/01/2026")])
    assert run_import(path).created == 1


# --- The store page (D10) ------------------------------------------------------------------


def detail(target: Store) -> str:
    return reverse("ledger:stores:detail", kwargs={"pk": target.pk})


def correct_url(target: Store, cost: StoreCost) -> str:
    return reverse("ledger:stores:correct_cost", kwargs={"pk": target.pk, "row": cost.pk})


def close_url(target: Store) -> str:
    return reverse("ledger:stores:close_cost", kwargs={"pk": target.pk})


def correct_form(**fields: str) -> dict[str, str]:
    data = {
        "landlord_rent": "2600.00",
        "effective_from": "2026-01-01",
        "effective_to": "",
        "other_monthly_cost": "",
        "other_cost_description": "",
        "landlord_lease_end": "",
        "notes": "",
        "change_note": "The lease says 2,600.",
    }
    data.update(fields)
    return data


def test_the_routes_are_admin_post_in_the_registry() -> None:
    for name in ("ledger:stores:correct_cost", "ledger:stores:close_cost"):
        rule = registry.rule_for(name)
        assert rule is not None
        assert rule.min_role is Role.ADMIN and rule.methods == {"POST"}


def test_an_admin_corrects_from_the_page_and_the_history_shows_who_when_why() -> None:
    target, old = opened()
    client, user = client_for(Role.ADMIN)
    page = client.get(detail(target)).content.decode()
    assert correct_url(target, old) in page
    assert 'id="close-cost-form"' in page
    form = client.get(correct_url(target, old))
    assert form.status_code == 200
    assert 'value="2500.00"' in form.content.decode()
    response = client.post(correct_url(target, old), correct_form())
    assert response.status_code == 302
    assert response["Location"] == detail(target) + "#store-cost"
    page = client.get(detail(target)).content.decode()
    assert service.CORRECTED in page
    assert f'id="cost-row-{old.pk}" class="cost-superseded"' in page
    old_row = page.split(f'id="cost-row-{old.pk}"')[1].split("</tr>")[0]
    assert "<s>$2,500.00</s>" in old_row
    assert "Superseded" in old_row and user.username in old_row
    assert "The lease says 2,600." in old_row
    old.refresh_from_db()
    assert old.superseded_at is not None
    # The page shows the time in the project's zone, New York (PLAN-011b D1).
    assert timezone.localtime(old.superseded_at).strftime("%Y-%m-%d %H:%M") in old_row
    assert "$2,600.00 a month from 2026-01-01" in page
    # PLAN-012e-2 D8, D10: the rule paragraphs moved to the help page, linked once.
    assert 'months after a closed row have no cost row and read "unknown"' not in page.lower()
    assert 'href="/help/store-costs/"' in page
    help_page = client.get("/help/store-costs/").content.decode().replace("&quot;", '"')
    assert 'months after a closed row have no cost row and read "unknown"' in help_page.lower()
    assert 'reads "no cost"' in help_page


def test_a_refused_correction_is_held_with_its_sentence() -> None:
    target, first = opened(starts=JAN)
    set_cost(target, "2700.00", date(2026, 4, 1))
    client, _ = client_for(Role.ADMIN)
    response = client.post(correct_url(target, first), correct_form(effective_to=""))
    assert response.status_code == 302
    assert response["Location"] == correct_url(target, first)
    page = client.get(correct_url(target, first)).content.decode()
    assert "overlap the row from 2026-04-01 to open" in page
    response = client.post(correct_url(target, first), correct_form(change_note="​"))
    page = client.get(correct_url(target, first)).content.decode()
    assert "A note is required: say why the row is changed." in page
    assert not StoreCost.objects.filter(superseded_at__isnull=False).exists()


def test_an_admin_closes_from_the_page() -> None:
    target, cost = opened()
    client, _ = client_for(Role.ADMIN)
    response = client.post(
        close_url(target), {"effective_to": "2026-03-31", "change_note": "Landlord ended it."}
    )
    assert response.status_code == 302
    assert response["Location"] == detail(target) + "#store-cost"
    page = client.get(detail(target)).content.decode()
    assert service.CLOSED.format(end="2026-03-31") in page
    assert "Landlord ended it." in page
    assert 'id="close-cost-form"' not in page
    cost.refresh_from_db()
    assert cost.effective_to == date(2026, 3, 31)


def test_a_close_without_a_note_or_before_the_start_says_why() -> None:
    target, cost = opened(starts=date(2026, 2, 1))
    client, _ = client_for(Role.ADMIN)
    client.post(close_url(target), {"effective_to": "2026-03-31", "change_note": " "})
    assert (
        "A note is required: say why the row is closed."
        in client.get(detail(target)).content.decode()
    )
    client.post(close_url(target), {"effective_to": "2026-01-01", "change_note": "Early."})
    page = client.get(detail(target)).content.decode()
    assert service.CLOSE_BEFORE_START.format(start="2026-02-01") in page
    cost.refresh_from_db()
    assert cost.effective_to is None


@pytest.mark.parametrize("role", [Role.BILLING, Role.VIEWER])
def test_billing_and_viewer_see_no_correct_or_close_and_are_refused(role: Role) -> None:
    target, cost = opened()
    client, _ = client_for(role)
    page = client.get(detail(target)).content.decode()
    assert correct_url(target, cost) not in page
    assert 'id="close-cost-form"' not in page
    assert client.get(correct_url(target, cost)).status_code == 403
    assert client.post(correct_url(target, cost), correct_form()).status_code == 403
    assert (
        client.post(
            close_url(target), {"effective_to": "2026-03-31", "change_note": "x"}
        ).status_code
        == 403
    )
    cost.refresh_from_db()
    assert (cost.superseded_at, cost.effective_to) == (None, None)


def test_bad_row_ids_are_404_and_close_takes_post_only() -> None:
    target, cost = opened()
    other, foreign = opened()
    client, _ = client_for(Role.ADMIN)
    assert client.get(correct_url(target, foreign)).status_code == 404
    bad = f"/stores/{target.pk}/costs/not-a-uuid/correct/"
    assert client.get(bad).status_code == 404
    assert client.get(close_url(target)).status_code == 405


def test_a_zero_rent_row_reads_no_cost_on_the_page() -> None:
    target, _ = opened(rent="0.00")
    client, _ = client_for(Role.VIEWER)
    assert "$0.00 a month (no cost) from 2026-01-01" in client.get(detail(target)).content.decode()

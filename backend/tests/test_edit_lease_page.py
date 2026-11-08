"""The Edit lease page (docs/qa/15/PLAN-015a-edit-lease.md §5): four cards in the New lease
family and the Changes card. The lease's identity (store, operator, start date, status) is
read-only here; the debit days, the channel, the proration until the first month is billed and a
rent change from a date (the next rent due date by default) are what a save changes.

``payments.get_provider`` is a :class:`~payments.fake.FakeProvider`; nothing leaves.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.test import Client

from accounts.roles import Role
from finance.dates import day_text
from ledger.models import (
    AuditLog,
    CollectionSchedule,
    CollectionStatus,
    Lease,
    LeaseBilling,
    LeaseRentHistory,
    PaymentChannel,
    RentCharge,
    ScheduleKind,
    ScheduleState,
)
from ledger.services import leases as service
from payments.fake import FakeProvider
from payments.models import ExternalRef
from tests.receivables_helpers import a_lease, a_rent_charge, a_store, an_operator
from tests.test_operator_views import client_for
from tests.text_helpers import assert_absent

pytestmark = pytest.mark.django_db

D = Decimal
#: The day the page treats as today: a lease debited on the 5th next pays on Oct 5, 2026.
TODAY = date(2026, 10, 1)
START = date(2026, 1, 13)


@pytest.fixture(autouse=True)
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    return TODAY


@pytest.fixture
def admin() -> Client:
    return client_for(Role.ADMIN, "admin-edit-lease")[0]


def a_world(**fields: Any) -> Lease:
    return a_lease(a_store("Istanbul", 1991), an_operator("Fatih Co"), started=START, **fields)


def path(lease: Lease) -> str:
    return f"/leases/{lease.pk}/edit/"


def page(client: Client, lease: Lease) -> str:
    response = client.get(path(lease))
    assert response.status_code == 200
    return response.content.decode()


def data(lease: Lease, **overrides: Any) -> dict[str, Any]:
    """What the page posts when nothing is touched (the switch posted only when on)."""
    values: dict[str, Any] = {
        "debit_day": str(lease.debit_day or ""),
        "second_debit_day": str(lease.second_debit_day or ""),
        "payment_channel": lease.payment_channel,
        "rent": "",
        "rent_effective_from": "",
    }
    if lease.prorate_first_month:
        values["prorate_first_month"] = "on"
    return {**values, **overrides}


def save(client: Client, lease: Lease, **overrides: Any) -> Lease:
    response = client.post(path(lease), data(lease, **overrides))
    assert response.status_code == 302
    return Lease.objects.get(pk=lease.pk)


def card(body: str, name: str) -> str:
    opening = f'<section class="card form-card edit-lease-card" id="{name}"'
    start = body.index(opening)
    return body[start : body.index("</section>", start)]


def tag_with(body: str, *parts: str) -> bool:
    return any(all(part in tag for part in parts) for tag in re.findall(r"<[^>]+>", body))


def bill_first_month(lease: Lease) -> RentCharge:
    return a_rent_charge(lease, START.replace(day=1), due="2100.00")


def a_live_schedule(lease: Lease, *, needs_review: bool = False) -> CollectionSchedule:
    row = CollectionSchedule.objects.create(
        lease=lease,
        kind=ScheduleKind.RENT,
        seq=1,
        debit_day=lease.debit_day,
        amount=D("3500.00"),
        state=ScheduleState.ACTIVE,
        needs_review=needs_review,
    )
    ExternalRef.objects.create(
        entity_type=service.ENTITY_SCHEDULE,
        entity_id=row.pk,
        provider=service.PROVIDER_KEY,
        external_kind=service.KIND_SCHEDULE,
        external_id=f"sched-{uuid.uuid4().hex[:8]}",
    )
    return row


# --- The page --------------------------------------------------------------------------------


def test_the_page_shows_the_four_cards_the_identity_as_text_and_the_changes_card(
    admin: Client,
) -> None:
    lease = a_world()
    body = page(admin, lease)
    for name in ("lease-card", "rent-card", "collection-card", "billing-card"):
        assert f'id="{name}"' in body, name
    identity = card(body, "lease-card")
    assert "#1991 Istanbul" in identity and "Fatih Co" in identity
    assert "Jan 13, 2026" in identity and "Active</span>" in identity
    for name in ("store", "operator", "started_on", "collection_status"):
        assert f'name="{name}"' not in body, name
    assert tag_with(identity, 'id="transfer-lease"', f'href="/stores/{lease.store_id}/transfer/"')
    assert tag_with(identity, 'id="end-lease"', f'href="/stores/{lease.store_id}/end-lease/"')
    start = body.index('<aside class="form-summary"')
    changes = body[start : body.index("</aside>", start)]
    assert 'id="lease-changes"' in body and 'class="changes-list"' in changes
    assert changes.count("btn-primary") == 1
    assert tag_with(changes, 'id="save-lease"', 'type="submit"')
    assert ">Save changes</button>" in changes
    assert tag_with(changes, 'id="cancel-lease"', f'href="/leases/{lease.pk}/"')


def test_without_the_script_save_is_enabled_and_the_rent_fields_are_shown(admin: Client) -> None:
    body = page(admin, a_world())
    assert "Change a field above to see it here." in body
    save_button = next(tag for tag in re.findall(r"<[^>]+>", body) if 'id="save-lease"' in tag)
    assert "disabled" not in save_button
    fields = next(tag for tag in re.findall(r"<[^>]+>", body) if 'id="rent-change-fields"' in tag)
    assert "hidden" not in fields
    # "Change rent" is the script's: hidden in the markup.
    assert tag_with(body, 'id="change-rent"', "hidden")
    assert re.search(r'<script src="[^"]*ledger/edit_lease[^"]*\.js" defer>', body)


def test_every_editable_control_carries_its_initial_value_and_its_words(admin: Client) -> None:
    lease = a_world(debit_day=5, second_debit_day=20)
    body = page(admin, lease)
    assert tag_with(body, 'name="debit_day"', 'data-initial="5"', 'data-label="Debit day"')
    assert tag_with(
        body, 'name="second_debit_day"', 'data-initial="20"', 'data-label="Second debit day"'
    )
    assert tag_with(body, 'name="prorate_first_month"', 'data-initial="true"')
    assert tag_with(body, 'name="rent"', 'data-initial=""', 'data-label="Monthly rent"')
    assert tag_with(body, 'name="rent"', 'data-current="3500.00"', 'data-current-text="$3,500.00"')
    radios = [tag for tag in re.findall(r"<input[^>]+>", body) if 'name="payment_channel"' in tag]
    assert len(radios) == 3
    assert all(
        f'data-initial="{PaymentChannel.PROVIDER_ACH}"' in tag
        and 'data-label="Payment channel"' in tag
        for tag in radios
    )


def test_the_rent_in_effect_the_upcoming_one_and_the_history_newest_first(admin: Client) -> None:
    lease = a_world()
    save(admin, lease, rent="3600.00", rent_effective_from="2026-12-01")
    body = card(page(admin, lease), "rent-card")
    assert '$3,500.00</span> <span class="muted">a month since Jan 13, 2026' in body
    assert "Changes to $3,600.00 on Dec 1, 2026" in body
    # Re-pinned for PLAN-015b §3 C: the rows of the list itself (the latest five); fewer than
    # six rows, so no "Show all".
    rows = history_rows(body)
    assert len(rows) == 2
    assert "$3,600.00" in rows[0] and "from Dec 1, 2026" in rows[0]
    assert "$3,500.00" in rows[1] and "Jan 13, 2026 – Nov 30, 2026" in rows[1]
    assert_absent(body, "rent-history-more")


def history_rows(body: str) -> list[str]:
    """The rows of the History list (ul#rent-history), not those behind "Show all"."""
    history = body[body.index('id="rent-history"') :]
    return re.findall(r"<li>(.*?)</li>", history[: history.index("</ul>")], re.S)


def test_the_history_shows_the_latest_five_and_the_rest_behind_show_all(admin: Client) -> None:
    lease = a_world()
    first = LeaseRentHistory.objects.get(lease=lease)
    first.effective_to = date(2026, 1, 31)
    first.save(update_fields=["effective_to"])
    # Six more rows, a month each, then the open one: seven in all.
    for month in range(2, 8):
        LeaseRentHistory.objects.create(
            lease=lease,
            amount=D("3500.00") + month,
            effective_from=date(2026, month, 1),
            effective_to=date(2026, month + 1, 1) - timedelta(days=1) if month < 7 else None,
            source="staff",
        )
    assert LeaseRentHistory.objects.filter(lease=lease).count() == 7
    body = card(page(admin, lease), "rent-card")
    rows = history_rows(body)
    assert len(rows) == 5
    assert "$3,507.00" in rows[0] and "from Jul 1, 2026" in rows[0]
    assert "$3,503.00" in rows[4] and "Mar 1, 2026 – Mar 31, 2026" in rows[4]
    more = body[body.index('<details class="rent-history-more" id="rent-history-more">') :]
    more = more[: more.index("</details>")]
    assert "<summary>Show all (2 more)</summary>" in more
    hidden = re.findall(r"<li>(.*?)</li>", more, re.S)
    assert len(hidden) == 2
    assert "$3,502.00" in hidden[0] and "Feb 1, 2026 – Feb 28, 2026" in hidden[0]
    assert "$3,500.00" in hidden[1] and "Jan 13, 2026 – Jan 31, 2026" in hidden[1]


def test_exactly_five_rows_show_no_show_all(admin: Client) -> None:
    lease = a_world()
    first = LeaseRentHistory.objects.get(lease=lease)
    first.effective_to = date(2026, 1, 31)
    first.save(update_fields=["effective_to"])
    for month in range(2, 6):
        LeaseRentHistory.objects.create(
            lease=lease,
            amount=D("3500.00") + month,
            effective_from=date(2026, month, 1),
            effective_to=date(2026, month + 1, 1) - timedelta(days=1) if month < 5 else None,
            source="staff",
        )
    body = card(page(admin, lease), "rent-card")
    assert len(history_rows(body)) == 5
    assert_absent(body, "rent-history-more")


# --- Rent ------------------------------------------------------------------------------------


def test_a_rent_change_without_a_date_takes_effect_on_the_next_rent_due_date(
    admin: Client,
) -> None:
    lease = a_world(debit_day=5)
    body = page(admin, lease)
    assert tag_with(body, 'name="rent_effective_from"', 'value="2026-10-05"')
    save(admin, lease, rent="3650.00", rent_effective_from="")
    rows = list(LeaseRentHistory.objects.filter(lease=lease).order_by("effective_from"))
    assert [(row.amount, row.effective_from, row.effective_to) for row in rows] == [
        (D("3500.00"), START, date(2026, 10, 4)),
        (D("3650.00"), date(2026, 10, 5), None),
    ]


def test_a_lease_without_a_debit_day_defaults_to_the_first_of_next_month(admin: Client) -> None:
    lease = a_world(status=CollectionStatus.PROSPECTIVE)
    Lease.objects.filter(pk=lease.pk).update(debit_day=None)
    lease.refresh_from_db()
    body = page(admin, lease)
    assert tag_with(body, 'name="rent_effective_from"', 'value="2026-11-01"')
    # Such a lease may have no debit day: "—" is a choice, and it is the one chosen.
    assert '<option value="" selected>—</option>' in card(body, "collection-card")


def test_a_paused_lease_without_a_debit_day_saves_a_channel_change(admin: Client) -> None:
    """QA-015a finding: a save of such a lease answered 500 (planned_debits raised)."""
    lease = a_world(status=CollectionStatus.PAUSED)
    Lease.objects.filter(pk=lease.pk).update(debit_day=None)
    lease.refresh_from_db()
    response = admin.post(
        path(lease), data(lease, debit_day="", payment_channel=PaymentChannel.INVOICE)
    )
    assert (response.status_code, response["Location"]) == (302, f"/leases/{lease.pk}/")
    saved = Lease.objects.get(pk=lease.pk)
    assert (saved.debit_day, saved.payment_channel) == (None, PaymentChannel.INVOICE)
    # Clearing the day of a paused lease that had one saves too.
    other = a_lease(a_store("Paused day", 1993), an_operator("Paused Co"), status="paused")
    assert save(admin, other, debit_day="").debit_day is None


def test_an_empty_or_unchanged_rent_writes_no_history(admin: Client) -> None:
    lease = a_world()
    save(admin, lease, debit_day="6")
    save(admin, lease, rent="3500.00", rent_effective_from="2026-11-05")
    assert LeaseRentHistory.objects.filter(lease=lease).count() == 1


def test_a_rent_date_on_the_open_rows_start_is_refused_on_the_date_field(admin: Client) -> None:
    lease = a_world()
    response = admin.post(
        path(lease), data(lease, rent="3600.00", rent_effective_from=START.isoformat())
    )
    assert (response.status_code, response["Location"]) == (302, path(lease))
    body = page(admin, lease)
    rent = card(body, "rent-card")
    assert "at least two days after the current rent started (Jan 13, 2026)" in rent
    assert LeaseRentHistory.objects.filter(lease=lease).count() == 1


@pytest.mark.parametrize("rent", ["0", "-5", "10.001", "abc"])
def test_an_invalid_rent_is_refused_and_nothing_is_saved(admin: Client, rent: str) -> None:
    lease = a_world()
    response = admin.post(path(lease), data(lease, rent=rent, debit_day="6"))
    assert response.status_code == 302
    body = page(admin, lease)
    assert 'class="field has-error"' in card(body, "rent-card")
    for name in ("lease-card", "rent-card", "collection-card", "billing-card"):
        assert f'id="{name}"' in body, name
    lease.refresh_from_db()
    assert lease.debit_day == 5 and LeaseRentHistory.objects.filter(lease=lease).count() == 1


# --- Collection ------------------------------------------------------------------------------


def test_the_debit_day_is_the_1st_to_the_28th_and_29_or_31_is_refused(admin: Client) -> None:
    lease = a_world(debit_day=5)
    body = page(admin, lease)
    assert ">28th</option>" in body and ">29th</option>" not in body
    assert save(admin, lease, debit_day="28").debit_day == 28
    for day in ("29", "31", "0"):
        response = admin.post(path(lease), data(lease, debit_day=day))
        assert response.status_code == 302
        assert "Choose one of the options." in card(page(admin, lease), "collection-card")
        assert Lease.objects.get(pk=lease.pk).debit_day == 28, day


def test_a_current_debit_day_above_the_28th_is_kept_unchanged(admin: Client) -> None:
    lease = a_world(debit_day=30, channel=PaymentChannel.WIRE)
    body = page(admin, lease)
    assert ">30th (current)</option>" in body and ">31st" not in body
    saved = save(admin, lease, payment_channel=PaymentChannel.INVOICE)
    assert (saved.debit_day, saved.payment_channel) == (30, PaymentChannel.INVOICE)
    response = admin.post(path(lease), data(saved, debit_day="31"))
    assert response.status_code == 302
    assert Lease.objects.get(pk=lease.pk).debit_day == 30


def test_twice_a_month_sets_and_clears_the_second_day(admin: Client) -> None:
    lease = a_world(debit_day=1)
    assert save(admin, lease, second_debit_day="15").second_debit_day == 15
    # Unticked, the hidden select is disabled and not posted: one debit a month again.
    values = data(lease)
    del values["second_debit_day"]
    admin.post(path(lease), values)
    assert Lease.objects.get(pk=lease.pk).second_debit_day is None
    response = admin.post(path(lease), data(lease, second_debit_day="1"))
    assert response.status_code == 302
    assert "must differ from the first" in page(admin, lease)


def test_a_current_wire_channel_is_kept_and_zelle_is_never_offered(admin: Client) -> None:
    lease = a_world(channel=PaymentChannel.WIRE)
    body = card(page(admin, lease), "collection-card")
    assert "Wire (current)" in body and tag_with(body, 'value="wire"', "checked")
    assert not tag_with(body, 'name="payment_channel"', 'value="zelle"')
    assert save(admin, lease, debit_day="6").payment_channel == PaymentChannel.WIRE
    other = a_lease(a_store("Bank debit", 1992), an_operator("Debit Co"))
    response = admin.post(path(other), data(other, payment_channel=PaymentChannel.ZELLE))
    assert response.status_code == 302
    assert "Choose Autopay (bank debit)" in page(admin, other)
    assert Lease.objects.get(pk=other.pk).payment_channel == PaymentChannel.PROVIDER_ACH


def test_a_channel_change_saves(admin: Client) -> None:
    lease = a_world()
    assert save(admin, lease, payment_channel="other").payment_channel == PaymentChannel.OTHER


# --- Proration -------------------------------------------------------------------------------


def test_the_switch_saves_while_the_first_month_is_not_billed(admin: Client) -> None:
    lease = a_world()
    assert not service.first_month_billed(lease)
    assert 'name="prorate_first_month"' in card(page(admin, lease), "rent-card")
    values = data(lease)
    del values["prorate_first_month"]
    admin.post(path(lease), values)
    assert Lease.objects.get(pk=lease.pk).prorate_first_month is False


def test_once_the_first_month_is_billed_the_switch_is_gone_and_a_post_is_ignored(
    admin: Client,
) -> None:
    lease = a_world()
    charge = bill_first_month(lease)
    body = card(page(admin, lease), "rent-card")
    assert 'name="prorate_first_month"' not in body
    # Re-pinned (Opus, QA-015a): the billed state is a facts row, not a second line of help.
    row = body[body.index('<div id="prorate-billed">') :]
    row = row[: row.index("</div>")]
    assert "<dt>Prorate the first month</dt>" in row
    assert f"<dd>Billed on {day_text(charge.created_at)}, prorated</dd>" in row
    # Without the field a post that leaves the switch out (what "off" posts) changes nothing.
    values = data(lease, debit_day="6")
    del values["prorate_first_month"]
    admin.post(path(lease), values)
    saved = Lease.objects.get(pk=lease.pk)
    assert (saved.prorate_first_month, saved.debit_day) == (True, 6)
    # A first month billed in full reads "not prorated".
    Lease.objects.filter(pk=lease.pk).update(prorate_first_month=False)
    assert ", not prorated</dd>" in card(page(admin, lease), "rent-card")


# --- The adversarial post (the owner's test) -------------------------------------------------


def test_a_post_cannot_change_the_store_operator_status_start_or_a_billed_proration(
    admin: Client,
) -> None:
    lease = a_world()
    bill_first_month(lease)
    other_store, other_operator = a_store("Elsewhere", 2001), an_operator("Someone Else")
    before = (
        lease.store_id,
        lease.operator_id,
        lease.collection_status,
        lease.started_on,
        lease.prorate_first_month,
    )
    response = admin.post(
        path(lease),
        {
            **data(lease, debit_day="7"),
            "store": str(other_store.pk),
            "operator": str(other_operator.pk),
            "collection_status": CollectionStatus.ENDED,
            "started_on": "2020-02-02",
            "prorate_first_month": "",
        },
    )
    assert response.status_code == 302
    lease.refresh_from_db()
    after = (
        lease.store_id,
        lease.operator_id,
        lease.collection_status,
        lease.started_on,
        lease.prorate_first_month,
    )
    assert after == before and lease.debit_day == 7
    entry = AuditLog.objects.filter(action="lease.updated").latest("id")
    assert entry.before is not None and entry.after is not None
    changed = {key for key in entry.after if entry.after[key] != entry.before.get(key)}
    assert changed == {"debit_day"}


# --- Billing details and the autopay warning ---------------------------------------------------


def test_the_billing_summary_shows_the_cached_last_four_only(admin: Client) -> None:
    lease = a_world()
    LeaseBilling.objects.create(
        lease=lease,
        company_name="Fatih Trading LLC",
        email="billing@example.com",
        bank_last4="4821",
        bank_name="Chase",
    )
    body = page(admin, lease)
    billing = card(body, "billing-card")
    assert "Fatih Trading LLC · billing@example.com · Chase ····4821" in billing
    assert tag_with(billing, 'id="edit-billing"', f'href="/leases/{lease.pk}/billing/"')
    for needle in ("routing", "account_number", "011000015", "000123456789"):
        assert_absent(body, needle)


def test_a_lease_without_billing_says_no_bank_account_is_on_file(admin: Client) -> None:
    assert "no bank account on file" in card(page(admin, a_world()), "billing-card")


def test_the_autopay_warning_is_only_on_a_lease_with_a_live_provider_schedule(
    admin: Client,
) -> None:
    lease = a_world()
    assert 'id="autopay-warning"' not in page(admin, lease)
    # A schedule without a provider reference, an ended one or one already flagged: no warning.
    row = CollectionSchedule.objects.create(
        lease=lease, kind=ScheduleKind.RENT, seq=1, debit_day=5, amount=D("3500.00")
    )
    assert 'id="autopay-warning"' not in page(admin, lease)
    row.delete()
    flagged = a_live_schedule(lease, needs_review=True)
    assert 'id="autopay-warning"' not in page(admin, lease)
    CollectionSchedule.objects.filter(pk=flagged.pk).update(state=ScheduleState.ENDED)
    assert 'id="autopay-warning"' not in page(admin, lease)
    a_live_schedule(lease)
    body = page(admin, lease)
    assert tag_with(body, 'id="autopay-warning"', "hidden")
    assert "Autopay will be marked Needs review" in body


# --- Access ----------------------------------------------------------------------------------


@pytest.mark.parametrize("role", [Role.BILLING, Role.VIEWER])
def test_billing_and_viewers_are_refused_and_change_nothing(role: Role) -> None:
    client, _ = client_for(role, f"edit-lease-{role}")
    lease = a_world()
    assert client.get(path(lease)).status_code == 403
    assert client.post(path(lease), data(lease, debit_day="9")).status_code == 403
    assert Lease.objects.get(pk=lease.pk).debit_day == 5


def test_a_missing_lease_is_404(admin: Client) -> None:
    assert admin.get(f"/leases/{uuid.uuid4()}/edit/").status_code == 404
    assert admin.post(f"/leases/{uuid.uuid4()}/edit/", {}).status_code == 404

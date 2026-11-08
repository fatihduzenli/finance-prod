"""PLAN-015d, Fable's finding 2: free rent and autopay. A rent schedule debits a fixed amount
every month, so it never debits a month the free period touches: a new schedule starts on the
first debit day of the month after the free period's last month; a partly free month is
collected at its net by the invoice and reminder path (rule B, as without autopay) and the
LEASE card says so; a change of the free period on Edit lease flags the live schedules."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from accounts.roles import Role
from ledger.models import AuditLog, FreeRentFrom, MessageRule, RentChargeStatus
from ledger.services import free_rent, receivables, reminders, rent_run
from ledger.services import leases as service
from payments.fake import FakeProvider
from tests.test_lease_services import (
    CUSTOMER,
    RENT,
    creates,
    edit,
    enable,
    fake,
    make_lease,
    method,
    updates,
)
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

__all__ = ["fake"]  # the fixture, used by name
D = Decimal
OCTOBER_1 = date(2026, 10, 1)


def a_lease_on_autopay(fake: FakeProvider, days: int) -> Any:
    """A lease started 1 Oct 2026 (rent $1,500.00, debit day 15) with ``days`` of free rent
    from the move-in date, autopay enabled on 1 Oct 2026 at 15:00 UTC."""
    fake.add_payment_method(CUSTOMER, method())
    lease = make_lease(
        started_on=OCTOBER_1, free_rent_days=days, free_rent_from=FreeRentFrom.MOVE_IN
    )
    assert enable(lease).action is service.AutopayAction.ENABLED
    return lease


def test_a_free_first_month_starts_autopay_on_the_second_months_debit_day(
    fake: FakeProvider,
) -> None:
    lease = a_lease_on_autopay(fake, 31)  # 1–31 Oct free in full
    assert free_rent.autopay_from(lease) == date(2026, 11, 1)
    [schedule] = creates(fake)
    # Without free rent the first debit is 15 Oct (test_lease_services); now 15 Nov.
    assert schedule.rule == "DTSTART=20261115T120000Z;FREQ=MONTHLY"
    assert schedule.amounts.total == RENT


def test_without_free_rent_autopay_starts_on_the_next_debit_day(fake: FakeProvider) -> None:
    lease = a_lease_on_autopay(fake, 0)
    assert free_rent.autopay_from(lease) is None
    assert [row.rule for row in creates(fake)] == ["DTSTART=20261015T120000Z;FREQ=MONTHLY"]


def test_a_partly_free_month_gets_no_schedule_debit_and_a_rule_b_reminder_of_the_net(
    fake: FakeProvider,
) -> None:
    lease = a_lease_on_autopay(fake, 45)  # 1 Oct – 14 Nov: November partly free
    [schedule] = creates(fake)
    # Neither October (free) nor November (partly free) is debited: December is the first.
    assert schedule.rule == "DTSTART=20261215T120000Z;FREQ=MONTHLY"
    assert not free_rent.debited_by_autopay(lease, date(2026, 11, 1))
    assert free_rent.debited_by_autopay(lease, date(2026, 12, 1))
    # Three days before 15 Nov: B with the net (14 of 30 days free: $700.00 off $1,500.00).
    found = {
        month.lease.pk: (rule.code, month.amount)
        for month, rule in reminders.rent_months_due(date(2026, 11, 12))
    }
    assert found[lease.pk] == (MessageRule.B, D("800.00"))
    # The month's charge is made with its concession and is not counted as on autopay: it
    # takes C, D and E as any charge not debited.
    charge, _made = rent_run.create_month(lease, date(2026, 11, 1), None)
    assert (charge.amount_due, charge.concession, charge.status) == (
        D("1500.00"),
        D("700.00"),
        RentChargeStatus.DUE,
    )
    late = charge.due_on.toordinal() + reminders.DAY_C
    rows = [row for row in receivables.open_balances(date.fromordinal(late))]
    row = next(row for row in rows if row.charge_id == charge.pk)
    assert reminders.on_autopay([row]) == set()
    rule = reminders.rule_for(row, date.fromordinal(late))
    assert rule is not None and rule.code == MessageRule.C


def test_a_free_month_gets_no_reminder_and_a_debited_month_gets_a(fake: FakeProvider) -> None:
    lease = a_lease_on_autopay(fake, 45)
    october = {month.lease.pk for month, _rule in reminders.rent_months_due(date(2026, 10, 12))}
    assert lease.pk not in october  # free in full: nothing owed, nothing debited
    december = {
        month.lease.pk: (rule.code, month.amount)
        for month, rule in reminders.rent_months_due(date(2026, 12, 12))
    }
    assert december[lease.pk] == (MessageRule.A, RENT)


def test_the_lease_card_says_a_partly_free_month_is_invoiced(
    fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = a_lease_on_autopay(fake, 45)
    assert free_rent.card_note(lease, date(2026, 11, 5), autopay=True) == (
        free_rent.NO_AUTOPAY_DEBIT
    )
    assert free_rent.card_note(lease, date(2026, 11, 5), autopay=False) == ""
    assert free_rent.card_note(lease, date(2026, 10, 5), autopay=True) == ""  # free in full
    assert free_rent.card_note(lease, date(2026, 12, 5), autopay=True) == ""  # debited
    monkeypatch.setattr(
        "django.utils.timezone.localdate", lambda *args, **kwargs: date(2026, 11, 5)
    )
    page = client_for(Role.ADMIN, "admin-free-autopay")[0].get(f"/stores/{lease.store_id}/")
    text = page.content.decode()
    assert 'id="header-free-rent-note"' in text
    assert "Free rent: no autopay debit this month; the net is invoiced" in text


def test_changing_the_free_period_flags_the_live_schedule(fake: FakeProvider) -> None:
    lease = a_lease_on_autopay(fake, 0)
    [row] = service.live_schedules(lease)
    assert not row.needs_review
    edit(lease, RENT, free_rent_days=30, free_rent_from=FreeRentFrom.NEXT_MONTH)
    [row] = service.live_schedules(lease)
    assert (row.needs_review, row.amount) == (True, RENT)
    assert service.schedule_review_message(lease, row, date(2026, 10, 2)) == (
        service.SCHEDULE_DIFFERS
    )


def test_an_unchanged_free_period_does_not_flag_the_schedule(fake: FakeProvider) -> None:
    lease = a_lease_on_autopay(fake, 31)
    edit(lease, RENT, free_rent_days=31, free_rent_from=FreeRentFrom.MOVE_IN)
    [row] = service.live_schedules(lease)
    assert not row.needs_review


def test_resume_in_a_free_month_starts_after_the_free_period(fake: FakeProvider) -> None:
    """Fable's review: resume applies the same floor as enabling."""
    lease = a_lease_on_autopay(fake, 31)  # 1–31 Oct free in full; DTSTART 15 Nov
    service.pause_autopay(lease.pk, actor=None)
    # Resumed on 5 Oct: the next 15th is 15 Oct, a free month; the schedule starts 15 Nov.
    outcome = service.resume_autopay(
        lease.pk, actor=None, now=datetime(2026, 10, 5, 15, 0, tzinfo=UTC)
    )
    assert outcome.action is service.AutopayAction.RESUMED
    [(_ref, update)] = [item for item in updates(fake) if item[1].active]
    assert update.rule == "DTSTART=20261115T120000Z;FREQ=MONTHLY"
    audit = AuditLog.objects.get(action="lease.autopay_resumed", entity_id=str(lease.pk))
    assert audit.after["rule"] == update.rule


def test_resume_after_the_free_period_takes_the_next_debit_day(fake: FakeProvider) -> None:
    lease = a_lease_on_autopay(fake, 31)
    service.pause_autopay(lease.pk, actor=None)
    service.resume_autopay(lease.pk, actor=None, now=datetime(2026, 12, 20, 15, 0, tzinfo=UTC))
    [(_ref, update)] = [item for item in updates(fake) if item[1].active]
    assert update.rule == "DTSTART=20270115T120000Z;FREQ=MONTHLY"


def test_the_lease_card_names_the_first_rent_debit_after_the_free_period(
    fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fable's review: the LEASE card's next rent debit uses the same floor as the schedule."""
    from ledger.services import workspace

    lease = a_lease_on_autopay(fake, 31)  # 1–31 Oct free in full; DTSTART 15 Nov
    moment = datetime(2026, 10, 5, 15, 0, tzinfo=UTC)
    schedules = service.live_schedules(lease)
    assert workspace._next_rent(lease, schedules, None, moment) == date(2026, 11, 15)
    monkeypatch.setattr("django.utils.timezone.now", lambda: moment)
    monkeypatch.setattr(
        "django.utils.timezone.localdate", lambda *args, **kwargs: date(2026, 10, 5)
    )
    page = client_for(Role.ADMIN, "admin-free-next")[0].get(f"/stores/{lease.store_id}/")
    text = page.content.decode()
    assert 'id="header-next-payment">Nov 15, 2026</dd>' in text
    assert "Oct 15, 2026" not in text


#: The "Next debit" cell of both tables, as the responsive table renders it.
NEXT_DEBIT = 'data-label="Next debit">'


def _next_runs(
    lease: Any, moment: datetime, monkeypatch: pytest.MonkeyPatch, user: str
) -> tuple[date | None, date | None, str, str]:
    """The Schedules tab's and the lease page's next run (service), and both pages."""
    from ledger.services import workspace

    [row] = workspace.schedules_tab(lease.store, moment.date(), moment, Role.ADMIN)["schedule_rows"]
    monkeypatch.setattr("django.utils.timezone.now", lambda: moment)
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: moment.date())
    client = client_for(Role.ADMIN, user)[0]
    tab = client.get(f"/stores/{lease.store_id}/?tab=schedules").content.decode()
    page = client.get(f"/leases/{lease.pk}/")
    lease_row = page.context["schedules"][0]
    return row["next_run"].date(), lease_row["next_run"].date(), tab, page.content.decode()


def test_the_schedules_tab_and_the_lease_page_name_the_run_after_the_free_period(
    fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fable's review: both readouts use the same floor as the schedule's DTSTART."""
    lease = a_lease_on_autopay(fake, 31)  # 1–31 Oct free in full; DTSTART 15 Nov
    moment = datetime(2026, 10, 5, 15, 0, tzinfo=UTC)
    tab_run, lease_run, tab, page = _next_runs(lease, moment, monkeypatch, "admin-runs-free")
    assert (tab_run, lease_run) == (date(2026, 11, 15), date(2026, 11, 15))
    for text in (tab, page):
        assert f"{NEXT_DEBIT}Nov 15, 2026</td>" in text
        assert f"{NEXT_DEBIT}Oct 15, 2026</td>" not in text


def test_without_free_rent_both_readouts_name_the_next_debit_day(
    fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = a_lease_on_autopay(fake, 0)
    moment = datetime(2026, 10, 5, 15, 0, tzinfo=UTC)
    tab_run, lease_run, tab, page = _next_runs(lease, moment, monkeypatch, "admin-runs-none")
    assert (tab_run, lease_run) == (date(2026, 10, 15), date(2026, 10, 15))
    for text in (tab, page):
        assert f"{NEXT_DEBIT}Oct 15, 2026</td>" in text

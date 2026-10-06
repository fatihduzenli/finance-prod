"""Ending a lease (PLAN-010b Batch C, the gap carried from Batch B): the service rules,
the autopay refusal, the audit row, the page, the role gates, and "Resolve deposit"
appearing once the lease has ended. Nothing reaches the payment provider.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts import registry
from accounts.roles import Role
from ledger.models import (
    AuditLog,
    CollectionSchedule,
    CollectionStatus,
    Lease,
    ScheduleKind,
    ScheduleState,
)
from ledger.services import leases as service
from ledger.services.leases import LeaseRefused
from payments.models import ExternalRef
from tests.test_deposits import held_deposit
from tests.test_lease_services import make_lease
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

TODAY = date(2026, 10, 5)
STARTED = date(2026, 1, 1)
REASON = "Operator moved out; keys returned."
CARD = "4111 1111 1111 1111"


def a_lease() -> Lease:
    return make_lease(started_on=STARTED)


def schedule(lease: Lease, state: str = ScheduleState.ACTIVE, linked: bool = True) -> None:
    row = CollectionSchedule.objects.create(
        lease=lease,
        kind=ScheduleKind.RENT,
        seq=1,
        debit_day=15,
        amount=Decimal("100.00"),
        state=state,
    )
    if linked:
        ExternalRef.objects.create(
            entity_type=service.ENTITY_SCHEDULE,
            entity_id=row.pk,
            provider=service.PROVIDER_KEY,
            external_kind=service.KIND_SCHEDULE,
            external_id=f"fake-schedule-{uuid.uuid4().hex[:8]}",
        )


def end(lease: Lease, on: date | None = date(2026, 9, 30), reason: str = REASON) -> Lease:
    return service.end_lease(lease.pk, on, reason, actor=None, today=TODAY)


def refused(lease: Lease, **kwargs: object) -> LeaseRefused:
    audited = AuditLog.objects.filter(action="lease.ended").count()
    with pytest.raises(LeaseRefused) as caught:
        end(lease, **kwargs)  # type: ignore[arg-type]
    unchanged = Lease.objects.get(pk=lease.pk)
    assert (unchanged.ended_on, unchanged.end_reason) == (lease.ended_on, lease.end_reason)
    assert unchanged.collection_status == lease.collection_status
    assert AuditLog.objects.filter(action="lease.ended").count() == audited
    return caught.value


# --- The service ---------------------------------------------------------------------------


def test_ending_sets_the_date_the_reason_and_the_ended_status_and_is_audited() -> None:
    lease = a_lease()
    ended = end(lease)
    fresh = Lease.objects.get(pk=lease.pk)
    assert (fresh.ended_on, fresh.end_reason, fresh.collection_status) == (
        date(2026, 9, 30),
        REASON,
        CollectionStatus.ENDED,
    )
    assert ended.pk == lease.pk
    entry = AuditLog.objects.get(action="lease.ended")
    assert entry.entity_id == str(lease.pk)
    assert entry.before is not None and entry.after is not None
    assert (entry.before["ended_on"], entry.before["collection_status"]) == (None, "active")
    assert (entry.after["ended_on"], entry.after["end_reason"]) == ("2026-09-30", REASON)
    assert entry.after["collection_status"] == "ended"


@pytest.mark.parametrize("on", [STARTED, TODAY, date(2026, 3, 15)])
def test_the_start_date_today_and_any_day_between_are_allowed(on: date) -> None:
    assert end(a_lease(), on=on).ended_on == on


def test_a_lease_without_a_start_date_can_end_on_any_past_day() -> None:
    lease = make_lease(started_on=None)
    assert end(lease, on=date(2020, 1, 1)).ended_on == date(2020, 1, 1)


def test_a_date_in_the_future_is_refused() -> None:
    error = refused(a_lease(), on=TODAY + timedelta(days=1))
    assert (error.field, str(error)) == ("ended_on", service.END_IN_FUTURE)


def test_a_date_before_the_start_is_refused() -> None:
    error = refused(a_lease(), on=STARTED - timedelta(days=1))
    assert error.field == "ended_on"
    assert str(error) == "The end date cannot be before the lease started (2026-01-01)."


def test_a_missing_date_is_refused() -> None:
    assert str(refused(a_lease(), on=None)) == service.END_DATE_REQUIRED


@pytest.mark.parametrize("reason", ["", "   ", "​⁠﻿", " \n"])
def test_an_empty_or_invisible_reason_is_refused(reason: str) -> None:
    error = refused(a_lease(), reason=reason)
    assert (error.field, str(error)) == ("end_reason", service.END_REASON_REQUIRED)


def test_a_reason_with_a_card_number_is_refused() -> None:
    error = refused(a_lease(), reason=f"Paid with {CARD}")
    assert error.field == "end_reason"
    assert "card or account numbers" in str(error) and CARD not in str(error)


def test_the_reason_is_stored_trimmed() -> None:
    assert end(a_lease(), reason=f"​  {REASON}  ").end_reason == REASON


def test_a_lease_that_has_ended_cannot_end_again() -> None:
    lease = end(a_lease())
    error = refused(lease, on=date(2026, 9, 1))
    assert (error.field, str(error)) == (None, service.ALREADY_ENDED)
    assert Lease.objects.get(pk=lease.pk).ended_on == date(2026, 9, 30)
    assert AuditLog.objects.filter(action="lease.ended").count() == 1


def test_autopay_on_is_refused_with_the_pause_step_first() -> None:
    lease = a_lease()
    schedule(lease)
    error = refused(lease)
    assert str(error) == service.END_AUTOPAY_ON
    assert str(error).startswith("Autopay is on for this lease. Pause autopay first")


@pytest.mark.parametrize(
    ("state", "linked"),
    [
        (ScheduleState.PAUSED, True),
        (ScheduleState.ENDED, True),
        (ScheduleState.FAILED, False),
        (ScheduleState.NONE, False),
    ],
)
def test_a_paused_ended_or_never_created_schedule_does_not_block(state: str, linked: bool) -> None:
    lease = a_lease()
    schedule(lease, state, linked)
    assert end(lease).collection_status == CollectionStatus.ENDED


def test_an_ended_lease_cannot_be_enrolled_in_autopay() -> None:
    lease = end(a_lease())
    assert service.NOT_ACTIVE in service.autopay_blockers(lease)


# --- The page and the roles ------------------------------------------------------------------


def end_path(lease: Lease) -> str:
    return f"/leases/{lease.pk}/end/"


def yesterday() -> str:
    return (timezone.localdate() - timedelta(days=1)).isoformat()


def test_the_route_is_admin_only() -> None:
    rule = registry.rule_for("ledger:leases:end")
    assert rule is not None and rule.min_role is Role.ADMIN and rule.methods == {"POST"}


@pytest.mark.parametrize("role", [Role.VIEWER, Role.BILLING])
def test_billing_and_viewer_cannot_end_a_lease(role: Role) -> None:
    lease = a_lease()
    client, _ = client_for(role)
    detail = client.get(f"/leases/{lease.pk}/").content.decode()
    assert end_path(lease) not in detail
    assert client.get(end_path(lease)).status_code == 403
    response = client.post(end_path(lease), {"ended_on": yesterday(), "end_reason": REASON})
    assert response.status_code == 403
    assert Lease.objects.get(pk=lease.pk).ended_on is None


def test_a_user_without_a_role_is_refused() -> None:
    client, _ = client_for(None)
    assert client.get(end_path(a_lease())).status_code == 403


def test_an_admin_ends_a_lease_from_the_page_and_resolve_deposit_appears() -> None:
    deposit = held_deposit(ended=False)
    lease = deposit.lease
    assert lease is not None
    client, user = client_for(Role.ADMIN)
    detail_path = f"/leases/{lease.pk}/"
    resolve = f"/leases/{lease.pk}/deposits/{deposit.pk}/resolve/"
    before = client.get(detail_path).content.decode()
    assert f'href="{end_path(lease)}"' in before and resolve not in before
    form = client.get(end_path(lease)).content.decode()
    assert 'id="end-lease-form"' in form and 'id="autopay-on"' not in form
    response = client.post(end_path(lease), {"ended_on": yesterday(), "end_reason": REASON})
    assert (response.status_code, response["Location"]) == (302, detail_path)
    after = client.get(detail_path).content.decode()
    assert f'<dd id="lease-ended-on">{yesterday()}</dd>' in after
    assert REASON in after and "Lease ended." in after
    assert end_path(lease) not in after
    assert resolve in after
    assert AuditLog.objects.get(action="lease.ended").actor == user


def test_a_refusal_is_held_on_the_form() -> None:
    lease = a_lease()
    schedule(lease)
    client, _ = client_for(Role.ADMIN)
    assert 'id="autopay-on"' in client.get(end_path(lease)).content.decode()
    response = client.post(end_path(lease), {"ended_on": yesterday(), "end_reason": REASON})
    assert response["Location"] == end_path(lease)
    assert service.END_AUTOPAY_ON in client.get(end_path(lease)).content.decode()
    future = (timezone.localdate() + timedelta(days=1)).isoformat()
    client.post(end_path(lease), {"ended_on": future, "end_reason": REASON})
    assert service.END_IN_FUTURE in client.get(end_path(lease)).content.decode()
    client.post(end_path(lease), {"ended_on": "", "end_reason": ""})
    page = client.get(end_path(lease)).content.decode()
    assert service.END_DATE_REQUIRED in page and service.END_REASON_REQUIRED in page
    assert Lease.objects.get(pk=lease.pk).ended_on is None


def test_a_card_number_in_the_reason_is_refused_and_never_held() -> None:
    lease = a_lease()
    client, _ = client_for(Role.ADMIN)
    client.post(end_path(lease), {"ended_on": yesterday(), "end_reason": f"Paid with {CARD}"})
    page = client.get(end_path(lease)).content.decode()
    assert "may not contain card or account numbers" in page
    assert CARD not in page and CARD.replace(" ", "") not in page
    assert Lease.objects.get(pk=lease.pk).ended_on is None


def test_the_end_page_of_an_unknown_lease_is_404() -> None:
    client, _ = client_for(Role.ADMIN)
    assert client.get(f"/leases/{uuid.uuid4()}/end/").status_code == 404
    assert client.get("/leases/nope/end/").status_code == 404

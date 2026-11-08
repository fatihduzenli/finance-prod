"""PLAN-015b §3 A and B (docs/qa/15/PLAN-015b-lease-status.md), Opus's tests of the pages: the
LEASE card's Status row and its actions per status, the Pause collection, Resume collection
(§9.2) and Activate now dialogs over the store page, the queued autopay jobs, the roles, CSRF,
the refusals, a reason with markup escaped, and the lease list's Paused badge.

``payments.get_provider`` is a :class:`~payments.fake.FakeProvider`: no view or service may
call it (its call list stays empty); only ``Job`` rows are written.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

import pytest
from django.test import Client

from accounts.roles import Role
from ledger.models import (
    AuditLog,
    CollectionSchedule,
    CollectionStatus,
    Lease,
    LeaseRentHistory,
    RentCharge,
    ScheduleKind,
    ScheduleState,
)
from ledger.services import leases as service
from ledger.services import notes as note_service
from ledger.services import rent_run, timeline, workspace
from ledger.validators import PAYMENT_DATA_MESSAGE
from ledger.views import leases as views
from payments.fake import FakeProvider
from payments.models import ExternalRef, Job, JobKind
from tests.auth_helpers import make_user, verified_client
from tests.receivables_helpers import a_lease, a_rent_charge, a_store, an_operator
from tests.test_operator_views import client_for
from tests.text_helpers import assert_absent

pytestmark = pytest.mark.django_db

TODAY = date(2026, 11, 7)
CARD = "4111 1111 1111 1111"


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
def billing() -> Client:
    return client_for(Role.BILLING)[0]


@pytest.fixture
def admin() -> Client:
    return client_for(Role.ADMIN, "admin-lease-status")[0]


@pytest.fixture
def viewer() -> Client:
    return client_for(Role.VIEWER)[0]


def a_world(status: str = CollectionStatus.ACTIVE, **fields: Any) -> Lease:
    return a_lease(
        a_store("Istanbul", 1991),
        an_operator("Fatih Co"),
        started=date(2026, 9, 1),
        debit_day=1,
        status=status,
        **fields,
    )


def autopay_on(lease: Lease, state: str = ScheduleState.ACTIVE) -> CollectionSchedule:
    """A rent schedule of ``lease`` with a provider reference, in ``state``."""
    row = CollectionSchedule.objects.create(
        lease=lease, kind=ScheduleKind.RENT, seq=1, debit_day=1, amount="3500.00", state=state
    )
    ExternalRef.objects.create(
        entity_type=service.ENTITY_SCHEDULE,
        entity_id=row.pk,
        provider=service.PROVIDER_KEY,
        external_kind=service.KIND_SCHEDULE,
        external_id="sch-walk-1",
    )
    return row


def store_url(lease: Lease) -> str:
    return f"/stores/{lease.store_id}/"


def pause_url(lease: Lease) -> str:
    return f"/leases/{lease.pk}/pause-collection/"


def resume_url(lease: Lease) -> str:
    return f"/leases/{lease.pk}/resume-collection/"


def activate_url(lease: Lease) -> str:
    return f"/leases/{lease.pk}/activate/"


def body(client: Client, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code)
    return response.content.decode()


def status_row(html: str) -> str:
    start = html.index('id="header-status"')
    return html[start : html.index("</dd>", start)]


def jobs_of(lease: Lease) -> list[str]:
    return list(
        Job.objects.filter(entity_id=lease.pk).order_by("created_at").values_list("kind", flat=True)
    )


def status_actions(lease: Lease) -> list[str]:
    return list(
        AuditLog.objects.filter(
            entity_type="lease", entity_id=str(lease.pk), action__in=service.STATUS_ACTIONS
        )
        .order_by("id")
        .values_list("action", flat=True)
    )


# --- The LEASE card's Status row -------------------------------------------------------------


def test_an_active_lease_shows_active_and_pause_collection_to_admin(admin: Client) -> None:
    lease = a_world()
    row = status_row(body(admin, store_url(lease)))
    assert 'class="badge badge-paid"' in row and "Active</span>" in row
    # PLAN-015b §9.4: the card's words are "Pause" and "Resume"; the ids are unchanged.
    assert f'id="pause-collection" href="{pause_url(lease)}">Pause</a>' in row
    assert_absent(row, "resume-collection")
    assert_absent(row, "activate-lease")
    assert_absent(row, "pause-reason")


def test_a_paused_lease_shows_paused_since_its_reason_and_resume(admin: Client) -> None:
    lease = a_world()
    service.pause_collection(lease.pk, reason="Store closed for repairs.", actor=None)
    row = status_row(body(admin, store_url(lease)))
    assert 'class="badge badge-pending"' in row and "Paused</span>" in row
    assert f'id="resume-collection" href="{resume_url(lease)}">Resume</a>' in row
    assert 'id="pause-reason">Paused since Nov 7, 2026 — Store closed for repairs.</span>' in row
    assert_absent(row, "pause-collection")


@pytest.mark.parametrize("status", [CollectionStatus.ACTIVE, CollectionStatus.PAUSED])
def test_billing_sees_the_status_and_no_pause_or_resume(billing: Client, status: str) -> None:
    # PLAN-015b §9.3: Pause and Resume collection are admin's.
    lease = a_world(status)
    row = status_row(body(billing, store_url(lease)))
    assert '<span class="badge badge-' in row
    for action in ("pause-collection", "resume-collection", "activate-lease"):
        assert_absent(row, action)


def test_a_prospective_lease_shows_prospective_and_activate_now(billing: Client) -> None:
    lease = a_world(CollectionStatus.PROSPECTIVE)
    row = status_row(body(billing, store_url(lease)))
    assert 'class="badge badge-neutral"' in row and "Prospective</span>" in row
    assert f'id="activate-lease" href="{activate_url(lease)}">Activate now</a>' in row


def test_a_store_whose_lease_ended_has_no_status_row(billing: Client) -> None:
    # The LEASE card says "No current lease" (lease_end.current_lease); nothing to act on.
    lease = a_world(CollectionStatus.ENDED)
    html = body(billing, store_url(lease))
    assert "No current lease" in html
    for action in ("header-status", "pause-collection", "resume-collection", "activate-lease"):
        assert_absent(html, action)


def test_the_ended_status_badge_tone_is_neutral() -> None:
    assert workspace.STATUS_TONES[CollectionStatus.ENDED] == "neutral"


def test_a_store_with_a_current_lease_shows_it_rather_than_an_upcoming_one(
    billing: Client,
) -> None:
    current = a_world()
    upcoming = a_lease(
        current.store, an_operator("Next Co"), started=date(2026, 12, 1), status="prospective"
    )
    row = status_row(body(billing, store_url(current)))
    assert "Active</span>" in row
    assert_absent(row, "activate-lease")
    assert str(upcoming.pk) not in row


def test_the_earliest_upcoming_prospective_lease_is_the_status_rows(billing: Client) -> None:
    later = a_world(CollectionStatus.PROSPECTIVE)
    Lease.objects.filter(pk=later.pk).update(started_on=date(2027, 1, 1))
    sooner = a_lease(
        later.store, an_operator("Soon Co"), started=date(2026, 12, 1), status="prospective"
    )
    row = status_row(body(billing, store_url(later)))
    assert f'href="{activate_url(sooner)}"' in row


@pytest.mark.parametrize(
    "status", [CollectionStatus.ACTIVE, CollectionStatus.PAUSED, CollectionStatus.PROSPECTIVE]
)
def test_a_viewer_sees_the_status_and_no_action(viewer: Client, status: str) -> None:
    lease = a_world(status)
    row = status_row(body(viewer, store_url(lease)))
    assert '<span class="badge badge-' in row
    for action in ("pause-collection", "resume-collection", "activate-lease"):
        assert_absent(row, action)


def test_a_reason_with_markup_is_escaped_on_the_card(billing: Client) -> None:
    lease = a_world()
    service.pause_collection(lease.pk, reason='<script>alert("x")</script> Closed', actor=None)
    html = body(billing, store_url(lease))
    assert "<script>alert(" not in html
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; Closed" in status_row(html)


def test_the_lease_list_badge_reads_paused(billing: Client) -> None:
    lease = a_world()
    service.pause_collection(lease.pk, reason="Store closed.", actor=None)
    html = body(billing, "/leases/")
    assert '<span class="badge badge-pending"><span class="badge-icon" aria-hidden="true">' in html
    assert "Paused</span></td>" in html


# --- Pause collection ------------------------------------------------------------------------


def test_the_pause_dialog_opens_over_the_store_page_with_its_reason_field(
    admin: Client,
) -> None:
    lease = a_world()
    html = body(admin, pause_url(lease))
    assert 'id="pause-collection-dialog"' in html and 'id="store-title"' in html
    assert "Are you sure?" in html and "What will happen" in html
    assert 'name="reason"' in html and "Required: why collection is paused." in html
    assert 'id="pause-collection-submit">Pause collection</button>' in html
    assert f'href="{store_url(lease)}" id="pause-collection-keep">Keep collecting</a>' in html
    # Autopay is off: the dialog does not speak of pausing it.
    assert_absent(html, "its schedules are paused")


def test_the_pause_dialog_says_autopay_will_be_paused_when_it_is_on(admin: Client) -> None:
    lease = a_world()
    autopay_on(lease)
    assert "Autopay is on: its schedules are paused in" in body(admin, pause_url(lease))


def test_pausing_without_autopay_pauses_and_queues_nothing(
    admin: Client, fake: FakeProvider
) -> None:
    lease = a_world()
    response = admin.post(pause_url(lease), {"reason": "Store closed for repairs."})
    assert response.status_code == 302 and response["Location"] == store_url(lease)
    lease.refresh_from_db()
    assert lease.collection_status == CollectionStatus.PAUSED
    assert jobs_of(lease) == [] and fake.calls == []
    assert status_actions(lease) == ["lease.collection_paused"]
    page = admin.get(store_url(lease)).content.decode()
    assert views.COLLECTION_PAUSED in page


def test_pausing_with_autopay_on_queues_its_pause_and_calls_nothing(
    admin: Client, fake: FakeProvider
) -> None:
    lease = a_world()
    autopay_on(lease)
    response = admin.post(pause_url(lease), {"reason": "Owner asked."}, follow=True)
    assert views.COLLECTION_PAUSED_AUTOPAY in response.content.decode()
    assert jobs_of(lease) == [JobKind.AUTOPAY_PAUSE]
    assert fake.calls == []
    # The schedule stays active until the worker runs the job.
    assert CollectionSchedule.objects.get(lease=lease).state == ScheduleState.ACTIVE


@pytest.mark.parametrize(
    ("reason", "message"),
    [
        ("", "A reason is required: say why collection is paused."),
        ("​  ", "A reason is required: say why collection is paused."),
        (f"Card {CARD}", PAYMENT_DATA_MESSAGE),
        ("x" * 2001, "Ensure this value has at most 2000 characters (it has 2001)."),
    ],
    ids=["empty", "invisible", "card number", "too long"],
)
def test_a_bad_reason_re_renders_the_dialog_and_changes_nothing(
    admin: Client, reason: str, message: str
) -> None:
    lease = a_world()
    response = admin.post(pause_url(lease), {"reason": reason})
    # The form is held for the dialog's next GET; a reason with payment data is dropped.
    assert response.status_code == 302 and response["Location"] == pause_url(lease)
    html = body(admin, pause_url(lease))
    assert 'id="pause-collection-dialog"' in html and message in html
    assert CARD not in html
    assert "4111111111111111" not in html
    lease.refresh_from_db()
    assert lease.collection_status == CollectionStatus.ACTIVE
    assert status_actions(lease) == [] and jobs_of(lease) == []


@pytest.mark.parametrize(
    "status", [CollectionStatus.PAUSED, CollectionStatus.PROSPECTIVE, CollectionStatus.ENDED]
)
def test_the_pause_dialog_refuses_a_lease_that_is_not_active(admin: Client, status: str) -> None:
    lease = a_world(status)
    html = body(admin, pause_url(lease))
    assert f'id="pause-collection-refusal"><strong>{service.PAUSE_NOT_ACTIVE}</strong>' in html
    assert_absent(html, "pause-collection-submit")
    response = admin.post(pause_url(lease), {"reason": "Owner asked."}, follow=True)
    html = response.content.decode()
    assert html.count(service.PAUSE_NOT_ACTIVE) == 1
    lease.refresh_from_db()
    assert lease.collection_status == status and status_actions(lease) == []


# --- Resume collection -----------------------------------------------------------------------


def test_resuming_resumes_charges_the_paused_months_and_queues_nothing_without_autopay(
    admin: Client, fake: FakeProvider
) -> None:
    lease = a_world()
    service.pause_collection(lease.pk, reason="Owner asked.", actor=None)
    response = admin.post(resume_url(lease), follow=True)
    assert response.redirect_chain[-1][0] == store_url(lease)
    assert views.COLLECTION_RESUMED in response.content.decode()
    lease.refresh_from_db()
    assert lease.collection_status == CollectionStatus.ACTIVE
    months = sorted(RentCharge.objects.filter(lease=lease).values_list("period_month", flat=True))
    assert months == [date(2026, 9, 1), date(2026, 10, 1), date(2026, 11, 1)]
    assert jobs_of(lease) == [] and fake.calls == []


def test_resuming_after_a_pause_that_paused_autopay_queues_its_resume(
    admin: Client, fake: FakeProvider
) -> None:
    lease = a_world()
    schedule = autopay_on(lease)
    admin.post(pause_url(lease), {"reason": "Owner asked."})
    # The worker ran the pause job.
    CollectionSchedule.objects.filter(pk=schedule.pk).update(state=ScheduleState.PAUSED)
    response = admin.post(resume_url(lease), follow=True)
    assert views.COLLECTION_RESUMED_AUTOPAY in response.content.decode()
    assert jobs_of(lease) == [JobKind.AUTOPAY_PAUSE, JobKind.AUTOPAY_RESUME]
    assert fake.calls == []


@pytest.mark.parametrize(
    "status", [CollectionStatus.ACTIVE, CollectionStatus.PROSPECTIVE, CollectionStatus.ENDED]
)
def test_resume_is_refused_unless_paused(admin: Client, status: str) -> None:
    lease = a_world(status)
    response = admin.post(resume_url(lease), follow=True)
    assert service.RESUME_NOT_PAUSED in response.content.decode()
    lease.refresh_from_db()
    assert lease.collection_status == status
    assert status_actions(lease) == [] and jobs_of(lease) == []


def test_the_resume_dialog_opens_over_the_store_page_and_says_what_it_bills(
    admin: Client,
) -> None:
    lease = a_world()
    service.pause_collection(lease.pk, reason="Owner asked.", actor=None)
    html = body(admin, resume_url(lease))
    assert 'id="resume-collection-dialog"' in html and 'id="store-title"' in html
    assert "Are you sure?" in html and "What will happen" in html
    what = html[html.index('id="resume-collection-what"') :]
    assert what.index('id="resume-collection-bills">') < what.index("becomes Active")
    assert 'id="resume-collection-bills">Resuming will bill 3 paused months: $10,500.00.<' in html
    assert 'id="resume-collection-submit">Resume collection</button>' in html
    assert f'href="{store_url(lease)}" id="resume-collection-keep">Keep it paused</a>' in html
    # Asking writes nothing.
    assert not RentCharge.objects.filter(lease=lease).exists()
    lease.refresh_from_db()
    assert lease.collection_status == CollectionStatus.PAUSED
    assert status_actions(lease) == ["lease.collection_paused"]


def test_the_resume_dialog_with_every_month_billed_says_it_bills_nothing(
    admin: Client,
) -> None:
    lease = a_world()
    for month in (date(2026, 9, 1), date(2026, 10, 1), date(2026, 11, 1)):
        a_rent_charge(lease, month)
    service.pause_collection(lease.pk, reason="Owner asked.", actor=None)
    html = body(admin, resume_url(lease))
    assert 'id="resume-collection-bills">Resuming will bill nothing.<' in html
    assert_absent(html, "Resuming will bill 0")
    admin.post(resume_url(lease))
    assert RentCharge.objects.filter(lease=lease).count() == 3


def test_the_resume_dialog_says_one_paused_month(admin: Client) -> None:
    lease = a_world()
    for month in (date(2026, 9, 1), date(2026, 10, 1)):
        a_rent_charge(lease, month)
    service.pause_collection(lease.pk, reason="Owner asked.", actor=None)
    html = body(admin, resume_url(lease))
    assert 'id="resume-collection-bills">Resuming will bill 1 paused month: $3,500.00.<' in html


def test_the_resume_dialog_counts_a_month_debited_twice_once(admin: Client) -> None:
    lease = a_world(second_debit_day=15)
    service.pause_collection(lease.pk, reason="Owner asked.", actor=None)
    html = body(admin, resume_url(lease))
    # Sep 1 and 15, Oct 1 and 15, Nov 1 (Nov 15 is after today): five halves of $3,500.00.
    assert 'id="resume-collection-bills">Resuming will bill 3 paused months: $8,750.00.<' in html


def test_resuming_bills_exactly_the_months_the_dialog_named(admin: Client) -> None:
    lease = a_world(second_debit_day=15)
    a_rent_charge(lease, date(2026, 9, 1), due="1750.00")
    service.pause_collection(lease.pk, reason="Owner asked.", actor=None)
    lease.refresh_from_db()
    planned = rent_run.months_resume_would_create(lease, TODAY)
    assert views.resume_bills(lease, TODAY) == ("Resuming will bill 3 paused months: $7,000.00.")
    admin.post(resume_url(lease))
    made = RentCharge.objects.filter(lease=lease).exclude(period_month=date(2026, 9, 1), seq=1)
    assert sorted((c.period_month, c.seq, c.due_on, c.amount_due) for c in made) == sorted(
        (plan.period_month, plan.seq, plan.due_on, plan.amount) for plan in planned
    )
    lease.refresh_from_db()
    assert rent_run.months_resume_would_create(lease, TODAY) == []
    assert rent_run.months_catch_up_would_create(lease, TODAY) == []


@pytest.mark.parametrize(
    "status", [CollectionStatus.ACTIVE, CollectionStatus.PROSPECTIVE, CollectionStatus.ENDED]
)
def test_the_resume_preview_is_empty_unless_the_lease_is_paused(status: str) -> None:
    lease = a_world(status)
    assert rent_run.months_resume_would_create(lease, TODAY) == []


@pytest.mark.parametrize(
    "status", [CollectionStatus.ACTIVE, CollectionStatus.PROSPECTIVE, CollectionStatus.ENDED]
)
def test_the_resume_dialog_refuses_a_lease_that_is_not_paused(admin: Client, status: str) -> None:
    lease = a_world(status)
    html = body(admin, resume_url(lease))
    assert f'id="resume-collection-refusal"><strong>{service.RESUME_NOT_PAUSED}</strong>' in html
    assert_absent(html, "resume-collection-submit")
    assert_absent(html, "Resuming will bill")


def test_the_resume_dialog_refuses_a_paused_lease_missing_a_requirement(
    admin: Client,
) -> None:
    lease = a_world(CollectionStatus.PAUSED)
    Lease.objects.filter(pk=lease.pk).update(operator=None)
    html = body(admin, resume_url(lease))
    assert f"<strong>{service.NO_OPERATOR}</strong>" in html
    assert_absent(html, "resume-collection-submit")
    admin.post(resume_url(lease))
    lease.refresh_from_db()
    assert lease.collection_status == CollectionStatus.PAUSED
    assert not RentCharge.objects.filter(lease=lease).exists()


# --- Activate now ----------------------------------------------------------------------------


def test_the_activate_dialog_says_the_start_date_moves_to_today(billing: Client) -> None:
    lease = a_world(CollectionStatus.PROSPECTIVE)
    Lease.objects.filter(pk=lease.pk).update(started_on=date(2026, 11, 20))
    html = body(billing, activate_url(lease))
    assert 'id="activate-dialog"' in html and 'id="store-title"' in html
    assert "The start date becomes Nov 7, 2026 (it was Nov 20, 2026)." in html
    assert "The first month is billed now" in html
    assert 'id="activate-submit">Activate now</button>' in html


def test_activating_makes_the_lease_active_and_bills_it(
    billing: Client, fake: FakeProvider
) -> None:
    lease = a_world(CollectionStatus.PROSPECTIVE)
    Lease.objects.filter(pk=lease.pk).update(started_on=date(2026, 11, 20))
    LeaseRentHistory.objects.filter(lease=lease).update(effective_from=date(2026, 11, 20))
    response = billing.post(activate_url(lease), follow=True)
    assert response.redirect_chain[-1][0] == store_url(lease)
    assert views.LEASE_ACTIVATED in response.content.decode()
    lease.refresh_from_db()
    assert (lease.collection_status, lease.started_on) == (CollectionStatus.ACTIVE, TODAY)
    assert RentCharge.objects.filter(lease=lease, due_on=TODAY).count() == 1
    [row] = AuditLog.objects.filter(entity_id=str(lease.pk), action="lease.activated")
    assert row.after is not None and row.after["trigger"] == "staff"
    assert fake.calls == []


def test_the_activate_dialog_on_the_start_date_keeps_it(billing: Client) -> None:
    lease = a_world(CollectionStatus.PROSPECTIVE)
    Lease.objects.filter(pk=lease.pk).update(started_on=TODAY)
    assert "The start date stays Nov 7, 2026." in body(billing, activate_url(lease))


@pytest.mark.parametrize(
    "status", [CollectionStatus.ACTIVE, CollectionStatus.PAUSED, CollectionStatus.ENDED]
)
def test_the_activate_dialog_refuses_a_lease_that_is_not_prospective(
    billing: Client, status: str
) -> None:
    lease = a_world(status)
    html = body(billing, activate_url(lease))
    assert f'id="activate-refusal"><strong>{service.NOT_PROSPECTIVE}</strong>' in html
    assert_absent(html, "activate-submit")
    response = billing.post(activate_url(lease))
    assert response.status_code == 200
    assert service.NOT_PROSPECTIVE in response.content.decode()
    lease.refresh_from_db()
    assert lease.collection_status == status and status_actions(lease) == []


def test_the_activate_dialog_refuses_a_lease_missing_a_requirement(billing: Client) -> None:
    lease = a_world(CollectionStatus.PROSPECTIVE)
    Lease.objects.filter(pk=lease.pk).update(operator=None)
    html = body(billing, activate_url(lease))
    assert f"<strong>{service.NO_OPERATOR}</strong>" in html
    billing.post(activate_url(lease))
    lease.refresh_from_db()
    assert lease.collection_status == CollectionStatus.PROSPECTIVE


# --- Roles, CSRF, a missing lease ------------------------------------------------------------


@pytest.mark.parametrize("route", ["pause", "resume", "activate"])
def test_a_viewers_get_and_post_are_403_and_change_nothing(viewer: Client, route: str) -> None:
    status = {
        "pause": CollectionStatus.ACTIVE,
        "resume": CollectionStatus.PAUSED,
        "activate": CollectionStatus.PROSPECTIVE,
    }[route]
    lease = a_world(status)
    path = {"pause": pause_url, "resume": resume_url, "activate": activate_url}[route](lease)
    data = {"reason": "Owner asked."} if route == "pause" else {}
    assert viewer.post(path, data).status_code == 403
    assert viewer.get(path).status_code == 403
    lease.refresh_from_db()
    assert lease.collection_status == status
    assert status_actions(lease) == [] and jobs_of(lease) == []


@pytest.mark.parametrize("route", ["pause", "resume"])
def test_billings_get_and_post_to_pause_and_resume_are_403_and_change_nothing(
    billing: Client, route: str
) -> None:
    # PLAN-015b §9.3: admin only, as the autopay pause and resume.
    status = CollectionStatus.ACTIVE if route == "pause" else CollectionStatus.PAUSED
    lease = a_world(status)
    autopay_on(lease, ScheduleState.ACTIVE if route == "pause" else ScheduleState.PAUSED)
    path = pause_url(lease) if route == "pause" else resume_url(lease)
    assert billing.get(path).status_code == 403
    assert billing.post(path, {"reason": "Owner asked."}).status_code == 403
    lease.refresh_from_db()
    assert lease.collection_status == status
    assert status_actions(lease) == [] and jobs_of(lease) == []
    assert not RentCharge.objects.filter(lease=lease).exists()


def test_billing_may_activate(billing: Client) -> None:
    lease = a_world(CollectionStatus.PROSPECTIVE)
    assert billing.get(activate_url(lease)).status_code == 200
    assert billing.post(activate_url(lease)).status_code == 302
    lease.refresh_from_db()
    assert lease.collection_status == CollectionStatus.ACTIVE


@pytest.mark.parametrize("route", ["pause", "resume", "activate"])
def test_a_post_without_the_csrf_token_is_refused(route: str) -> None:
    status = {
        "pause": CollectionStatus.ACTIVE,
        "resume": CollectionStatus.PAUSED,
        "activate": CollectionStatus.PROSPECTIVE,
    }[route]
    lease = a_world(status)
    # The role each route needs, so the token is what refuses the POST.
    role = Role.BILLING if route == "activate" else Role.ADMIN
    user, device = make_user(f"csrf-{route}", role)
    assert device is not None
    client = verified_client(user, device, Client(enforce_csrf_checks=True))
    path = {"pause": pause_url, "resume": resume_url, "activate": activate_url}[route](lease)
    assert client.post(path, {"reason": "Owner asked."}).status_code == 403
    lease.refresh_from_db()
    assert lease.collection_status == status and status_actions(lease) == []


@pytest.mark.parametrize(
    "path",
    [
        "/leases/00000000-0000-0000-0000-000000000000/pause-collection/",
        "/leases/00000000-0000-0000-0000-000000000000/resume-collection/",
        "/leases/00000000-0000-0000-0000-000000000000/activate/",
        "/leases/not-a-uuid/pause-collection/",
    ],
)
def test_a_missing_lease_is_404(admin: Client, path: str) -> None:
    assert admin.get(path).status_code == 404


def test_a_post_to_a_missing_lease_is_404(admin: Client) -> None:
    missing = "/leases/00000000-0000-0000-0000-000000000000/resume-collection/"
    assert admin.post(missing).status_code == 404


# --- Notes & follow-ups: the status changes (PLAN-015b §3 B) ---------------------------------


def entries_of(html: str) -> list[str]:
    """The ids of the list's entries, in order."""
    block = html[html.index('id="contact-entries"') :]
    block = block[: block.index("</ul>")]
    return re.findall(r'<li class="contact-entry[^"]*" id="([^"]+)"', block)


def changes_of(lease: Lease) -> list[AuditLog]:
    return list(
        AuditLog.objects.filter(
            entity_type="lease", entity_id=str(lease.pk), action__in=service.STATUS_ACTIONS
        ).order_by("id")
    )


def test_the_tab_lists_pause_and_resume_with_a_note_newest_first(billing: Client) -> None:
    _, staff = client_for(Role.BILLING, "pause-staff")
    lease = a_world()
    service.pause_collection(lease.pk, reason="Store closed for repairs.", actor=staff)
    note = note_service.add(lease.store, "Called the operator.", staff)
    service.resume_collection(lease.pk, actor=staff, today=TODAY)
    paused, resumed = changes_of(lease)
    html = body(billing, f"{store_url(lease)}?tab=contacts")
    assert entries_of(html) == [f"status-{resumed.pk}", f"note-{note.pk}", f"status-{paused.pk}"]
    block = html[html.index(f'id="status-{paused.pk}"') :]
    block = block[: block.index("</li>")]
    assert "Collection paused — Store closed for repairs." in block
    assert 'class="badge badge-pending"' in block and "pause-staff" in block
    block = html[html.index(f'id="status-{resumed.pk}"') :]
    block = block[: block.index("</li>")]
    assert "Collection resumed" in block and 'class="badge badge-paid"' in block


def test_an_activation_reads_start_date_or_by_staff(billing: Client) -> None:
    by_job = a_world(CollectionStatus.PROSPECTIVE)
    Lease.objects.filter(pk=by_job.pk).update(started_on=TODAY)
    service.activate_due(TODAY)
    html = body(billing, f"{store_url(by_job)}?tab=contacts")
    assert "Lease activated (start date)" in html and "The daily job" in html
    by_staff = a_lease(
        a_store("Ankara", 1992), an_operator("Ankara Co"), started=TODAY, status="prospective"
    )
    service.activate(by_staff.pk, actor=None, today=TODAY)
    assert "Lease activated (by staff)" in body(billing, f"{store_url(by_staff)}?tab=contacts")


def test_the_lease_status_filter_lists_only_the_status_changes(billing: Client) -> None:
    lease = a_world()
    _, staff = client_for(Role.BILLING)
    note_service.add(lease.store, "Called the operator.", staff)
    service.pause_collection(lease.pk, reason="Owner asked.", actor=None)
    [paused] = changes_of(lease)
    html = body(billing, f"{store_url(lease)}?tab=contacts&show=status")
    assert entries_of(html) == [f"status-{paused.pk}"]
    assert ">Lease status</a>" in html
    notes_only = body(billing, f"{store_url(lease)}?tab=contacts&show=notes")
    assert all(entry.startswith("note-") for entry in entries_of(notes_only))
    assert len(entries_of(notes_only)) == 1


def test_the_status_filter_with_none_says_so(billing: Client) -> None:
    lease = a_world()
    html = body(billing, f"{store_url(lease)}?tab=contacts&show=status")
    assert 'id="timeline-empty">No status changes yet.</li>' in html


def test_the_all_entries_page_counts_and_lists_the_status_changes(billing: Client) -> None:
    lease = a_world()
    service.pause_collection(lease.pk, reason="Owner asked.", actor=None)
    service.resume_collection(lease.pk, actor=None, today=TODAY)
    assert timeline.Listing(lease.store, "all", include_deleted=False).count() == 2
    assert timeline.Listing(lease.store, "status", include_deleted=False).count() == 2
    assert timeline.Listing(lease.store, "notes", include_deleted=False).count() == 0
    html = body(billing, f"/stores/{lease.store_id}/contacts/")
    assert len([entry for entry in entries_of(html) if entry.startswith("status-")]) == 2


def test_another_stores_status_changes_are_not_listed(billing: Client) -> None:
    lease = a_world()
    other = a_lease(a_store("Izmir", 1993), an_operator("Izmir Co"))
    service.pause_collection(other.pk, reason="Not this store.", actor=None)
    html = body(billing, f"{store_url(lease)}?tab=contacts")
    assert_absent(html, "Not this store.")
    assert timeline.Listing(lease.store, "status", include_deleted=False).count() == 0


def test_a_reason_with_markup_is_escaped_in_the_timeline(billing: Client) -> None:
    lease = a_world()
    service.pause_collection(lease.pk, reason="<b>Closed</b> & gone", actor=None)
    html = body(billing, f"{store_url(lease)}?tab=contacts")
    assert "Collection paused — &lt;b&gt;Closed&lt;/b&gt; &amp; gone" in html
    assert "<b>Closed</b>" not in html


def test_the_charges_tab_card_keeps_its_contact_entries_only(billing: Client) -> None:
    lease = a_world()
    service.pause_collection(lease.pk, reason="Owner asked.", actor=None)
    html = body(billing, store_url(lease))
    card = html[html.index('id="contact-card"') :]
    card = card[: card.index("</section>")]
    assert_absent(card, "Collection paused")

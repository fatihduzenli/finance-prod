"""The Autopay tab's "Repeating debits" card and its actions (docs/qa/PLAN-012f-a D14, D15):
the rows, their line and state badge; the actions each role sees and may take (billing pauses
and resumes, an admin stops and changes the end; a viewer nothing); a plan of another store in
the URL is 404 and changes nothing; the Change end form's validation; the plan's audit rows on
the History tab; the daily-run page's card; the help paragraph; the tab's query budget. Nothing
reaches a network: the payment provider is a :class:`~payments.fake.FakeProvider`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.contrib.messages import get_messages
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.roles import Role
from finance.dates import day_text
from ledger.models import AuditLog, AutopayPlan, Lease, PaymentChannel
from ledger.services import autopay_plans, workspace
from payments.fake import FakeProvider
from payments.models import ExternalRef
from payments.provider import CustomerRecord, PaymentMethodRecord
from tests.job_helpers import run_queued_jobs
from tests.receivables_helpers import a_lease, a_store
from tests.test_lease_services import make_operator
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

ACTIONS = ("plan_pause", "plan_resume", "plan_stop", "plan_end")


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


@pytest.fixture
def worker() -> Iterator[None]:
    with override_settings(STAX_CALLER="worker"):
        yield


def today() -> date:
    return timezone.localdate()


def a_plan_lease(fake: FakeProvider | None = None, operator: Any = None) -> Lease:
    """An active lease whose operator has a provider customer and a linked bank method."""
    tag = uuid.uuid4().hex[:8]
    if operator is None:
        email = f"{tag}@example.com"
        customer = f"cus-{tag}"
        operator = make_operator(email=email, customer=customer)
        ExternalRef.objects.create(
            entity_type="operator",
            entity_id=operator.pk,
            provider="stax_pay",
            external_kind="payment_method",
            external_id=f"pm-{tag}",
        )
        if fake is not None:
            fake.customers[customer] = CustomerRecord(
                external_id=customer, raw_status="", fetched_at=timezone.now(), operator_email=email
            )
            fake.add_payment_method(
                customer,
                PaymentMethodRecord(
                    external_id=f"pm-{tag}", raw_status="", fetched_at=timezone.now(), method="bank"
                ),
            )
    return a_lease(
        a_store(f"Store {tag}"),
        operator,
        channel=PaymentChannel.PROVIDER_ACH,
        started=date(2026, 1, 1),
    )


def a_plan(lease: Lease, **overrides: Any) -> AutopayPlan:
    values: dict[str, Any] = {
        "item_name": "Sign fee",
        "description": "",
        "quantity": Decimal("1"),
        "unit_amount": Decimal("100.00"),
        "cadence": "monthly",
        "first_on": today() + timedelta(days=10),
        "end_rule": "count",
        "end_count": 3,
        "end_on": None,
        "actor": None,
        "confirmed": False,
        "today": today(),
    }
    values.update(overrides)
    outcome = autopay_plans.create(lease, **values)
    assert outcome.ok and outcome.plan is not None, outcome.message
    return outcome.plan


def a_scheduled_plan(lease: Lease, **overrides: Any) -> AutopayPlan:
    """A plan whose first occurrence's schedule the worker made (``fake`` and ``worker``)."""
    plan = a_plan(lease, **overrides)
    run_queued_jobs()
    return plan


def fresh(plan: AutopayPlan) -> AutopayPlan:
    return AutopayPlan.objects.get(pk=plan.pk)


def url(name: str, store_id: object, plan: object) -> str:
    return reverse(f"ledger:stores:{name}", kwargs={"pk": store_id, "plan": plan})


def tab_html(client: Client, store_id: object) -> str:
    path = reverse("ledger:stores:detail", kwargs={"pk": store_id}) + "?tab=schedules"
    response = client.get(path)
    assert response.status_code == 200
    return response.content.decode()


def flashed(response: Any) -> list[str]:
    """The messages of the request, oldest first; read, so the next request starts empty."""
    return [str(message) for message in get_messages(response.wsgi_request)]


def last_flashed(response: Any) -> str:
    return flashed(response)[-1]


def card(html: str) -> str:
    start = html.index('id="repeating-debits"')
    return html[start : html.index("</section>", start)]


# --- The card ----------------------------------------------------------------------------------


def test_the_card_lists_each_plan_with_its_line_and_its_state_badge(
    fake: FakeProvider, worker: None
) -> None:
    lease = a_plan_lease(fake)
    first = today() + timedelta(days=10)
    active = a_scheduled_plan(lease, item_name="Sign fee", first_on=first)
    paused = a_scheduled_plan(lease, item_name="Cleaning", end_rule="never", end_count=None)
    assert autopay_plans.pause(paused, client_for(Role.BILLING)[1]).ok
    stopped = a_scheduled_plan(
        lease, item_name="Parking", end_rule="date", end_count=None, end_on=first + timedelta(400)
    )
    assert autopay_plans.stop(stopped, client_for(Role.ADMIN)[1]).ok
    finished = a_scheduled_plan(lease, item_name="Signage")
    AutopayPlan.objects.filter(pk=finished.pk).update(state="finished")
    html = card(tab_html(client_for(Role.VIEWER)[0], lease.store_id))
    rows = {plan.pk: html[html.index(f'id="plan-{plan.pk}"') :] for plan in (active, paused)}
    assert html.index(f'id="plan-{paused.pk}"') < html.index(f'id="plan-{stopped.pk}"')
    line = f"Monthly from {day_text(first)} · 1 of 3 made · next {day_text(first)}"
    assert line in rows[active.pk]
    assert "badge-paid" in rows[active.pk].split("</li>")[0]
    assert ">✓</span> On</span>" in rows[active.pk].split("</li>")[0]
    paused_row = rows[paused.pk].split("</li>")[0]
    assert "badge-pending" in paused_row and "Paused" in paused_row
    assert "· 0 made" in paused_row and "next" not in paused_row
    stopped_row = html[html.index(f'id="plan-{stopped.pk}"') :].split("</li>")[0]
    assert "badge-neutral" in stopped_row and "Stopped" in stopped_row
    assert f"until {day_text(first + timedelta(400))}" in stopped_row
    finished_row = html[html.index(f'id="plan-{finished.pk}"') :].split("</li>")[0]
    assert "badge-neutral" in finished_row and "Finished" in finished_row
    assert "$100.00" in rows[active.pk]


def test_a_store_without_a_plan_shows_the_empty_line() -> None:
    lease = a_plan_lease()
    html = card(tab_html(client_for(Role.VIEWER)[0], lease.store_id))
    assert 'id="plan-rows-empty"' in html and "plan-row" not in html.replace("plan-rows", "")


def test_each_role_sees_only_its_actions(fake: FakeProvider, worker: None) -> None:
    lease = a_plan_lease(fake)
    active = a_scheduled_plan(lease)
    paused = a_scheduled_plan(lease, item_name="Cleaning")
    assert autopay_plans.pause(paused, client_for(Role.BILLING)[1]).ok
    viewer = card(tab_html(client_for(Role.VIEWER)[0], lease.store_id))
    assert "<form" not in viewer
    billing = card(tab_html(client_for(Role.BILLING)[0], lease.store_id))
    assert f'id="plan-pause-{active.pk}"' in billing
    assert f'id="plan-resume-{paused.pk}"' in billing
    assert f'id="plan-pause-{paused.pk}"' not in billing
    assert "plan-stop-" not in billing and "plan-end-" not in billing
    admin = card(tab_html(client_for(Role.ADMIN)[0], lease.store_id))
    assert f'id="plan-stop-{active.pk}"' in admin and f'id="plan-stop-{paused.pk}"' in admin
    assert f'id="plan-end-{active.pk}"' in admin
    assert admin.count('class="js-single-submit') == admin.count("<form")
    assert url("plan_stop", lease.store_id, active.pk) in admin


def test_a_stopped_plan_offers_no_action() -> None:
    lease = a_plan_lease()
    plan = a_plan(lease)
    AutopayPlan.objects.filter(pk=plan.pk).update(state="stopped")
    admin = card(tab_html(client_for(Role.ADMIN)[0], lease.store_id))
    assert "<form" not in admin


def test_the_operator_page_names_the_store_of_each_plan() -> None:
    lease = a_plan_lease()
    a_plan(lease)
    second = a_plan_lease(operator=lease.operator)
    a_plan(second, item_name="Cleaning")
    path = reverse("ledger:operators:detail", kwargs={"pk": lease.operator_id}) + "?tab=schedules"
    html = card(client_for(Role.VIEWER)[0].get(path).content.decode())
    assert f"{lease.store.external_label} · Monthly" in html
    assert f"{second.store.external_label} · Monthly" in html


# --- The actions ---------------------------------------------------------------------------------


def test_billing_pauses_and_resumes_and_the_sentence_is_shown(
    fake: FakeProvider, worker: None
) -> None:
    lease = a_plan_lease(fake)
    plan = a_scheduled_plan(lease)
    client, _ = client_for(Role.BILLING)
    response = client.post(url("plan_pause", lease.store_id, plan.pk))
    assert response.status_code == 302
    assert response["Location"] == workspace.tab_url(lease.store_id, workspace.TAB_SCHEDULES)
    assert flashed(response) == [autopay_plans.PAUSED]
    assert fresh(plan).state == "paused"
    response = client.post(url("plan_resume", lease.store_id, plan.pk))
    assert response.status_code == 302
    assert fresh(plan).state == "active"
    message = last_flashed(response)
    assert message.startswith("Plan resumed; the next debit is on ")


def test_a_refused_action_shows_the_services_sentence(fake: FakeProvider, worker: None) -> None:
    lease = a_plan_lease(fake)
    plan = a_scheduled_plan(lease)
    client, _ = client_for(Role.BILLING)
    response = client.post(url("plan_resume", lease.store_id, plan.pk))
    assert response.status_code == 302
    assert flashed(response) == [autopay_plans.NOT_PAUSED]
    assert fresh(plan).state == "active"


@pytest.mark.parametrize("name", ACTIONS)
def test_a_viewers_post_is_refused_and_changes_nothing(name: str) -> None:
    lease = a_plan_lease()
    plan = a_plan(lease)
    before = AuditLog.objects.count()
    response = client_for(Role.VIEWER)[0].post(
        url(name, lease.store_id, plan.pk), {"end_rule": "never"}
    )
    assert response.status_code == 403
    assert fresh(plan).state == "active" and fresh(plan).end_rule == "count"
    assert AuditLog.objects.count() == before


@pytest.mark.parametrize("name", ["plan_stop", "plan_end"])
def test_billings_stop_and_change_end_are_refused(name: str) -> None:
    lease = a_plan_lease()
    plan = a_plan(lease)
    before = AuditLog.objects.count()
    response = client_for(Role.BILLING)[0].post(
        url(name, lease.store_id, plan.pk), {"end_rule": "never"}
    )
    assert response.status_code == 403
    assert (fresh(plan).state, fresh(plan).end_rule) == ("active", "count")
    assert AuditLog.objects.count() == before


def test_an_admin_stops_a_plan(fake: FakeProvider, worker: None) -> None:
    lease = a_plan_lease(fake)
    plan = a_scheduled_plan(lease)
    response = client_for(Role.ADMIN)[0].post(url("plan_stop", lease.store_id, plan.pk))
    assert response.status_code == 302
    assert flashed(response) == [autopay_plans.STOPPED]
    assert fresh(plan).state == "stopped"


@pytest.mark.parametrize("name", ACTIONS)
def test_a_plan_of_another_store_is_404_and_changes_nothing(name: str) -> None:
    lease = a_plan_lease()
    other = a_plan_lease()
    plan = a_plan(lease)
    before = AuditLog.objects.count()
    client, _ = client_for(Role.ADMIN)
    assert client.post(url(name, other.store_id, plan.pk), {"end_rule": "never"}).status_code == 404
    assert client.post(url(name, lease.store_id, uuid.uuid4())).status_code == 404
    assert client.post(url(name, lease.store_id, "not-a-uuid")).status_code == 404
    assert client.post(url(name, "not-a-uuid", plan.pk)).status_code == 404
    assert (fresh(plan).state, fresh(plan).end_rule) == ("active", "count")
    assert AuditLog.objects.count() == before


@pytest.mark.parametrize("name", ["plan_pause", "plan_resume", "plan_end"])
def test_the_reversible_actions_and_change_end_take_post_only(name: str) -> None:
    lease = a_plan_lease()
    plan = a_plan(lease)
    assert client_for(Role.ADMIN)[0].get(url(name, lease.store_id, plan.pk)).status_code == 405


# --- Change end -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"end_rule": "count", "end_count": "1"}, autopay_plans.END_COUNT),
        ({"end_rule": "count", "end_count": "1000"}, autopay_plans.END_COUNT),
        ({"end_rule": "count", "end_count": ""}, autopay_plans.END_COUNT),
        ({"end_rule": "date", "end_on": ""}, autopay_plans.END_DATE_REQUIRED),
        (
            {"end_rule": "count", "end_count": "two"},
            "Check the end of the plan. Nothing was changed.",
        ),
        ({"end_rule": "<b>x</b>"}, "Check the end of the plan. Nothing was changed."),
        ({}, "Check the end of the plan. Nothing was changed."),
        (
            {"end_rule": "date", "end_on": "2026-02-30"},
            "Check the end of the plan. Nothing was changed.",
        ),
    ],
)
def test_change_end_refuses_a_bad_rule_and_writes_nothing(
    data: dict[str, str], message: str
) -> None:
    lease = a_plan_lease()
    plan = a_plan(lease)
    before = AuditLog.objects.count()
    response = client_for(Role.ADMIN)[0].post(url("plan_end", lease.store_id, plan.pk), data)
    assert response.status_code == 302
    assert flashed(response) == [message]
    current = fresh(plan)
    assert (current.end_rule, current.end_count, current.state) == ("count", 3, "active")
    assert AuditLog.objects.count() == before


def test_change_end_to_a_date_before_the_first_debit_is_refused() -> None:
    lease = a_plan_lease()
    plan = a_plan(lease)
    day = (plan.first_on - timedelta(days=1)).isoformat()
    response = client_for(Role.ADMIN)[0].post(
        url("plan_end", lease.store_id, plan.pk), {"end_rule": "date", "end_on": day}
    )
    assert flashed(response) == [autopay_plans.END_BEFORE_FIRST]
    assert fresh(plan).end_rule == "count"


def test_change_end_rewrites_the_rule_or_finishes_a_met_plan() -> None:
    lease = a_plan_lease()
    plan = a_plan(lease)
    client, _ = client_for(Role.ADMIN)
    response = client.post(
        url("plan_end", lease.store_id, plan.pk), {"end_rule": "count", "end_count": "6"}
    )
    assert [last_flashed(response)] == [autopay_plans.END_CHANGED]
    assert (fresh(plan).end_count, fresh(plan).state) == (6, "active")
    response = client.post(
        url("plan_end", lease.store_id, plan.pk), {"end_rule": "never", "end_count": "6"}
    )
    assert [last_flashed(response)] == [autopay_plans.END_CHANGED]
    assert (fresh(plan).end_rule, fresh(plan).end_count) == ("never", None)
    response = client.post(
        url("plan_end", lease.store_id, plan.pk), {"end_rule": "never", "end_count": ""}
    )
    assert [last_flashed(response)] == [autopay_plans.UNCHANGED]
    AutopayPlan.objects.filter(pk=plan.pk).update(made_count=2)
    response = client.post(
        url("plan_end", lease.store_id, plan.pk), {"end_rule": "count", "end_count": "2"}
    )
    assert [last_flashed(response)] == [autopay_plans.FINISHED]
    assert fresh(plan).state == "finished"


def test_the_change_end_form_holds_the_plans_rule() -> None:
    lease = a_plan_lease()
    plan = a_plan(lease, end_count=7)
    html = card(tab_html(client_for(Role.ADMIN)[0], lease.store_id))
    form = html[html.index(f'id="plan-end-{plan.pk}"') :].split("</details>")[0]
    assert 'value="count" checked' in form and 'value="date">' in form
    assert 'value="never">' in form
    assert f'id="plan-end-count-{plan.pk}" min="2" max="999" step="1" value="7"' in form


# --- History, the run page, the help page --------------------------------------------------------


def test_the_history_tab_lists_the_plans_audit_rows(fake: FakeProvider, worker: None) -> None:
    lease = a_plan_lease(fake)
    plan = a_scheduled_plan(lease)
    client, _ = client_for(Role.ADMIN)
    client.post(url("plan_pause", lease.store_id, plan.pk))
    path = reverse("ledger:stores:detail", kwargs={"pk": lease.store_id}) + "?tab=history"
    html = client.get(path).content.decode()
    assert autopay_plans.CREATED_ACTION in html and autopay_plans.PAUSED_ACTION in html
    assert f"autopay_plan {str(plan.pk)[:4]}…{str(plan.pk)[-4:]}" in html


def test_the_run_page_lists_the_plan_debits_created_today() -> None:
    lease = a_plan_lease()
    plan = a_plan(lease, item_name="Sign fee")
    html = client_for(Role.VIEWER)[0].get("/run/").content.decode()
    start = html.index('id="autopay-charges-card"')
    section = html[start : html.index("</section>", start)]
    assert "Repeating debits created today" in section
    assert "Sign fee · 1 of 3" in section and "$100.00" in section
    assert lease.store.external_label in section
    assert f"/stores/{plan.store_id}/?tab=schedules" in section


def test_the_run_page_card_is_empty_without_a_plan_debit() -> None:
    html = client_for(Role.VIEWER)[0].get("/run/").content.decode()
    assert "No repeating debit was created today." in html


def test_the_help_page_has_the_repeating_debits_paragraph() -> None:
    path = reverse("finance:help_topic", kwargs={"slug": "autopay-and-daily-charges"})
    html = client_for(Role.VIEWER)[0].get(path).content.decode()
    assert 'id="help-repeating-debits"' in html
    assert "Repeating debits." in html and "Set up autopay" in html


# --- The query budget ----------------------------------------------------------------------------


def test_the_autopay_tab_reads_its_plans_in_one_query_whatever_their_number() -> None:
    lease = a_plan_lease()
    a_plan(lease)

    def build() -> int:
        with CaptureQueriesContext(connection) as queries:
            workspace.store_workspace(lease.store, today(), Role.ADMIN, tab="schedules")
        return len(queries)

    small = build()
    for name in ("Cleaning", "Parking", "Signage", "Storage"):
        a_plan(lease, item_name=name)
    assert build() == small


# --- Stop's confirmation page ---------------------------------------------------------------------


def test_stop_asks_first_on_a_confirmation_page_and_writes_nothing() -> None:
    lease = a_plan_lease()
    plan = a_plan(lease, item_name="Sign fee", end_count=12)
    before = AuditLog.objects.count()
    response = client_for(Role.ADMIN)[0].get(url("plan_stop", lease.store_id, plan.pk))
    assert response.status_code == 200
    html = response.content.decode()
    assert "Are you sure?" in html and 'id="plan-stop-submit"' in html
    assert "Sign fee, $100.00 monthly from" in html
    assert "The pending debit's schedule is removed" in html
    assert "debits already made stay" in html and "This cannot be undone" in html
    assert f'action="{url("plan_stop", lease.store_id, plan.pk)}"' not in html  # posts to itself
    assert 'id="plan-stop-form"' in html and 'class="js-single-submit"' in html
    assert fresh(plan).state == "active"
    assert AuditLog.objects.count() == before


def test_the_card_links_stop_to_its_confirmation_page() -> None:
    lease = a_plan_lease()
    plan = a_plan(lease)
    admin = card(tab_html(client_for(Role.ADMIN)[0], lease.store_id))
    link = f'<a class="btn btn-danger" href="{url("plan_stop", lease.store_id, plan.pk)}"'
    assert link in admin
    assert f'action="{url("plan_stop", lease.store_id, plan.pk)}"' not in admin


@pytest.mark.parametrize("role", [Role.VIEWER, Role.BILLING])
def test_the_stop_page_is_refused_below_admin(role: Role) -> None:
    lease = a_plan_lease()
    plan = a_plan(lease)
    assert client_for(role)[0].get(url("plan_stop", lease.store_id, plan.pk)).status_code == 403


def test_the_stop_page_of_another_stores_plan_is_404() -> None:
    lease = a_plan_lease()
    other = a_plan_lease()
    plan = a_plan(lease)
    client, _ = client_for(Role.ADMIN)
    assert client.get(url("plan_stop", other.store_id, plan.pk)).status_code == 404
    assert client.get(url("plan_stop", lease.store_id, uuid.uuid4())).status_code == 404


def test_the_stop_page_of_a_stopped_plan_offers_no_button() -> None:
    lease = a_plan_lease()
    plan = a_plan(lease)
    AutopayPlan.objects.filter(pk=plan.pk).update(state="stopped")
    html = client_for(Role.ADMIN)[0].get(url("plan_stop", lease.store_id, plan.pk)).content.decode()
    assert autopay_plans.NOT_LIVE in html.replace("&#x27;", "'")
    assert 'id="plan-stop-submit"' not in html


def test_a_refused_stop_goes_back_to_the_confirmation_page() -> None:
    lease = a_plan_lease()
    plan = a_plan(lease)
    AutopayPlan.objects.filter(pk=plan.pk).update(state="finished")
    response = client_for(Role.ADMIN)[0].post(url("plan_stop", lease.store_id, plan.pk))
    assert response.status_code == 302
    assert response["Location"] == url("plan_stop", lease.store_id, plan.pk)
    assert flashed(response) == [autopay_plans.NOT_LIVE]
    assert fresh(plan).state == "finished"

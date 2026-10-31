"""Adversarial cases for PLAN-012f-a Batch E (autopay plans), by the QA owner (plan §6). Two
daily runs on the same day make one occurrence; a hostile cadence or end rule never reaches a
row; a plan's actions refuse a plan of another store or operator and write nothing; a viewer's
or billing's forbidden action is refused; markup in an item name is escaped on the Autopay tab,
the Charges tab, the daily run page and the History tab; the new module and its templates carry
no provider name (Rule 8); every occurrence's Stax rule is a single occurrence; the plan never
makes a rent charge; a plan on an ended lease is refused; the lead never creates an occurrence
early."""

from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from accounts.roles import Role
from ledger.models import Lease, OneOffCharge, PaymentChannel
from ledger.services import autopay_plans
from payments.fake import FakeProvider
from tests.job_helpers import run_queued_jobs
from tests.receivables_helpers import a_lease, a_store
from tests.test_autopay_plans import a_plan_lease, fake, worker  # noqa: F401
from tests.test_lease_services import bill_lease
from tests.test_one_off_charges import an_operator as an_operator_with_bank
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

MARKUP = '<script>alert("x")</script>'
TODAY = date(2026, 10, 24)
BACKEND = Path(__file__).resolve().parents[1]


_CUSTOMERS = iter(range(1, 1000))


def _lease_with_bank(name: str = "Plan Co") -> Lease:
    """An active lease with a provider customer and a linked bank method, as the plan
    requires (the one-off tests' operator helper; each operator its own ids). PLAN-014 C3:
    the account is the operator's first lease's; this further lease shares it."""
    n = next(_CUSTOMERS)
    operator = an_operator_with_bank(
        email=f"{name.lower().replace(' ', '-')}-{n}@example.com",
        customer=f"cus-e-{n}",
        bank=f"pm-e-{n}",
    )
    return bill_lease(
        a_lease(
            a_store(f"Store {name}"),
            operator,
            channel=PaymentChannel.PROVIDER_ACH,
            started=date(2026, 1, 1),
        )
    )


def _plan(lease: Lease, **overrides: Any) -> Any:
    values: dict[str, Any] = dict(
        item_name=f"Sign fee {MARKUP}",
        description="",
        quantity=Decimal("1"),
        unit_amount=Decimal("100.00"),
        cadence="monthly",
        first_on=TODAY + timedelta(days=10),
        end_rule="count",
        end_count=3,
        end_on=None,
        confirmed=False,
        today=TODAY,
    )
    values.update(overrides)
    return autopay_plans.create(lease, actor=None, **values)


def test_two_runs_on_the_same_day_make_one_occurrence_and_the_lead_never_runs_early() -> None:
    lease = _lease_with_bank()
    outcome = _plan(lease)
    assert outcome.ok, outcome.message
    plan = outcome.plan
    first = OneOffCharge.objects.filter(autopay_plan=plan).count()
    assert first == 1  # the first occurrence at setup
    day_before_lead = plan.next_on - timedelta(days=autopay_plans.LEAD_DAYS + 1)
    autopay_plans.run_due(day_before_lead)
    assert OneOffCharge.objects.filter(autopay_plan=plan).count() == 1
    on_lead = plan.next_on - timedelta(days=autopay_plans.LEAD_DAYS)
    autopay_plans.run_due(on_lead)
    autopay_plans.run_due(on_lead)
    assert OneOffCharge.objects.filter(autopay_plan=plan).count() == 2
    periods = sorted(
        int(number)
        for number in OneOffCharge.objects.filter(autopay_plan=plan).values_list(
            "period_no", flat=True
        )
        if number is not None
    )
    assert periods == [1, 2]


@pytest.mark.parametrize("cadence", [MARKUP, "daily", "MONTHLY", "", "monthly; drop", "9" * 40])
def test_a_hostile_cadence_is_refused_and_writes_nothing(cadence: str) -> None:
    lease = _lease_with_bank("Cad Co")
    before = OneOffCharge.objects.count()
    try:
        outcome = _plan(lease, cadence=cadence)
    except (ValueError, TypeError):
        outcome = None
    assert outcome is None or not outcome.ok
    assert OneOffCharge.objects.count() == before


@pytest.mark.parametrize(
    ("end_rule", "end_count", "end_on"),
    [
        (MARKUP, None, None),
        ("count", 1, None),
        ("count", -5, None),
        ("date", None, TODAY),
        ("never", 3, None),
    ],
)
def test_a_hostile_end_rule_is_refused(
    end_rule: str, end_count: int | None, end_on: date | None
) -> None:
    lease = _lease_with_bank("End Co")
    before = OneOffCharge.objects.count()
    try:
        outcome = _plan(lease, end_rule=end_rule, end_count=end_count, end_on=end_on)
    except (ValueError, TypeError):
        outcome = None
    if end_rule == "never" and end_count == 3:
        # A count with the rule "never" is ignored, not refused: the plan is open-ended.
        assert outcome is not None and outcome.ok and outcome.plan.end_count is None
        return
    assert outcome is None or not outcome.ok
    assert OneOffCharge.objects.count() == before


def test_a_rent_item_and_an_ended_lease_are_refused() -> None:
    lease = _lease_with_bank("Rent Co")
    refused = _plan(lease, item_name="Rent")
    assert not refused.ok and "rent" in refused.message.lower()
    lease.ended_on = TODAY - timedelta(days=1)
    lease.save(update_fields=["ended_on"])
    assert not _plan(lease).ok
    assert not OneOffCharge.objects.filter(lease=lease).exists()


def test_every_occurrence_is_a_single_stax_occurrence_and_never_a_rent_charge() -> None:
    from payments.models import Job

    lease = _lease_with_bank("Rule Co")
    outcome = _plan(lease)
    assert outcome.ok, outcome.message
    charge = OneOffCharge.objects.get(autopay_plan=outcome.plan)
    assert charge.collection == "autopay_due"
    assert Job.objects.filter(kind="schedule_create").exists()
    from payments.one_off_invoices import schedule_rule

    rule = schedule_rule(charge.due_date, timezone.now())
    assert "FREQ=DAILY;COUNT=1" in rule and "FREQ=MONTHLY" not in rule
    assert not lease.rent_charges.filter(amount_due=Decimal("100.00")).exists()


def test_markup_in_an_item_name_is_escaped_wherever_the_plan_shows() -> None:
    lease = _lease_with_bank("Markup Co")
    assert _plan(lease).ok
    admin, _ = client_for(Role.ADMIN)
    pages = [
        reverse("ledger:stores:detail", kwargs={"pk": lease.store_id}) + "?tab=schedules",
        reverse("ledger:stores:detail", kwargs={"pk": lease.store_id}) + "?tab=charges",
        reverse("ledger:stores:detail", kwargs={"pk": lease.store_id}) + "?tab=history",
        "/run/",
    ]
    for path in pages:
        body = admin.get(path).content.decode()
        assert MARKUP not in body, path


def test_a_plans_actions_refuse_another_stores_plan_and_a_viewers_post() -> None:
    lease = _lease_with_bank("Owner Co")
    other = _lease_with_bank("Other Co")
    outcome = _plan(lease)
    assert outcome.ok
    plan = outcome.plan
    try:
        pause = reverse("ledger:stores:plan_pause", kwargs={"pk": other.store_id, "plan": plan.pk})
        stop = reverse("ledger:stores:plan_stop", kwargs={"pk": lease.store_id, "plan": plan.pk})
    except NoReverseMatch:
        pytest.skip("the plan actions' URLs are Batch E3's")
    admin, _ = client_for(Role.ADMIN)
    assert admin.post(pause).status_code == 404
    plan.refresh_from_db()
    assert plan.state == "active"
    viewer, _ = client_for(Role.VIEWER)
    assert viewer.post(stop).status_code in (302, 403)
    billing, _ = client_for(Role.BILLING)
    assert billing.post(stop).status_code in (302, 403)
    plan.refresh_from_db()
    assert plan.state == "active"


def test_the_new_module_and_its_templates_carry_no_provider_name() -> None:
    files = [BACKEND / "ledger" / "services" / "autopay_plans.py"]
    files += sorted((BACKEND / "ledger" / "templates" / "ledger").rglob("*plan*.html"))
    for path in files:
        if path.exists():
            assert not re.search(r"stax", path.read_text(encoding="utf-8"), re.I), path.name


def test_stop_asks_first_and_a_second_stop_changes_nothing(
    fake: FakeProvider,  # noqa: F811
    worker: None,  # noqa: F811
) -> None:
    """Stop deletes the pending debit's schedule and is final: a provider write behind a
    confirmation page (docs/CLAUDE.md Rule 2); billing cannot open it; a second stop is
    refused and leaves the plan stopped. The first debit's provider request runs first:
    while it is queued, a stop is refused as busy (E1's rule)."""
    lease = a_plan_lease(fake)
    outcome = _plan(lease)
    assert outcome.ok
    plan = outcome.plan
    run_queued_jobs()
    try:
        stop = reverse("ledger:stores:plan_stop", kwargs={"pk": lease.store_id, "plan": plan.pk})
    except NoReverseMatch:
        pytest.skip("the plan actions' URLs are Batch E3's")
    billing, _ = client_for(Role.BILLING)
    assert billing.get(stop).status_code in (302, 403)
    admin, _ = client_for(Role.ADMIN)
    page = admin.get(stop)
    assert page.status_code == 200
    text = page.content.decode()
    assert "cannot be undone" in text and MARKUP not in text
    assert text.count("<form") >= 1 and 'method="post"' in text
    assert admin.post(stop).status_code == 302
    plan.refresh_from_db()
    assert plan.state == "stopped"
    before = OneOffCharge.objects.filter(autopay_plan=plan).count()
    assert admin.post(stop).status_code == 302
    plan.refresh_from_db()
    assert plan.state == "stopped"
    assert OneOffCharge.objects.filter(autopay_plan=plan).count() == before

"""PLAN-015j, Fable's adversarial set (docs/qa/15/PLAN-015j-move-in-charge-timing.md §6): what
a hostile or broken New lease POST, and a hostile store-page payment entry, must not do.

Today is 2026-11-03; the lease starts 2026-11-15 (prospective); rent $2,000.00."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from django.test import Client

from accounts.roles import Role
from ledger.models import InstallmentPlan, InvoiceDraft, Lease, OneOffCharge, Payment
from tests import test_new_lease_page as new_lease
from tests.test_new_lease_page import create_data, create_path, post_create, vacant_store
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

today = new_lease.today
admin = new_lease.admin
DEP = "setup_security_deposit"
FEE = "setup_sign_fee"


def operator() -> Any:
    return new_lease.an_operator_with("Hostile Person", email="hostile@example.com")


def attempt(client: Client, **fields: str) -> tuple[Any, Lease | None]:
    store = vacant_store()
    response = post_create(client, store, create_data(store, operator(), **fields))
    return response, Lease.objects.filter(store=store).first()


def nothing_saved() -> bool:
    return not (
        Lease.objects.exists()
        or OneOffCharge.objects.exists()
        or InstallmentPlan.objects.exists()
        or InvoiceDraft.objects.exists()
    )


def installments(prefix: str, count: str, first: str = "2026-12-01") -> dict[str, str]:
    return {
        f"{prefix}_price": "600.00",
        f"{prefix}_when": "installments",
        f"{prefix}_count": count,
        f"{prefix}_first_due": first,
        f"{prefix}_cadence": "monthly",
    }


@pytest.mark.parametrize(
    "value",
    ["later", "ON_DATE", "installments ", "with_invoice'; DROP TABLE leases;--", "\x00", "🙂"],
)
def test_a_when_outside_the_three_values_is_refused_on_a_priced_row(
    admin: Client, today: Any, value: str
) -> None:
    response, lease = attempt(admin, **{f"{FEE}_price": "500.00", f"{FEE}_when": value})
    assert response.status_code == 302
    assert lease is None and nothing_saved()


def test_a_hostile_when_on_a_row_without_a_price_charges_nothing(admin: Client, today: Any) -> None:
    _, lease = attempt(
        admin, **{f"{FEE}_when": "<script>", f"{FEE}_count": "9999", f"{FEE}_on": "x"}
    )
    assert lease is not None
    assert not OneOffCharge.objects.filter(lease=lease).exists()
    assert not InstallmentPlan.objects.exists()


def test_fields_of_a_fee_type_that_is_not_offered_are_ignored(admin: Client, today: Any) -> None:
    _, lease = attempt(
        admin,
        setup_no_such_fee_price="900.00",
        setup_no_such_fee_when="installments",
        setup_no_such_fee_count="3",
        setup_no_such_fee_first_due="2026-12-01",
    )
    assert lease is not None
    assert not OneOffCharge.objects.filter(lease=lease).exists()
    assert not InstallmentPlan.objects.exists()


@pytest.mark.parametrize("count", ["9999", "13", "1", "0", "-3", "2.5", "1e1", "", "three"])
def test_a_tampered_count_saves_nothing(admin: Client, today: Any, count: str) -> None:
    response, lease = attempt(admin, **installments(FEE, count))
    assert response.status_code == 302
    assert lease is None and nothing_saved()


BAD_DAYS = ["2026-11-02", "0000-00-00", "2026-13-45", "<img src=x>", "\x00", "99999-01-01", ""]


@pytest.mark.parametrize("day", BAD_DAYS)
def test_a_bad_or_past_due_date_saves_nothing(admin: Client, today: Any, day: str) -> None:
    response, lease = attempt(
        admin, **{f"{FEE}_price": "500.00", f"{FEE}_when": "on_date", f"{FEE}_on": day}
    )
    assert response.status_code == 302
    assert lease is None and nothing_saved()


@pytest.mark.parametrize("day", BAD_DAYS)
def test_a_bad_or_past_first_installment_date_saves_nothing(
    admin: Client, today: Any, day: str
) -> None:
    response, lease = attempt(admin, **installments(DEP, "3", day))
    assert response.status_code == 302
    assert lease is None and nothing_saved()


def test_a_count_in_other_digits_is_read_as_its_number(admin: Client, today: Any) -> None:
    """Arabic-Indic "٣" is the integer 3 to the form: a plan of three, nothing else."""
    _, lease = attempt(admin, **installments(FEE, "٣"))
    assert lease is not None
    assert list(InstallmentPlan.objects.values_list("count", flat=True)) == [3]


@pytest.mark.parametrize(
    ("first", "cadence"),
    [("9999-12-01", "monthly"), ("9999-12-28", "weekly"), ("9999-12-20", "biweekly")],
)
def test_a_plan_that_runs_past_the_calendar_is_refused_not_a_server_error(
    admin: Client, today: Any, first: str, cadence: str
) -> None:
    """Installments that would fall after the year 9999: refused with nothing saved."""
    fields = installments(FEE, "12", first)
    fields[f"{FEE}_cadence"] = cadence
    response, lease = attempt(admin, **fields)
    assert response.status_code == 302
    assert lease is None and nothing_saved()


def test_a_due_date_at_the_end_of_the_calendar_is_answered(admin: Client, today: Any) -> None:
    response, _ = attempt(
        admin, **{f"{FEE}_price": "500.00", f"{FEE}_when": "on_date", f"{FEE}_on": "9999-12-31"}
    )
    assert response.status_code == 302


def test_markup_typed_into_the_timing_fields_comes_back_escaped(admin: Client, today: Any) -> None:
    store = vacant_store()
    hostile = '"><script>alert("when-field")</script>'
    data = create_data(
        store,
        operator(),
        **{
            f"{FEE}_price": "500.00",
            f"{FEE}_when": "installments",
            f"{FEE}_count": hostile,
            f"{FEE}_first_due": hostile,
        },
    )
    response = admin.post(create_path(store), data, follow=True)
    body = response.content.decode()
    assert response.status_code == 200
    assert '<script>alert("when-field")</script>' not in body
    assert "&lt;script&gt;alert(&quot;when-field&quot;)&lt;/script&gt;" in body
    assert nothing_saved()


def test_one_refused_plan_rolls_back_the_other_rows_plan_and_the_lease(
    admin: Client, today: Any
) -> None:
    """The deposit's plan is fine; the sign fee's ($0.05 in 12) is refused by the engine."""
    fields = installments(DEP, "3")
    fields.update(installments(FEE, "12"))
    fields[f"{FEE}_price"] = "0.05"
    response, lease = attempt(admin, **fields)
    assert response.status_code == 302
    assert lease is None and nothing_saved()


def test_a_replayed_form_makes_one_lease_and_one_plan_per_charge(admin: Client, today: Any) -> None:
    store = vacant_store()
    fields = installments(DEP, "3")
    fields.update(installments(FEE, "2"))
    data = create_data(store, operator(), **fields)
    for _ in range(3):
        assert post_create(admin, store, data).status_code == 302
    lease = Lease.objects.get(store=store)
    parents = OneOffCharge.objects.filter(lease=lease, plan__isnull=True)
    assert parents.count() == 2
    assert InstallmentPlan.objects.count() == 2
    assert sorted(InstallmentPlan.objects.values_list("count", flat=True)) == [2, 3]
    assert OneOffCharge.objects.filter(plan__isnull=False).count() == 5
    assert InvoiceDraft.objects.filter(lease=lease).count() == 1


def test_the_same_field_posted_twice_takes_one_value_and_does_not_break(
    admin: Client, today: Any
) -> None:
    store = vacant_store()
    data = create_data(store, operator(), **{f"{FEE}_price": "500.00"})
    data[f"{FEE}_when"] = ["on_date", "with_invoice"]
    data[f"{FEE}_on"] = ["2026-11-10", "2026-12-10"]
    assert post_create(admin, store, data).status_code == 302
    lease = Lease.objects.get(store=store)
    fee = OneOffCharge.objects.get(lease=lease)
    assert (fee.dated, str(fee.due_date)) == (False, "2026-11-15")
    assert InvoiceDraft.objects.get(lease=lease).lines.filter(existing_charge=fee).exists()


@pytest.mark.parametrize("role", [Role.VIEWER])
def test_a_viewer_cannot_open_or_post_the_page(today: Any, role: Any) -> None:
    viewer = client_for(role, f"viewer-{uuid.uuid4().hex[:6]}")[0]
    store = vacant_store()
    assert viewer.get(create_path(store)).status_code == 403
    response = post_create(viewer, store, create_data(store, operator(), **installments(FEE, "3")))
    assert response.status_code == 403
    assert nothing_saved()


def test_the_store_payment_entry_refuses_a_charge_of_another_lease(
    admin: Client, today: Any
) -> None:
    """A prospective lease's entry cannot pay a charge that belongs to another store's lease."""
    _, mine = attempt(
        admin, **{f"{FEE}_price": "500.00", f"{FEE}_when": "on_date", f"{FEE}_on": "2026-11-10"}
    )
    other_store = vacant_store(438, "Other Store")
    post_create(
        admin,
        other_store,
        create_data(
            other_store,
            new_lease.an_operator_with("Other Person", email="other@example.com"),
            **{f"{FEE}_price": "700.00", f"{FEE}_when": "on_date", f"{FEE}_on": "2026-11-10"},
        ),
    )
    assert mine is not None
    theirs = OneOffCharge.objects.get(lease__store=other_store)
    response = admin.post(
        f"/stores/{mine.store_id}/payments/new/",
        {
            "entry_key": str(uuid.uuid4()),
            "method": "wire",
            "amount": "700.00",
            "paid_on": "2026-11-03",
            "reference": "",
            "note": "Hostile allocation",
            f"alloc_one_off_{theirs.pk}": "700.00",
        },
    )
    assert response.status_code in (200, 302)
    theirs.refresh_from_db()
    assert theirs.amount_paid == 0
    assert not Payment.objects.filter(one_off_charge=theirs).exists()

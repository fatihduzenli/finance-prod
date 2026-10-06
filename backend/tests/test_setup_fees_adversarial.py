"""Adversarial cases for PLAN-010b (setup fees, store costs, deposits, P&L), by the QA owner.

Plan §6 "Adversarial": role sweeps over every new write route, hostile values through the
checklist and the importer, the deposits-held arithmetic, future-dated costs, malformed
identifiers, and the P&L never counting what it must not.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from django.core.management import call_command

from accounts.roles import Role
from ledger.models import (
    AuditLog,
    DepositResolution,
    DepositResolutionKind,
    OneOffCharge,
    Payment,
    PaymentKind,
    StoreCost,
)
from ledger.services import payments_ledger, pnl
from ledger.services import store_costs as cost_service
from tests.test_deposits import held_deposit, resolve, sibling
from tests.test_operator_views import client_for
from tests.test_setup_charges import a_charge, a_lease, fee, item, pay
from tests.test_store_costs import row, run_import, set_cost, store, write_csv

pytestmark = pytest.mark.django_db

D = Decimal
CARD = "4111111111111111"
CARD_SPACED = "4111 1111 1111 1111"


# --- role sweep over every new write route ----------------------------------------------------


def _write_routes(charge: OneOffCharge) -> list[str]:
    lease = charge.lease
    assert lease is not None
    return [
        f"/leases/{lease.pk}/setup-charges/new/",
        f"/leases/{lease.pk}/deposits/{charge.pk}/resolve/",
        f"/leases/{lease.pk}/end/",
        f"/one-off-charges/{charge.pk}/payments/new/",
        f"/one-off-charges/{charge.pk}/retry-mark/",
        f"/one-off-charges/{charge.pk}/link-invoice/",
        f"/one-off-charges/{charge.pk}/edit/",
        f"/one-off-charges/{charge.pk}/waive/",
        f"/stores/{lease.store.pk}/cost/",
    ]


@pytest.mark.parametrize("role", [Role.VIEWER, None])
def test_a_viewer_or_a_user_without_a_role_is_refused_on_every_new_write_route(
    role: Role | None,
) -> None:
    charge = a_charge()
    client, _ = client_for(role)
    before = (
        OneOffCharge.objects.count(),
        Payment.objects.count(),
        StoreCost.objects.count(),
        AuditLog.objects.count(),
    )
    for path in _write_routes(charge):
        for response in (client.post(path, {"note": "x", "amount": "1"}), client.get(path)):
            assert response.status_code in (403, 405), (path, response.status_code)
    assert before == (
        OneOffCharge.objects.count(),
        Payment.objects.count(),
        StoreCost.objects.count(),
        AuditLog.objects.count(),
    )


def test_billing_cannot_waive_end_a_lease_or_resolve_a_deposit() -> None:
    charge = a_charge()
    billing, _ = client_for(Role.BILLING)
    lease = charge.lease
    assert lease is not None
    for path in (
        f"/one-off-charges/{charge.pk}/waive/",
        f"/leases/{lease.pk}/end/",
        f"/leases/{lease.pk}/deposits/{charge.pk}/resolve/",
    ):
        assert billing.post(path, {"note": "x"}).status_code == 403, path


def test_the_pnl_routes_accept_only_safe_methods_from_every_role() -> None:
    for role in (Role.ADMIN, Role.BILLING, Role.VIEWER):
        client, _ = client_for(role)
        for path in ("/reports/pnl/", "/reports/pnl/export.csv"):
            assert client.get(path).status_code == 200, (role, path)
            assert client.post(path, {}).status_code in (403, 405), (role, path)


# --- malformed identifiers -------------------------------------------------------------------


@pytest.mark.parametrize("key", ["not-a-uuid", "00000000-0000-0000-0000-000000000000", "%00"])
def test_unknown_or_malformed_setup_charge_ids_are_404_not_500(key: str) -> None:
    admin, _ = client_for(Role.ADMIN)
    for path in (
        f"/one-off-charges/{key}/",
        f"/one-off-charges/{key}/edit/",
        f"/one-off-charges/{key}/waive/",
    ):
        response = admin.get(path) if path.endswith(f"{key}/") else admin.post(path, {"note": "x"})
        assert response.status_code == 404, path


def test_a_deposit_of_another_lease_cannot_be_resolved_through_a_mismatched_url() -> None:
    charge = held_deposit()
    other = a_lease()
    admin, _ = client_for(Role.ADMIN)
    response = admin.post(
        f"/leases/{other.pk}/deposits/{charge.pk}/resolve/",
        {
            "kind": DepositResolutionKind.WITHHELD,
            "amount": "2500.00",
            "resolved_on": "2026-10-01",
            "note": "wrong lease in the url",
        },
    )
    assert response.status_code == 404
    assert not DepositResolution.objects.exists()


# --- hostile values through the checklist and the add form --------------------------------------


def test_the_checklist_ignores_unknown_fee_codes_and_refuses_impossible_prices() -> None:
    from ledger.models import Lease
    from tests.test_setup_charges import lease_form

    sign = fee()
    admin, _ = client_for(Role.ADMIN)
    base = lease_form()  # one store and one operator for every post below
    # An unknown code posts nothing: the lease is created with no charge.
    admin.post("/leases/new/", {**base, "setup_bogus_tick": "on", "setup_bogus_price": "1.00"})
    assert (Lease.objects.count(), OneOffCharge.objects.count()) == (1, 0)
    Lease.objects.all().delete()
    # Impossible prices: nothing is saved, not even the lease.
    prices = ("1000000000000.00", "NaN", "-5", "0.001", "Infinity", "1e300", "0x10", "1,000")
    for price in prices:
        data = {
            **base,
            "creation_key": str(uuid.uuid4()),
            f"setup_{sign.code}_tick": "on",
            f"setup_{sign.code}_price": price,
        }
        response = admin.post("/leases/new/", data)
        assert response.status_code in (200, 302), price
        assert (Lease.objects.count(), OneOffCharge.objects.count()) == (0, 0), price
    # Other spellings of an exact amount (scientific notation, other digit scripts) are
    # numbers like any other and are stored as the exact amount they name.
    for spelling, exact in (("1e3", D("1000.00")), ("\u0967\u0966\u0966", D("100.00"))):
        admin.post(
            "/leases/new/",
            {
                **base,
                "creation_key": str(uuid.uuid4()),
                f"setup_{sign.code}_tick": "on",
                f"setup_{sign.code}_price": spelling,
            },
        )
        assert list(OneOffCharge.objects.values_list("price", flat=True)) == [exact], spelling
        OneOffCharge.objects.all().delete()
        Lease.objects.all().delete()


def test_a_vendor_or_note_with_a_card_number_never_reaches_a_row_or_a_page() -> None:
    charge = a_charge()
    billing, _ = client_for(Role.BILLING)
    billing.post(
        f"/one-off-charges/{charge.pk}/edit/",
        {
            "cost": "500.00",
            "price": "1000.00",
            "vendor": f"Signs {CARD_SPACED}",
            "due_date": "2026-10-01",
        },
    )
    billing.post(
        f"/one-off-charges/{charge.pk}/payments/new/",
        {
            "method": "wire",
            "amount": "1000.00",
            "paid_on": "2026-10-01",
            "note": f"paid from card {CARD}",
            "creation_key": str(uuid.uuid4()),
        },
    )
    page = billing.get(f"/one-off-charges/{charge.pk}/").content.decode()
    assert CARD not in page and CARD_SPACED not in page
    assert CARD not in str(OneOffCharge.objects.values_list("vendor", "note"))
    assert not Payment.objects.filter(one_off_charge=charge).exists()


# --- the importer under hostile files -------------------------------------------------------


def test_a_card_number_anywhere_in_a_row_refuses_that_row_and_writes_nothing_for_it(
    tmp_path: Path,
) -> None:
    first, second = store(1796), store(437)
    path = write_csv(
        tmp_path / "hostile.csv",
        [
            row("1796", "2500.00", notes=f"card {CARD}"),
            row("437", "3000.00", location=CARD_SPACED),  # a reference-only column
        ],
    )
    report = run_import(path)
    assert report.refused == 2 and report.created == 0
    assert not StoreCost.objects.filter(store__in=[first, second]).exists()
    for _, reason in report.refusals:
        assert CARD not in reason and "4111" not in reason


def test_a_dry_run_with_refused_rows_exits_one_and_writes_nothing(tmp_path: Path) -> None:
    store(1796)
    path = write_csv(tmp_path / "dry.csv", [row("1796", "2500.00"), row("9999999", "1.00")])
    out, err = io.StringIO(), io.StringIO()
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command("import_store_costs", str(path), "--dry-run", stdout=out, stderr=err)
    assert not StoreCost.objects.exists()
    assert not AuditLog.objects.filter(entity_type="store_cost").exists()


def test_a_csv_with_a_formula_in_a_text_cell_is_stored_inert_and_exported_safely(
    tmp_path: Path,
) -> None:
    target = store(1796)
    path = write_csv(
        tmp_path / "formula.csv",
        [row("1796", "2500.00", other_monthly_cost="50.00", other_cost_description="=HYPERLINK()")],
    )
    report = run_import(path)
    assert report.created == 1, report.refusals
    assert cost_service.open_cost(target) is not None
    viewer, _ = client_for(Role.VIEWER)
    body = viewer.get("/reports/pnl/export.csv?from=2026-01&to=2026-01").getvalue().decode()
    for line in csv.reader(io.StringIO(body)):
        for cell in line:
            assert not cell.startswith(("=", "+", "@")), cell


def test_a_header_only_file_and_an_empty_file_import_nothing_without_a_traceback(
    tmp_path: Path,
) -> None:
    header_only = write_csv(tmp_path / "header.csv", [])
    assert run_import(header_only).read == 0
    empty = tmp_path / "empty.csv"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(cost_service.FileRefused):
        cost_service.parse_file(empty)
    assert not StoreCost.objects.exists()


# --- store costs: dates -----------------------------------------------------------------------


def test_a_future_dated_cost_row_never_changes_todays_margin_and_never_shows_zero() -> None:
    from tests.test_store_costs import leased

    lease = leased("3500.00")
    target = lease.store
    assert cost_service.margin(target, date(2026, 10, 1)) is None
    outcome = set_cost(target, "2500.00", date(2030, 1, 1))
    assert outcome.ok
    assert cost_service.current_cost(target, date(2026, 10, 1)) is None
    assert cost_service.margin(target, date(2026, 10, 1)) is None
    assert cost_service.margin(target, date(2030, 1, 1)) == D("1000.00")


# --- deposits held: the ledger total and the P&L footer agree --------------------------------


def test_a_transferred_deposit_is_held_once_in_the_ledger_totals() -> None:
    charge = held_deposit()
    target = sibling(charge)
    outcome = resolve(
        charge, DepositResolutionKind.TRANSFERRED, "2500.00", method="", to_lease=target
    )
    assert outcome.ok, outcome.message
    rows = payments_ledger.ledger_payments(payments_ledger.parse_filter({}))
    totals = payments_ledger.totals(rows)
    # One deposit of 2,500 moved from one lease to another: held once, not twice.
    assert totals.deposits_held == D("2500.00")
    report = pnl.rows(pnl.parse_query({"from": "2026-01", "to": "2026-12"}))
    assert sum((r.fees_billed or D(0)) for r in report) == D(0)  # a deposit is never a fee


def test_a_refund_above_what_was_paid_is_refused_and_a_second_refund_too() -> None:
    charge = held_deposit(paid="1000.00", price="2500.00")
    assert not resolve(charge, DepositResolutionKind.REFUNDED, "1000.01").ok
    assert resolve(charge, DepositResolutionKind.REFUNDED, "1000.00").ok
    assert not resolve(charge, DepositResolutionKind.REFUNDED, "0.01").ok
    assert Payment.objects.filter(kind=PaymentKind.REFUND_DEPOSIT).count() == 1


# --- the P&L never counts what it must not ------------------------------------------------------


def test_the_pnl_counts_no_deposit_no_voided_payment_and_no_waived_charge() -> None:
    from ledger.models import PaymentStatus
    from ledger.services import one_off_charges as setup_service

    deposit = held_deposit(ended=False)  # paid deposit of 2,500 on its lease
    lease = deposit.lease
    assert lease is not None
    sign = setup_service.add_charge(lease.pk, item(fee(), "1000.00"), actor=None).charge
    assert sign is not None
    paid = pay(sign, "1000.00")
    assert paid.ok and paid.payment is not None
    Payment.objects.filter(pk=paid.payment.pk).update(status=PaymentStatus.VOIDED)
    waived = setup_service.add_charge(
        lease.pk, item(fee("architecture_fee"), "700.00"), actor=None
    ).charge
    assert waived is not None
    assert setup_service.waive(waived.pk, note="never built", actor=None).ok

    report = pnl.rows(pnl.parse_query({"from": "2026-01", "to": "2026-12"}))
    fees_billed = sum((r.fees_billed or D(0)) for r in report)
    fees_collected = sum((r.fees_collected or D(0)) for r in report)
    assert fees_billed == D("1000.00")  # the sign fee only: no deposit, no waived charge
    assert fees_collected == D("0.00")  # the voided payment counts for nothing


@pytest.mark.parametrize(
    "params",
    [
        {"from": "0001-01", "to": "9999-12"},
        {"from": "2026-13", "to": "2026-00"},
        {"from": "2026-01", "to": "2025-01"},
        {"group": "../../etc", "detail": "1' OR 1=1"},
        {"from": "2026-01" * 500},
    ],
)
def test_hostile_pnl_parameters_never_500(params: dict[str, str]) -> None:
    viewer, _ = client_for(Role.VIEWER)
    assert viewer.get("/reports/pnl/", params).status_code == 200
    assert viewer.get("/reports/pnl/export.csv", params).status_code == 200


# --- the owner's two-row example ships in the image ------------------------------------------


@pytest.mark.parametrize("suffix", ["csv", "xlsx"])
def test_the_in_image_copy_of_the_two_row_example_equals_the_docs_copy(suffix: str) -> None:
    """``docs/`` is not in the container image; the owner runs the import against the copy
    under ``backend/tests/fixtures/store_costs/``, which must stay identical."""
    from tests.conftest import REPO_ROOT

    docs_copy = REPO_ROOT / "docs" / "templates" / "examples" / f"store_costs_two_rows.{suffix}"
    image_copy = REPO_ROOT / "backend" / "tests" / "fixtures" / "store_costs" / f"two_rows.{suffix}"
    assert image_copy.read_bytes() == docs_copy.read_bytes()
    parsed = cost_service.parse_file(image_copy)
    assert [r.cells["store_no"] for r in parsed.rows] == ["1796", "437"]

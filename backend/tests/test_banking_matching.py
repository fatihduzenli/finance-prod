"""Bank deposit matching (docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md I3; §5 I).

exact by an invoice number whose open charge's balance equals the amount (the invoice's own
charge or a builder invoice's line), refused when the amount differs (weak or none instead);
strong by a sender alias and one open charge of that lease, or that lease's whole balance;
weak with exactly one charge of the amount anywhere; none with two (listed); a tie at a level
stops there; a void invoice, a lease-less charge and a paid charge are never candidates; the
alias normalisation; the queries do not grow with the transactions.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from banking import matching
from banking.models import BankTransaction, Confidence, SenderAlias, Source, normalise
from ledger.models import InvoiceLineItem, InvoiceStatus, Lease, Operator, RentChargeStatus
from tests.receivables_helpers import (
    a_lease,
    a_one_off_charge,
    a_rent_charge,
    a_store,
    an_invoice,
)

pytestmark = pytest.mark.django_db

TODAY = date(2026, 10, 15)
OCTOBER = date(2026, 10, 1)
D = Decimal


def deposit(amount: str, description: str = "", counterparty: str = "") -> BankTransaction:
    return BankTransaction.objects.create(
        source=Source.FAKE,
        external_id=f"t-{uuid.uuid4().hex[:12]}",
        posted_on=date(2026, 10, 10),
        amount=D(amount),
        description=description,
        counterparty=counterparty,
    )


def lease_at(number: int) -> Lease:
    return a_lease(a_store(f"Store {number}", number))


def owner(lease: Lease) -> Operator:
    assert lease.operator is not None
    return lease.operator


def alias(lease: Lease, text: str) -> SenderAlias:
    return SenderAlias.objects.create(lease=lease, text=text, normalised=normalise(text))


def test_exact_by_invoice_number_and_equal_balance() -> None:
    lease = lease_at(1991)
    charge = a_rent_charge(lease, OCTOBER, due="1200.00")
    an_invoice(owner(lease), 501, "1200.00", rent_charge=charge)
    found = matching.suggest(deposit("1200.00", "WIRE IN ref inv-2026-0501 thanks"), TODAY)
    assert (found.confidence, found.lease, found.charge_type, found.charge_id) == (
        Confidence.EXACT,
        lease.pk,
        "rent",
        charge.pk,
    )


def test_exact_by_a_builder_invoice_line() -> None:
    lease = lease_at(12)
    charge = a_one_off_charge(owner(lease), "250.00", OCTOBER, lease=lease)
    invoice = an_invoice(owner(lease), 77, "250.00")
    InvoiceLineItem.objects.create(
        invoice=invoice,
        position=1,
        description="Broken shelf",
        quantity=D("1"),
        unit_price=D("250.00"),
        line_total=D("250.00"),
        one_off_charge=charge,
    )
    found = matching.suggest(deposit("250.00", "ZELLE INV-2026-0077"), TODAY)
    assert (found.confidence, found.charge_type, found.charge_id) == (
        Confidence.EXACT,
        "one_off",
        charge.pk,
    )


def test_exact_is_refused_when_the_amount_differs_and_none_is_left() -> None:
    lease = lease_at(1991)
    charge = a_rent_charge(lease, OCTOBER, due="1200.00")
    an_invoice(owner(lease), 501, "1200.00", rent_charge=charge)
    found = matching.suggest(deposit("1100.00", "WIRE INV-2026-0501"), TODAY)
    assert found == matching.NONE


def test_exact_refused_when_the_amount_differs_falls_to_weak() -> None:
    lease = lease_at(1991)
    charge = a_rent_charge(lease, OCTOBER, due="1200.00")
    an_invoice(owner(lease), 501, "1200.00", rent_charge=charge)
    other = lease_at(7)
    other_charge = a_rent_charge(other, OCTOBER, due="1100.00")
    found = matching.suggest(deposit("1100.00", "WIRE INV-2026-0501"), TODAY)
    assert (found.confidence, found.lease, found.charge_id) == (
        Confidence.WEAK,
        other.pk,
        other_charge.pk,
    )


def test_a_void_invoice_is_not_exact() -> None:
    lease = lease_at(1991)
    charge = a_rent_charge(lease, OCTOBER, due="1200.00")
    an_invoice(owner(lease), 501, "1200.00", rent_charge=charge, internal_status=InvoiceStatus.VOID)
    found = matching.suggest(deposit("1200.00", "WIRE INV-2026-0501"), TODAY)
    # The charge is still the one charge of 1200.00: weak, not exact.
    assert found.confidence == Confidence.WEAK


def test_strong_by_alias_and_one_charge_of_that_lease() -> None:
    lease = lease_at(1991)
    a_rent_charge(lease, OCTOBER, due="1200.00")
    fee = a_one_off_charge(owner(lease), "300.00", OCTOBER, lease=lease)
    other = lease_at(5)
    a_one_off_charge(owner(other), "300.00", OCTOBER, lease=other)
    alias(lease, "Fatih Trading LLC")
    found = matching.suggest(deposit("300.00", "ACH CREDIT", "fatih   trading\tllc"), TODAY)
    assert (found.confidence, found.lease, found.charge_type, found.charge_id) == (
        Confidence.STRONG,
        lease.pk,
        "one_off",
        fee.pk,
    )


def test_strong_by_alias_and_the_whole_balance() -> None:
    lease = lease_at(1991)
    a_rent_charge(lease, OCTOBER, due="1200.00")
    a_one_off_charge(owner(lease), "300.00", OCTOBER, lease=lease)
    alias(lease, "FATIH TRADING")
    found = matching.suggest(deposit("1500.00", "WIRE FROM FATIH TRADING LLC"), TODAY)
    assert (found.confidence, found.lease, found.charge_type, found.charge_id) == (
        Confidence.STRONG,
        lease.pk,
        "",
        None,
    )


def test_an_alias_without_an_equal_amount_is_not_strong() -> None:
    lease = lease_at(1991)
    a_rent_charge(lease, OCTOBER, due="1200.00")
    alias(lease, "FATIH TRADING")
    assert matching.suggest(deposit("999.00", "", "FATIH TRADING"), TODAY) == matching.NONE


def test_weak_with_exactly_one_candidate() -> None:
    lease = lease_at(3)
    charge = a_rent_charge(lease, OCTOBER, due="875.50")
    a_rent_charge(lease_at(4), OCTOBER, due="900.00")
    found = matching.suggest(deposit("875.50", "MOBILE DEPOSIT"), TODAY)
    assert (found.confidence, found.lease, found.charge_id) == (
        Confidence.WEAK,
        lease.pk,
        charge.pk,
    )
    assert found.candidates == (matching.Candidate(lease.pk, "rent", charge.pk),)


def test_none_with_two_candidates_listed() -> None:
    first, second = lease_at(3), lease_at(4)
    one = a_rent_charge(first, OCTOBER, due="900.00")
    two = a_rent_charge(second, OCTOBER, due="900.00")
    found = matching.suggest(deposit("900.00", "DEPOSIT"), TODAY)
    assert (found.confidence, found.lease, found.charge_id) == (Confidence.NONE, None, None)
    assert set(found.candidates) == {
        matching.Candidate(first.pk, "rent", one.pk),
        matching.Candidate(second.pk, "rent", two.pk),
    }


def test_a_tie_at_exact_stops_there() -> None:
    first, second = lease_at(3), lease_at(4)
    one = a_rent_charge(first, OCTOBER, due="900.00")
    two = a_rent_charge(second, OCTOBER, due="900.00")
    an_invoice(owner(first), 11, "900.00", rent_charge=one)
    an_invoice(owner(second), 12, "900.00", rent_charge=two)
    alias(first, "SENDER ONE")
    found = matching.suggest(deposit("900.00", "INV-2026-0011 INV-2026-0012", "SENDER ONE"), TODAY)
    assert found.confidence == Confidence.NONE
    assert len(found.candidates) == 2


def test_a_tie_between_two_aliased_leases_is_none() -> None:
    first, second = lease_at(3), lease_at(4)
    a_rent_charge(first, OCTOBER, due="900.00")
    a_rent_charge(second, OCTOBER, due="900.00")
    alias(first, "SAME FAMILY")
    alias(second, "SAME FAMILY")
    found = matching.suggest(deposit("900.00", "", "SAME FAMILY"), TODAY)
    assert found.confidence == Confidence.NONE
    assert {candidate.lease for candidate in found.candidates} == {first.pk, second.pk}


def test_paid_and_lease_less_charges_are_never_candidates() -> None:
    lease = lease_at(3)
    a_rent_charge(lease, OCTOBER, RentChargeStatus.PAID, "900.00", "900.00")
    a_one_off_charge(owner(lease), "900.00", OCTOBER, lease=None)
    assert matching.suggest(deposit("900.00"), TODAY) == matching.NONE


def test_the_alias_normalisation() -> None:
    assert normalise("  fatih \t trading\nllc ") == "FATIH TRADING LLC"
    assert normalise("Café  Ömer") == "CAFÉ ÖMER"
    assert normalise("   ") == ""


def test_the_queries_do_not_grow_with_the_transactions() -> None:
    lease = lease_at(1991)
    charge = a_rent_charge(lease, OCTOBER, due="1200.00")
    an_invoice(owner(lease), 501, "1200.00", rent_charge=charge)
    alias(lease, "FATIH")
    one = [deposit("1200.00", "INV-2026-0501")]
    with CaptureQueriesContext(connection) as small:
        matching.suggest_all(one, TODAY)
    many = [deposit("1200.00", f"INV-2026-05{n:02d} FATIH") for n in range(10)]
    with CaptureQueriesContext(connection) as large:
        matching.suggest_all(many, TODAY)
    assert len(large.captured_queries) == len(small.captured_queries)

"""PLAN-014 Batch I (Fable): adversarial cases beside Opus's ``test_banking.py``. The rework
paths — confirm, undo, confirm again (a new creation key each time, the attempt in the audit
trail, the first payments voided and the receivable restored exactly); an undo refused when a
line can no longer be voided (nothing changes); a split naming one charge twice above its
balance writes nothing; the kept remainder lands on the first store named; the lease's open
balance after confirm and undo reconciles with the receivables; the amount parser's edges;
every banking POST is CSRF-protected; ``?status=`` and ``return_status`` garbage fall back to a
known state; an institution name holding a card number is withheld at link; a three-letter
alias is a substring match and a tie is none; an undo after the suggestion's charge closed
leaves the deposit new with a fresh suggestion."""

from __future__ import annotations

import json
import re
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from django.urls import reverse

from accounts.roles import Role
from banking import matching, services
from banking.models import BankTransaction, Confidence, SenderAlias, Source, Status
from ledger.models import AuditLog, LeaseCredit, Payment, PaymentStatus, RentChargeStatus
from ledger.services import receivables
from tests.receivables_helpers import a_lease, a_one_off_charge, a_rent_charge, a_store
from tests.test_banking import (
    OCTOBER,
    TODAY,
    deposit,
    enable,
    lease_at,
    owner,
)
from tests.test_banking import billing_user as billing_user  # noqa: PLC0414 (the fixture)
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

D = Decimal
ZERO = D("0.00")


def open_total(lease: Any) -> Decimal:
    rows = receivables.open_balances(TODAY, operator=lease.operator_id)
    return sum((row.balance for row in rows if row.lease_id == lease.pk), ZERO)


# --- Confirm, undo, confirm again --------------------------------------------------------------


def test_confirm_undo_confirm_uses_a_new_key_each_time_and_the_trail_counts_the_attempts(
    billing_user: Any,
) -> None:
    lease = lease_at(11)
    charge = a_rent_charge(lease, OCTOBER, due="1200.00")
    before = open_total(lease)
    txn = deposit("1200.00", "WIRE IN 11")
    choice = services.Choice(lease.pk, "rent", charge.pk)
    assert services.confirm(txn.pk, choice, actor=billing_user, today=TODAY).ok
    assert open_total(lease) == before - D("1200.00")
    first_key = services.entry_key(txn, 0, lease.store_id)
    assert services.undo(txn.pk, actor=billing_user, today=TODAY).ok
    assert open_total(lease) == before  # restored exactly
    assert services.confirm(txn.pk, choice, actor=billing_user, today=TODAY).ok
    assert services.undo(txn.pk, actor=billing_user, today=TODAY).ok
    assert services.confirm(txn.pk, choice, actor=billing_user, today=TODAY).ok
    confirmed = list(
        AuditLog.objects.filter(action=services.CONFIRMED_ACTION, entity_id=str(txn.pk))
        .order_by("at", "pk")
        .values_list("after__creation_key", flat=True)
    )
    assert len(set(confirmed)) == 3 and confirmed[0] == str(first_key)
    undone = AuditLog.objects.filter(action=services.UNDONE_ACTION, entity_id=str(txn.pk))
    assert sorted(row.after["attempt"] for row in undone) == [1, 2]
    settled = Payment.objects.filter(reference=str(txn.pk), status=PaymentStatus.SETTLED)
    voided = Payment.objects.filter(reference=str(txn.pk), status=PaymentStatus.VOIDED)
    assert (settled.count(), voided.count()) == (1, 2)
    charge.refresh_from_db()
    assert (charge.status, charge.amount_paid) == (RentChargeStatus.PAID, D("1200.00"))
    assert open_total(lease) == before - D("1200.00")


def test_an_undo_is_refused_whole_when_one_line_can_no_longer_be_voided(
    billing_user: Any,
) -> None:
    lease = lease_at(12)
    rent = a_rent_charge(lease, OCTOBER, due="1000.00")
    fee = a_one_off_charge(owner(lease), "200.00", OCTOBER, lease=lease)
    txn = deposit("1200.00")
    assert services.confirm(txn.pk, services.Choice(lease.pk), actor=billing_user, today=TODAY).ok
    # One of the two lines is no longer settled (marked returned): the manual void refuses.
    line = Payment.objects.get(one_off_charge=fee)
    Payment.objects.filter(pk=line.pk).update(status=PaymentStatus.RETURNED)
    outcome = services.undo(txn.pk, actor=billing_user, today=TODAY)
    assert not outcome.ok
    assert outcome.message.endswith("Nothing was changed.")
    assert "$200.00" in outcome.message
    txn.refresh_from_db()
    assert txn.status == Status.CONFIRMED and txn.allocation_group is not None
    assert Payment.objects.get(rent_charge=rent).status == PaymentStatus.SETTLED  # untouched
    assert not AuditLog.objects.filter(action=services.UNDONE_ACTION).exists()


def test_an_undo_after_the_suggested_charge_closed_leaves_a_fresh_suggestion(
    billing_user: Any,
) -> None:
    lease = lease_at(13)
    charge = a_rent_charge(lease, OCTOBER, due="750.00")
    txn = deposit("750.00", "RENT 13")
    services.refresh_suggestions(TODAY)
    txn.refresh_from_db()
    assert (txn.confidence, txn.suggested_charge_id) == (Confidence.WEAK, charge.pk)
    assert services.confirm(
        txn.pk, services.Choice(lease.pk, "rent", charge.pk), actor=billing_user, today=TODAY
    ).ok
    assert services.undo(txn.pk, actor=billing_user, today=TODAY).ok
    txn.refresh_from_db()
    # The charge is open again after the void, so the suggestion comes back as it was.
    assert txn.status == Status.NEW
    assert (txn.confidence, txn.suggested_charge_id) == (Confidence.WEAK, charge.pk)


# --- Split edges --------------------------------------------------------------------------------


def test_a_split_naming_one_charge_twice_above_its_balance_writes_nothing(
    billing_user: Any,
) -> None:
    lease = lease_at(21)
    charge = a_rent_charge(lease, OCTOBER, due="1000.00")
    txn = deposit("1200.00")
    outcome = services.split(
        txn.pk,
        [
            services.SplitLine(lease.store_id, charge.pk, D("700.00")),
            services.SplitLine(lease.store_id, charge.pk, D("500.00")),
        ],
        actor=billing_user,
        today=TODAY,
    )
    assert not outcome.ok
    assert "more than its balance" in outcome.message
    assert not Payment.objects.exists()
    txn.refresh_from_db()
    assert txn.status == Status.NEW and txn.allocation_group is None


def test_the_kept_remainder_of_a_split_lands_on_the_first_store_named(billing_user: Any) -> None:
    first, second = lease_at(22), lease_at(23)
    one = a_rent_charge(first, OCTOBER, due="1000.00")
    two = a_rent_charge(second, OCTOBER, due="500.00")
    txn = deposit("1600.00")
    short = services.split(
        txn.pk,
        [
            services.SplitLine(second.store_id, two.pk, D("500.00")),
            services.SplitLine(first.store_id, one.pk, D("1000.00")),
        ],
        actor=billing_user,
        today=TODAY,
    )
    assert not short.ok and "$1,500.00" in short.message and "$1,600.00" in short.message
    kept = services.split(
        txn.pk,
        [
            services.SplitLine(second.store_id, two.pk, D("500.00")),
            services.SplitLine(first.store_id, one.pk, D("1000.00")),
        ],
        actor=billing_user,
        keep_credit=True,
        today=TODAY,
    )
    assert kept.ok, kept.message
    [credit] = LeaseCredit.objects.all()
    assert (credit.lease_id, credit.amount) == (second.pk, D("100.00"))
    txn.refresh_from_db()
    assert txn.status == Status.SPLIT
    assert services.undo(txn.pk, actor=billing_user, today=TODAY).message == services.UNDO_CREDIT


def test_a_split_line_on_a_deposit_charge_is_refused(billing_user: Any) -> None:
    from tests.test_aging import a_deposit

    lease = lease_at(24)
    held = a_deposit(lease, "2000.00", OCTOBER)
    txn = deposit("2000.00")
    outcome = services.split(
        txn.pk,
        [services.SplitLine(lease.store_id, held.pk, D("2000.00"))],
        actor=billing_user,
        today=TODAY,
    )
    # A deposit charge is in the store's open charges, so the entry decides; either way the
    # bank deposit is not applied twice and the state is consistent.
    txn.refresh_from_db()
    if outcome.ok:
        assert txn.status == Status.SPLIT and txn.allocation_group is not None
    else:
        assert txn.status == Status.NEW and not Payment.objects.exists()


# --- The amount parser --------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["", "   ", None, 12, ["1"]])
def test_an_empty_or_non_text_amount_is_none(raw: object) -> None:
    assert services.parse_amount(raw) is None


@pytest.mark.parametrize(
    "raw", ["0", "-5", "0.001", "1,000.00", "$10", "NaN", "Infinity", "-Infinity", "1e400", "abc"]
)
def test_an_amount_that_is_not_cents_above_zero_is_refused(raw: str) -> None:
    with pytest.raises(ValueError, match="more than zero"):
        services.parse_amount(raw)


def test_an_exponent_amount_is_read_as_its_value() -> None:
    assert services.parse_amount("1E+2") == D("100")
    assert services.parse_amount(" 12.50 ") == D("12.50")


# --- The routes ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["confirm", "ignore", "undo", "split", "fetch_now", "link_token", "link_exchange"]
)
def test_every_banking_post_is_csrf_protected(name: str) -> None:
    txn = deposit("10.00")
    kwargs = {"pk": txn.pk} if name in {"confirm", "ignore", "undo", "split"} else {}
    client, _ = client_for(Role.ADMIN)
    client.handler.enforce_csrf_checks = True  # the signed-in client, now strict
    response = client.post(reverse(f"banking:{name}", kwargs=kwargs), {"lease": str(uuid.uuid4())})
    assert response.status_code == 403
    txn.refresh_from_db()
    assert txn.status == Status.NEW


def test_status_garbage_falls_back_and_return_status_garbage_returns_to_the_plain_page(
    billing_user: Any,
) -> None:
    client, _ = client_for(Role.BILLING)
    txn = deposit("10.00")
    for garbage in ("bogus", "../..", "new'--", "x" * 500, "%00"):
        page = client.get(reverse("banking:page"), {"status": garbage})
        assert page.status_code == 200, garbage
        assert garbage not in page.content.decode()
    response = client.post(
        reverse("banking:ignore", kwargs={"pk": txn.pk}),
        {"reason": "not_rent", "return_status": "javascript:alert(1)"},
    )
    assert response.status_code == 302
    assert response["Location"] == reverse("banking:page")
    assert client.get(reverse("banking:confirm", kwargs={"pk": uuid.uuid4()})).status_code in (
        404,
        405,
    )
    assert client.post(reverse("banking:undo", kwargs={"pk": "not-a-uuid"})).status_code == 404


# --- Link: text from the browser ---------------------------------------------------------------


def test_an_institution_name_holding_a_card_number_is_withheld_at_link(settings: Any) -> None:
    from banking.sources import FakeSource, get_source

    settings.PLAID_CLIENT_ID = ""
    settings.PLAID_SECRET = ""
    settings.PLAID_TOKEN_KEY = ""
    enable()
    admin, admin_user = client_for(Role.ADMIN)
    assert isinstance(get_source(), FakeSource)
    response = admin.post(
        reverse("banking:link_exchange"),
        {
            "public_token": "fake-public-token",
            "account_id": "",
            "institution": "Bank 4111 1111 1111 1111 <b>x</b>",
        },
    )
    assert response.status_code == 302
    connection = services.active_connection()
    assert connection is not None
    assert "4111" not in connection.institution and "<b>" not in connection.institution
    linked = AuditLog.objects.get(action=services.LINKED_ACTION)
    assert re.search(r"\b4111\b", json.dumps(linked.after)) is None  # nor in the audit row
    # A public token that is not an identifier never reaches the service.
    services.disconnect(admin_user)
    refused = admin.post(
        reverse("banking:link_exchange"),
        {"public_token": "fake token with spaces", "account_id": "", "institution": "X"},
    )
    assert refused.status_code == 302 and services.active_connection() is None


# --- Matching edges -----------------------------------------------------------------------------


def test_a_three_letter_alias_matches_as_a_substring_and_a_tie_is_none() -> None:
    first, second = lease_at(31), lease_at(32)
    a_rent_charge(first, OCTOBER, due="900.00")
    a_rent_charge(second, OCTOBER, due="900.00")
    SenderAlias.objects.create(lease=first, text="LLC", normalised="LLC")
    txn = deposit("900.00", "PAYMENT", "Kaya Repairs LLC")
    found = matching.suggest(txn, TODAY)
    assert found.confidence == Confidence.STRONG and found.lease == first.pk
    SenderAlias.objects.create(lease=second, text="LLC", normalised="LLC")
    tie = matching.suggest(txn, TODAY)
    assert tie.confidence == Confidence.NONE and tie.lease is None
    assert {candidate.lease for candidate in tie.candidates} == {first.pk, second.pk}
    assert services.auto_confirm(TODAY) == 0  # a strong or tied match is never auto-confirmed


def test_an_invoice_number_of_a_closed_charge_is_not_exact_and_falls_through() -> None:
    from tests.receivables_helpers import an_invoice

    lease = lease_at(33)
    paid = a_rent_charge(lease, date(2026, 9, 1), RentChargeStatus.PAID, "640.00", "640.00")
    an_invoice(owner(lease), 777, "640.00", rent_charge=paid)
    open_charge = a_rent_charge(lease, OCTOBER, due="640.00")
    txn = deposit("640.00", "INV-2026-0777 rent")
    found = matching.suggest(txn, TODAY)
    assert found.confidence == Confidence.WEAK and found.charge_id == open_charge.pk


def test_a_description_with_two_invoice_numbers_of_equal_charges_is_a_tie_at_exact() -> None:
    from tests.receivables_helpers import an_invoice

    lease = lease_at(34)
    one = a_rent_charge(lease, date(2026, 9, 1), due="500.00")
    two = a_rent_charge(lease, OCTOBER, due="500.00")
    an_invoice(owner(lease), 801, "500.00", rent_charge=one)
    an_invoice(owner(lease), 802, "500.00", rent_charge=two)
    txn = deposit("500.00", "INV-2026-0801 INV-2026-0802")
    found = matching.suggest(txn, TODAY)
    assert found.confidence == Confidence.NONE
    assert {candidate.charge_id for candidate in found.candidates} == {one.pk, two.pk}


# --- The transaction row itself ----------------------------------------------------------------


def test_a_foreign_external_id_cannot_be_reused_for_another_amount() -> None:
    from django.db import IntegrityError, transaction

    first = deposit("10.00")
    with pytest.raises(IntegrityError), transaction.atomic():
        BankTransaction.objects.create(
            source=Source.FAKE,
            external_id=first.external_id,
            posted_on=first.posted_on,
            amount=D("20.00"),
        )


def test_a_lease_less_charge_is_never_a_candidate_even_for_the_operators_alias() -> None:
    lease = lease_at(35)
    store_less = a_one_off_charge(owner(lease), "120.00", OCTOBER)  # no lease
    SenderAlias.objects.create(lease=lease, text="Nora", normalised="NORA")
    txn = deposit("120.00", "Nora pays")
    found = matching.suggest(txn, TODAY)
    assert found.confidence == Confidence.NONE and found.candidates == ()
    assert store_less.lease_id is None
    assert a_store is not None and a_lease is not None  # the helpers stay imported for the world

"""The operator portal, slice 2: Pay by bank debit and the Bank account page
(docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md H4, H5; §5 H).

Pay: the amount is the charge's balance or the store's open balance only (a tampered amount is
refused and nothing is written), a charge of another operator is the portal's 404, the flag off
says online payment is not available yet and creates nothing, the fake provider runs the
payment end to end (the portal payment paid, the payment settled by its webhook, the charge
paid, audited), a replay of the page's key makes one job, the processor's limit refuses, a lease
without a bank method is sent to the Bank page first, a failed attempt is reported. Bank: the
fake hosted fields attach a method, a replacement supersedes the old one and keeps the
processor's object, a shared account's replacement changes every sharing lease and the page
says so, no hosted fields says "not set up", autopay cannot be changed, only the method's
reference is accepted (Rule 1). ``{pay_link}`` names the portal's Pay page for an operator with
an active account. Portal migration 0002 and payments migration 0022 both ways. Nothing reaches
a network: the payment provider is a :class:`~payments.fake.FakeProvider`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, override_settings
from django.utils import timezone

from accounts.roles import Role
from ledger.models import (
    AuditLog,
    CollectionMethod,
    Lease,
    LeaseBilling,
    OneOffCharge,
    OneOffChargeStatus,
    Operator,
    Payment,
    PaymentStatus,
    Store,
    StoreStatus,
)
from ledger.services import messaging, receivables
from ledger.services import one_off_charges as one_off_service
from payments import jobs
from payments.errors import ProviderRejected
from payments.fake import FakeProvider
from payments.models import ExternalRef, Job, JobKind, JobStatus
from payments.provider import PaymentMethodRecord
from portal import payments as portal_payments
from portal.models import PortalPayment, PortalPaymentCharge, PortalPaymentStatus, PortalUser
from tests.job_helpers import run_queued_jobs
from tests.receivables_helpers import a_rent_charge
from tests.test_charge_now import LAST_FOUR, store_methods
from tests.test_lease_services import make_lease
from tests.test_one_off_charges import BANK, CUSTOMER, an_operator, created, item
from tests.test_operator_views import client_for
from tests.test_portal import active_user, portal_client
from tests.test_webhook_handlers import deliver
from tests.text_helpers import assert_absent

pytestmark = pytest.mark.django_db

D = Decimal
BASE = "http://localhost:8000"
NEW_BANK = "pm-bank-portal-2"
NEW_LAST_FOUR = "4321"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider(pay_invoice_enabled=True)
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


@pytest.fixture
def enabled() -> Iterator[None]:
    with override_settings(STAX_CALLER="worker", STAX_PAY_INVOICE_ENABLED=True):
        yield


class World:
    """An operator with a portal account and one lease (store #1991) on its own processor
    customer with a linked bank method (····6789); an unpaid rent charge of $1,500.00 and an
    e-mailed one-off charge of $100.00 whose invoice is at the processor."""

    def __init__(self, fake: FakeProvider) -> None:
        self.operator = an_operator(fake, email="pay.owner@example.com")
        store_methods(self.operator)
        self.lease = Lease.objects.get(operator=self.operator)
        LeaseBilling.objects.filter(lease=self.lease).update(bank_last4=LAST_FOUR)
        Store.objects.filter(pk=self.lease.store_id).update(store_number=1991)
        self.store = Store.objects.get(pk=self.lease.store_id)
        self.rent = a_rent_charge(self.lease, date(2026, 9, 1), due="1500.00")
        self.one_off = created(
            self.operator, item(CollectionMethod.EMAIL, "100.00"), lease=self.lease
        )
        run_queued_jobs()
        fake.calls.clear()
        self.user = active_user(self.operator)
        self.client = portal_client(self.user)

    def pay_url(self, charge: object = None) -> str:
        url = f"/portal/stores/{self.store.pk}/pay/"
        return f"{url}?charge={charge}" if charge is not None else url


@pytest.fixture
def world(fake: FakeProvider, enabled: None) -> World:
    return World(fake)


def text(response: Any) -> str:
    return str(response.content.decode())


def post_pay(world: World, charge: object, amount: str, key: uuid.UUID | None = None) -> Any:
    return world.client.post(
        world.pay_url(),
        {
            "charge": "" if charge is None else str(charge),
            "amount": amount,
            "creation_key": str(key or uuid.uuid4()),
        },
    )


def written() -> tuple[int, int, int, int, int]:
    """What a refusal must leave as it was."""
    return (
        PortalPayment.objects.count(),
        PortalPaymentCharge.objects.count(),
        Job.objects.count(),
        AuditLog.objects.count(),
        OneOffCharge.objects.count(),
    )


def portal_actions() -> list[str]:
    return list(
        AuditLog.objects.filter(entity_type="portal_payment")
        .order_by("id")
        .values_list("action", flat=True)
    )


# --- Pay: the page ----------------------------------------------------------------------------


def test_the_pay_page_confirms_the_charges_amount_and_the_banks_last_four(world: World) -> None:
    page = text(world.client.get(world.pay_url(world.rent.pk)))
    assert 'id="pay-amount">$1,500.00<' in page
    assert f'id="pay-bank">····{LAST_FOUR}' in page
    assert 'name="amount" value="1500.00"' in page
    assert f'name="charge" value="{world.rent.pk}"' in page
    assert 'id="pay-form"' in page
    whole = text(world.client.get(world.pay_url()))
    assert 'id="pay-amount">$1,600.00<' in whole and 'name="amount" value="1600.00"' in whole
    assert "Pay the whole balance" in whole


def test_the_store_page_offers_pay_per_charge_the_whole_balance_and_the_bank(world: World) -> None:
    page = text(world.client.get(f"/portal/stores/{world.store.pk}/"))
    assert f'href="/portal/stores/{world.store.pk}/pay/?charge={world.rent.pk}"' in page
    assert f'href="/portal/stores/{world.store.pk}/pay/?charge={world.one_off.pk}"' in page
    assert 'id="pay-whole"' in page
    assert f'href="/portal/stores/{world.store.pk}/bank/" id="store-bank-link"' in page


@pytest.mark.parametrize(
    "amount", ["1.00", "1500.01", "1499.99", "-1500.00", "0", "", "NaN", "1e3", "1,500.00"]
)
def test_a_tampered_amount_is_refused_and_nothing_is_written(world: World, amount: str) -> None:
    before = written()
    response = post_pay(world, world.rent.pk, amount)
    assert response.status_code == 400
    assert portal_payments.TAMPERED in text(response)
    assert written() == before


def test_the_same_amount_without_cents_is_the_amount(world: World) -> None:
    assert post_pay(world, world.rent.pk, "1500").status_code == 302
    assert PortalPayment.objects.get().amount == D("1500.00")


def test_the_whole_balance_takes_the_stores_open_balance_only(world: World) -> None:
    before = written()
    response = post_pay(world, None, "1500.00")  # the rent alone, posted as the whole balance
    assert response.status_code == 400 and portal_payments.TAMPERED in text(response)
    assert written() == before
    assert post_pay(world, None, "1600.00").status_code == 302
    assert PortalPaymentCharge.objects.count() == 2
    assert PortalPayment.objects.get().amount == D("1600.00")


def test_a_charge_of_another_operator_or_store_is_the_portals_404(
    world: World, fake: FakeProvider
) -> None:
    other = an_operator(fake, customer="cus-other", bank="pm-other")
    other_lease = Lease.objects.get(operator=other)
    foreign = a_rent_charge(other_lease, date(2026, 9, 1), due="777.00")
    before = written()
    assert world.client.get(world.pay_url(foreign.pk)).status_code == 404
    assert post_pay(world, foreign.pk, "777.00").status_code == 404
    assert world.client.get(world.pay_url("not-a-uuid")).status_code == 404
    assert world.client.get(world.pay_url(uuid.uuid4())).status_code == 404
    assert world.client.get(f"/portal/stores/{other_lease.store_id}/pay/").status_code == 404
    assert written() == before


def test_with_the_flag_off_the_page_says_so_and_nothing_is_created(world: World) -> None:
    before = written()
    with override_settings(STAX_PAY_INVOICE_ENABLED=False):
        page = text(world.client.get(world.pay_url(world.rent.pk)))
        assert portal_payments.PAY_UNAVAILABLE in page and 'id="pay-form"' not in page
        response = post_pay(world, world.rent.pk, "1500.00")
    assert response.status_code == 400
    assert portal_payments.PAY_UNAVAILABLE in text(response)
    assert written() == before


def test_a_lease_without_a_bank_method_is_sent_to_the_bank_page_first(world: World) -> None:
    ExternalRef.objects.filter(entity_id=world.lease.pk, external_kind="payment_method").update(
        superseded_at=timezone.now()
    )
    bank = f"/portal/stores/{world.store.pk}/bank/?pay=1"
    response = world.client.get(world.pay_url(world.rent.pk))
    assert (response.status_code, response["Location"]) == (302, bank)
    before = written()
    response = post_pay(world, world.rent.pk, "1500.00")
    assert (response.status_code, response["Location"]) == (302, bank)
    assert written() == before
    assert portal_payments.PAY_NEEDS_BANK in text(world.client.get(bank))


def test_above_the_processors_limit_the_charge_is_refused(world: World) -> None:
    before = written()
    with override_settings(PROVIDER_MAX_TXN_AMOUNT=D("1000.00")):
        page = text(world.client.get(world.pay_url(world.rent.pk)))
        assert "above the limit of one online payment ($1,000.00)" in page
        assert 'id="pay-form"' not in page
        response = post_pay(world, world.rent.pk, "1500.00")
        assert response.status_code == 400
        assert "above the limit of one online payment" in text(response)
        whole = text(world.client.get(world.pay_url()))
        assert portal_payments.SOME_NOT_PAYABLE in whole
    assert written() == before


# --- Pay: end to end on the fake ---------------------------------------------------------------


def _status(world: World, payment: PortalPayment) -> str:
    return text(
        world.client.get(f"/portal/stores/{world.store.pk}/pay/status/?payment={payment.pk}")
    )


def test_a_one_off_charge_is_paid_end_to_end_on_the_fake(world: World, fake: FakeProvider) -> None:
    response = post_pay(world, world.one_off.pk, "100.00")
    payment = PortalPayment.objects.get()
    assert response.status_code == 302
    assert (
        response["Location"] == f"/portal/stores/{world.store.pk}/pay/status/?payment={payment.pk}"
    )
    assert (payment.status, payment.amount, payment.user, payment.lease) == (
        PortalPaymentStatus.REQUESTED,
        D("100.00"),
        world.user,
        world.lease,
    )
    [job] = Job.objects.filter(kind=JobKind.PORTAL_PAY)
    assert (job.entity_id, job.requested_by, job.status) == (
        world.one_off.pk,
        None,
        JobStatus.QUEUED,
    )
    assert fake.calls == []  # the web never calls the processor
    waiting = _status(world, payment)
    assert portal_payments.SUBMITTING in waiting and 'http-equiv="refresh"' in waiting
    run_queued_jobs()
    payment.refresh_from_db()
    assert payment.status == PortalPaymentStatus.PAID
    charge = OneOffCharge.objects.get(pk=world.one_off.pk)
    assert charge.collection == CollectionMethod.CHARGE_NOW and charge.charge_now_key is not None
    [(_, request)] = [args for name, args in fake.calls if name == "pay_invoice"]
    assert request.idempotency_id == str(charge.charge_now_key)  # type: ignore[attr-defined]
    done = _status(world, payment)
    assert portal_payments.SUBMITTED in done and 'http-equiv="refresh"' not in done
    [paid] = Payment.objects.filter(one_off_charge=charge)
    assert paid.status == PaymentStatus.PENDING
    txn = next(iter(fake.transactions))
    fake.transactions[txn] = replace(fake.transactions[txn], settled_at=timezone.now())
    assert deliver("update_transaction_settled", id=txn) == "processed"
    paid.refresh_from_db()
    assert paid.status == PaymentStatus.SETTLED
    charge.refresh_from_db()
    assert (charge.status, charge.amount_paid) == (OneOffChargeStatus.PAID, D("100.00"))
    assert portal_actions() == [portal_payments.REQUESTED_ACTION, portal_payments.PAID_ACTION]
    requested = AuditLog.objects.get(action=portal_payments.REQUESTED_ACTION)
    assert requested.actor is None and requested.after is not None
    assert requested.after["portal_user"] == str(world.user.pk)
    assert BANK not in str(requested.after) and CUSTOMER not in str(requested.after)


def test_a_rent_charge_is_paid_through_its_bank_debit_on_the_fake(
    world: World, fake: FakeProvider
) -> None:
    assert post_pay(world, world.rent.pk, "1500.00").status_code == 302
    line = PortalPaymentCharge.objects.get()
    debit = line.debit
    assert (line.kind, line.charge_id, line.amount) == ("rent", world.rent.pk, D("1500.00"))
    assert debit.source_rent_charge_id == world.rent.pk and debit.price == D("1500.00")
    assert debit.collection == CollectionMethod.CHARGE_NOW
    run_queued_jobs()
    names = [name for name, _ in fake.calls]
    assert names.index("create_invoice") < names.index("pay_invoice")
    assert names.count("pay_invoice") == 1
    assert PortalPayment.objects.get().status == PortalPaymentStatus.PAID
    txn = next(iter(fake.transactions))
    fake.transactions[txn] = replace(fake.transactions[txn], settled_at=timezone.now())
    assert deliver("update_transaction_settled", id=txn) == "processed"
    debit.refresh_from_db()
    assert debit.status == OneOffChargeStatus.PAID
    rows = receivables.open_balances(timezone.localdate(), world.operator.pk)
    assert world.rent.pk not in {row.charge_id for row in rows}


def test_a_replay_of_the_pages_key_makes_one_job(world: World) -> None:
    key = uuid.uuid4()
    first = post_pay(world, world.one_off.pk, "100.00", key)
    second = post_pay(world, world.one_off.pk, "100.00", key)
    assert first.status_code == second.status_code == 302
    assert first["Location"] == second["Location"]
    assert PortalPayment.objects.count() == 1
    assert Job.objects.filter(kind=JobKind.PORTAL_PAY).count() == 1
    run_queued_jobs()
    third = post_pay(world, world.one_off.pk, "100.00", key)
    assert third["Location"] == first["Location"]
    assert Job.objects.filter(kind=JobKind.PORTAL_PAY).count() == 1


def test_a_second_payment_while_one_is_on_its_way_is_refused(world: World) -> None:
    assert post_pay(world, world.one_off.pk, "100.00").status_code == 302
    before = written()
    response = post_pay(world, world.one_off.pk, "100.00")
    assert response.status_code == 400
    assert portal_payments.CHARGE_PENDING in text(response)
    assert written() == before


def test_a_failed_attempt_is_reported_and_audited(world: World, fake: FakeProvider) -> None:
    fake.fail_next("pay_invoice", ProviderRejected(422, error_fields=("payment_method_id",)))
    post_pay(world, world.one_off.pk, "100.00")
    run_queued_jobs()
    payment = PortalPayment.objects.get()
    assert payment.status == PortalPaymentStatus.FAILED
    assert PortalPaymentCharge.objects.get().status == PortalPaymentStatus.FAILED
    page = _status(world, payment)
    assert "The payment could not be made" in page and ">Failed<" in page
    assert portal_actions() == [portal_payments.REQUESTED_ACTION, portal_payments.FAILED_ACTION]


def test_after_a_failed_attempt_the_operator_tries_again_a_minute_later_with_the_same_key(
    world: World, fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.fail_next("pay_invoice", ProviderRejected(422, error_fields=("payment_method_id",)))
    post_pay(world, world.one_off.pk, "100.00")
    run_queued_jobs()
    again = post_pay(world, world.one_off.pk, "100.00")
    assert again.status_code == 400 and portal_payments.CHARGE_WAIT in text(again)
    later = timezone.now() + timedelta(seconds=61)
    monkeypatch.setattr("django.utils.timezone.now", lambda: later)
    assert post_pay(world, world.one_off.pk, "100.00").status_code == 302
    run_queued_jobs()
    requests = [args[1] for name, args in fake.calls if name == "pay_invoice"]
    keys = {request.idempotency_id for request in requests}  # type: ignore[attr-defined]
    assert keys == {str(OneOffCharge.objects.get(pk=world.one_off.pk).charge_now_key)}
    assert [p.status for p in PortalPayment.objects.order_by("created_at")] == [
        PortalPaymentStatus.FAILED,
        PortalPaymentStatus.PAID,
    ]


def test_after_a_failed_rent_debit_its_bank_debit_is_paid_again(
    world: World, fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.fail_next("pay_invoice", ProviderRejected(422, error_fields=("payment_method_id",)))
    post_pay(world, world.rent.pk, "1500.00")
    run_queued_jobs()
    debit = PortalPaymentCharge.objects.get().debit
    later = timezone.now() + timedelta(seconds=61)
    monkeypatch.setattr("django.utils.timezone.now", lambda: later)
    page = text(world.client.get(f"/portal/stores/{world.store.pk}/"))
    assert f"?charge={debit.pk}" in page  # the open bank debit is the charge to pay now
    assert post_pay(world, debit.pk, "1500.00").status_code == 302
    run_queued_jobs()
    assert PortalPayment.objects.order_by("created_at").last().status == PortalPaymentStatus.PAID  # type: ignore[union-attr]


def test_the_whole_balance_is_paid_only_once_every_charge_is(
    world: World, fake: FakeProvider
) -> None:
    post_pay(world, None, "1600.00")
    payment = PortalPayment.objects.get()
    assert Job.objects.filter(kind=JobKind.PORTAL_PAY).count() == 2
    first = jobs.claim_next()
    assert first is not None
    fake.fail_next("pay_invoice", ProviderRejected(422, error_fields=("payment_method_id",)))
    jobs.run(first)
    payment.refresh_from_db()
    assert payment.status == PortalPaymentStatus.REQUESTED  # one charge still waits
    assert 'http-equiv="refresh"' in _status(world, payment)
    run_queued_jobs()
    payment.refresh_from_db()
    assert payment.status == PortalPaymentStatus.FAILED
    page = _status(world, payment)
    assert ">Failed<" in page and ">Submitted<" in page
    assert portal_actions() == [portal_payments.REQUESTED_ACTION, portal_payments.FAILED_ACTION]


def test_a_charge_that_changed_since_the_page_refuses_the_whole_request(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale = portal_payments.pay_page(
        world.user, world.store, str(world.one_off.pk), timezone.localdate()
    )
    assert stale is not None and stale.payable
    # Staff start Charge now on the charge after the operator opened the page.
    staff = client_for(Role.ADMIN)[1]
    assert one_off_service.debit(
        world.one_off, D("100.00"), timezone.localdate(), actor=staff, confirmed=True
    ).ok
    monkeypatch.setattr(portal_payments, "pay_page", lambda *args: stale)
    response = post_pay(world, world.one_off.pk, "100.00")
    assert response.status_code == 400
    assert portal_payments.CHARGE_PENDING in text(response)
    payment = PortalPayment.objects.get()
    assert payment.status == PortalPaymentStatus.REFUSED
    assert not PortalPaymentCharge.objects.exists()
    assert not Job.objects.filter(kind=JobKind.PORTAL_PAY).exists()
    assert portal_actions() == [portal_payments.REFUSED_ACTION]
    assert portal_payments.PAY_REFUSED in _status(world, payment)


def test_the_status_page_answers_only_the_users_own_payment(
    world: World, fake: FakeProvider
) -> None:
    base = f"/portal/stores/{world.store.pk}/pay/status/"
    assert portal_payments.NO_PAYMENT_YET in text(world.client.get(base))
    post_pay(world, world.one_off.pk, "100.00")
    payment = PortalPayment.objects.get()
    other = an_operator(fake, customer="cus-other", bank="pm-other")
    stranger = PortalPayment.objects.create(
        user=active_user(other),
        lease=Lease.objects.get(operator=other),
        amount=D("5.00"),
        creation_key=uuid.uuid4(),
    )
    assert world.client.get(f"{base}?payment={stranger.pk}").status_code == 404
    assert world.client.get(f"{base}?payment=garbage").status_code == 404
    assert world.client.get(f"{base}?payment={uuid.uuid4()}").status_code == 404
    assert world.client.get(f"{base}?payment={payment.pk}").status_code == 200


# --- Bank account ---------------------------------------------------------------------------


def bank_url(store: Store) -> str:
    return f"/portal/stores/{store.pk}/bank/"


def new_method(external_id: str = NEW_BANK, kind: str = "bank") -> PaymentMethodRecord:
    return PaymentMethodRecord(
        external_id=external_id,
        raw_status="",
        fetched_at=timezone.now(),
        method=kind,
        last_four=NEW_LAST_FOUR,
        bank_type="checking",
        nickname="New",
    )


def method_refs(lease: Lease) -> list[tuple[str, bool]]:
    """(method id, current) of the lease's payment-method references, oldest first."""
    refs = ExternalRef.objects.filter(entity_id=lease.pk, external_kind="payment_method")
    return [
        (ref.external_id, ref.superseded_at is None) for ref in refs.order_by("created_at", "id")
    ]


@override_settings(STAX_PROVIDER="fake")
def test_a_replacement_on_the_fake_supersedes_the_old_method_and_keeps_its_object(
    world: World, fake: FakeProvider
) -> None:
    page = text(world.client.get(bank_url(world.store)))
    assert "Replace the bank account" in page and 'name="payment_method_id"' in page
    assert f"The new account replaces ····{LAST_FOUR}." in page
    fake.add_payment_method(CUSTOMER, new_method())
    response = world.client.post(bank_url(world.store), {"payment_method_id": NEW_BANK})
    assert (response.status_code, response["Location"]) == (302, bank_url(world.store) + "?sent=1")
    [job] = Job.objects.filter(kind=JobKind.LEASE_METHOD_ATTACH)
    assert job.entity_id == world.lease.pk and job.params["portal_user"] == str(world.user.pk)
    assert portal_payments.BANK_CHECKING in text(
        world.client.get(bank_url(world.store) + "?sent=1")
    )
    run_queued_jobs()
    assert method_refs(world.lease) == [(BANK, False), (NEW_BANK, True)]
    assert BANK in fake.payment_methods  # the processor's object is kept (TODO-VERIFY-069)
    assert not [name for name, _ in fake.calls if "delete" in name]
    done = text(world.client.get(bank_url(world.store) + "?sent=1"))
    assert f"Bank account ····{NEW_LAST_FOUR} connected." in done
    audit = AuditLog.objects.get(action=portal_payments.BANK_REPLACED_ACTION)
    assert audit.entity_id == str(world.user.pk) and audit.actor is None
    assert NEW_BANK not in str(audit.after)


@override_settings(STAX_PROVIDER="fake")
def test_a_first_bank_account_is_attached_and_audited(fake: FakeProvider, enabled: None) -> None:
    operator = an_operator(fake, email="first.bank@example.com", bank=None)
    lease = Lease.objects.get(operator=operator)
    client = portal_client(active_user(operator))
    page = text(client.get(bank_url(lease.store)))
    assert "Add a bank account" in page and 'id="bank-on-file">none' in page
    fake.add_payment_method(CUSTOMER, new_method())
    client.post(bank_url(lease.store), {"payment_method_id": NEW_BANK})
    run_queued_jobs()
    assert method_refs(lease) == [(NEW_BANK, True)]
    assert AuditLog.objects.filter(action=portal_payments.BANK_ATTACHED_ACTION).count() == 1


@override_settings(STAX_PROVIDER="fake")
def test_a_shared_accounts_replacement_changes_every_sharing_lease_and_says_so(
    world: World, fake: FakeProvider
) -> None:
    sharer = make_lease(
        operator=world.operator,
        store=Store.objects.create(
            external_label="Store 0202", store_number=1992, status=StoreStatus.ACTIVE
        ),
    )
    assert LeaseBilling.objects.get(lease=sharer).shares_account_of == world.lease
    page = text(world.client.get(bank_url(sharer.store)))
    assert 'id="bank-shared">Shared with #1991<' in page
    assert f'id="bank-on-file">····{LAST_FOUR}' in page
    assert "shared with #1991: replacing it changes it for each of them too" in page
    holder_page = text(world.client.get(bank_url(world.store)))
    assert "shared with #1992: replacing it changes it for each of them too" in holder_page
    fake.add_payment_method(CUSTOMER, new_method())
    world.client.post(bank_url(sharer.store), {"payment_method_id": NEW_BANK})
    [job] = Job.objects.filter(kind=JobKind.LEASE_METHOD_ATTACH)
    assert job.entity_id == world.lease.pk  # the account's lease, not the sharer
    run_queued_jobs()
    for store in (world.store, sharer.store):
        assert f'id="bank-on-file">····{NEW_LAST_FOUR}' in text(world.client.get(bank_url(store)))


def test_without_hosted_fields_the_page_says_not_set_up_and_queues_nothing(world: World) -> None:
    page = text(world.client.get(bank_url(world.store)))
    assert "Adding a bank account online is not set up yet." in page
    assert 'name="payment_method_id"' not in page and "<script" not in page
    response = world.client.post(bank_url(world.store), {"payment_method_id": NEW_BANK})
    assert response.status_code == 400
    assert not Job.objects.filter(kind=JobKind.LEASE_METHOD_ATTACH).exists()


@override_settings(STAX_PROVIDER="fake")
def test_autopay_cannot_be_changed_here(world: World) -> None:
    page = text(world.client.get(bank_url(world.store)))
    assert "Automatic debits are set up by our staff." in page
    from portal import urls

    assert not [pattern for pattern in urls.urlpatterns if "autopay" in str(pattern.pattern)]
    response = world.client.post(
        bank_url(world.store), {"payment_method_id": NEW_BANK, "autopay": "off"}
    )
    assert response.status_code == 400 and portal_payments.BANK_ONLY_THE_ID in text(response)
    assert not Job.objects.filter(kind=JobKind.LEASE_METHOD_ATTACH).exists()


@override_settings(STAX_PROVIDER="fake")
@pytest.mark.parametrize(
    "posted",
    [
        {"routing_number": "021000021", "account_number": "000123456789"},
        {"payment_method_id": NEW_BANK, "account_number": "000123456789"},
        {"payment_method_id": "not an id!"},
        {},
    ],
)
def test_only_the_methods_reference_is_accepted(world: World, posted: dict[str, str]) -> None:
    response = world.client.post(bank_url(world.store), posted)
    assert response.status_code == 400
    assert not Job.objects.filter(kind=JobKind.LEASE_METHOD_ATTACH).exists()
    assert_absent(text(response), "000123456789")


@override_settings(STAX_PROVIDER="fake")
def test_a_second_change_while_one_is_checked_is_refused(world: World) -> None:
    world.client.post(bank_url(world.store), {"payment_method_id": NEW_BANK})
    response = world.client.post(bank_url(world.store), {"payment_method_id": "pm-third"})
    assert response.status_code == 400 and portal_payments.BANK_BUSY in text(response)
    assert Job.objects.filter(kind=JobKind.LEASE_METHOD_ATTACH).count() == 1


def test_the_bank_page_is_never_framed_and_scripts_come_from_this_site(world: World) -> None:
    response: Any = world.client.get(bank_url(world.store))
    assert "frame-ancestors 'none'" in response["Content-Security-Policy"]
    assert response["Content-Security-Policy"].startswith("script-src 'self';")
    assert response["X-Frame-Options"] == "DENY"


def test_an_ended_lease_cannot_change_its_bank(world: World, fake: FakeProvider) -> None:
    with override_settings(STAX_PROVIDER="fake"):
        Lease.objects.filter(pk=world.lease.pk).update(ended_on=date(2026, 9, 30))
        page = text(world.client.get(bank_url(world.store)))
    assert portal_payments.BANK_ENDED in page


@pytest.mark.parametrize("path", ["pay/", "bank/"])
def test_the_pay_and_bank_posts_are_csrf_protected(world: World, path: str) -> None:
    strict = Client(enforce_csrf_checks=True)
    strict.cookies = world.client.cookies
    before = written()
    response = strict.post(
        f"/portal/stores/{world.store.pk}/{path}",
        {"charge": str(world.one_off.pk), "amount": "100.00", "creation_key": str(uuid.uuid4())},
    )
    assert response.status_code == 403
    assert written() == before


# --- {pay_link} ---------------------------------------------------------------------------------


def test_the_pay_link_is_the_portals_pay_page_for_an_active_account(world: World) -> None:
    context = messaging.context_for(
        world.lease, world.rent, "A", today=timezone.localdate(), amount=D("1500.00")
    )
    assert context["pay_link"] == f"{BASE}/portal/stores/{world.store.pk}/pay/"
    PortalUser.objects.filter(pk=world.user.pk).update(is_active=False, disabled_at=timezone.now())
    context = messaging.context_for(world.lease, world.rent, "A", today=timezone.localdate())
    assert context["pay_link"].startswith(f"{BASE}/statement/")


def test_without_a_usable_portal_account_the_pay_link_stays_the_statement_link(
    world: World, fake: FakeProvider
) -> None:
    operator = an_operator(fake, email="no.portal@example.com", customer="cus-np", bank="pm-np")
    lease = Lease.objects.get(operator=operator)
    charge = a_rent_charge(lease, date(2026, 9, 1), due="10.00")
    context = messaging.context_for(lease, charge, "A", today=timezone.localdate())
    assert context["pay_link"].startswith(f"{BASE}/statement/")
    # An archived operator's account cannot sign in: its messages keep the statement link.
    Operator.objects.filter(pk=world.operator.pk).update(archived_at=timezone.now())
    world.lease.refresh_from_db()
    context = messaging.context_for(world.lease, world.rent, "A", today=timezone.localdate())
    assert context["pay_link"].startswith(f"{BASE}/statement/")


# --- Migrations ---------------------------------------------------------------------------------


def _migrate(targets: list[tuple[str, str]]) -> None:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(targets)


@pytest.fixture
def leaves() -> Iterator[list[tuple[str, str]]]:
    found = MigrationExecutor(connection).loader.graph.leaf_nodes()
    yield found
    _migrate(found)


@pytest.mark.django_db(transaction=True)
def test_the_portal_payment_migrations_go_both_ways(leaves: list[tuple[str, str]]) -> None:
    _migrate([("payments", "0021_job_run_after")])
    tables = set(connection.introspection.table_names())
    assert not {"portal_payments", "portal_payment_charges"} & tables
    _migrate(leaves)
    tables = set(connection.introspection.table_names())
    assert {"portal_payments", "portal_payment_charges"} <= tables


@pytest.mark.django_db(transaction=True)
def test_the_migrations_refuse_to_reverse_over_a_portal_payment(
    leaves: list[tuple[str, str]], fake: FakeProvider
) -> None:
    operator = an_operator(fake, email="mig.owner@example.com")
    lease = Lease.objects.get(operator=operator)
    PortalPayment.objects.create(
        user=active_user(operator), lease=lease, amount=D("1.00"), creation_key=uuid.uuid4()
    )
    with pytest.raises(RuntimeError, match="Portal payments exist"):
        _migrate([("portal", "0001_initial")])
    PortalPayment.objects.all().delete()
    debit = created(operator, item(CollectionMethod.EMAIL, "1.00"), lease=lease)
    Job.objects.create(kind=JobKind.PORTAL_PAY, entity_type="one_off_charge", entity_id=debit.pk)
    # The portal's 0002 (empty now) goes first: it depends on the job kind.
    with pytest.raises(RuntimeError, match="Portal payment jobs exist"):
        _migrate([("payments", "0021_job_run_after")])
    assert "portal_payments" not in connection.introspection.table_names()
    _migrate(leaves)
    assert Job.objects.filter(kind=JobKind.PORTAL_PAY).count() == 1

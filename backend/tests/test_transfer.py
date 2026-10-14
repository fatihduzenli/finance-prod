"""The transfer wizard (docs/qa/PLAN-012e-lease-end-transfer-installments.md D6; §6
"Transfer"): the blockers (every lease-end blocker and every charge still owed, listed
per charge), the incoming start date, the hand-over day and the one-lease-per-day
invariant, the incoming lease and the move-in draft (nothing at the payment provider
until it is sent), the inline operator and its sync job, the audit, the pages and the
roles. The adversarial cases are the QA owner's.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client

from accounts import registry
from accounts.roles import Role
from ledger.models import (
    AuditLog,
    CollectionStatus,
    InvoiceDraft,
    InvoiceDraftStatus,
    Lease,
    LeaseEnd,
    LeaseEndReason,
    LeaseEndStatus,
    LeaseRentHistory,
    LeaseTransfer,
    LeaseTransferStatus,
    OneOffCharge,
    OneOffChargeStatus,
    Operator,
    Payment,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    RefundMethod,
    RentCharge,
)
from ledger.services import attention, invoice_builder, lease_end, transfer
from payments.models import ExternalRef, Job, JobKind
from tests.test_lease_end import (
    ENDED,
    TODAY,
    a_live_plan,
    a_manual_charge,
    a_schedule,
    a_store_lease,
    a_wire_lease,
    an_email_charge,
    deposit,
    fresh,
    october,
    settle,
)
from tests.test_lease_services import make_operator
from tests.test_operator_views import client_for
from tests.test_setup_charges import fee

pytestmark = pytest.mark.django_db

D = Decimal
STARTS = ENDED + timedelta(days=1)


# --- Builders ------------------------------------------------------------------------------


@pytest.fixture
def admin() -> tuple[Client, User]:
    return client_for(Role.ADMIN)


@pytest.fixture
def pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)


def incoming_operator(name: str = "Southwind Stores") -> Operator:
    """An operator linked to a (fake) customer at the payment provider."""
    operator = Operator.objects.create(
        display_name=name, email=f"{uuid.uuid4().hex[:8]}@example.com"
    )
    ExternalRef.objects.create(
        entity_type="operator",
        entity_id=operator.pk,
        provider="stax_pay",
        external_kind="customer",
        external_id=f"cus-{uuid.uuid4().hex[:8]}",
    )
    return operator


def begin(lease: Lease, user: User | None = None, *, ended_on: date = ENDED) -> LeaseTransfer:
    """A transfer of the lease's store with its operator chosen and its end date set."""
    row = transfer.start(lease.store, user, today=TODAY)
    assert transfer.set_operator(row, incoming_operator(), actor=user).ok
    end = fresh(row).lease_end
    outcome = lease_end.set_terms(
        end, ended_on, LeaseEndReason.TRANSFER, True, actor=user, today=TODAY
    )
    assert outcome.ok, outcome.message
    return LeaseTransfer.objects.get(pk=row.pk)


def incoming(row: LeaseTransfer, user: User | None = None, **fields: Any) -> transfer.Outcome:
    values: dict[str, Any] = {
        "started_on": STARTS,
        "rent": D("2500.00"),
        "debit_day": 1,
        "second_debit_day": None,
        "channel": "wire",
        "prorate_first_month": True,
        "deposit_amount": D("3000.00"),
        "setup": [transfer.SetupInput(fee("sign_fee"), D("450.00"))],
        "actor": user,
    }
    values.update(fields)
    return transfer.set_incoming(fresh(row), **values)


def confirm(row: LeaseTransfer, user: User | None) -> transfer.Outcome:
    return transfer.confirm(fresh(row), user, confirmed=True, today=TODAY)


def a_clean_lease() -> Lease:
    """October paid in full and a $2,000 deposit held: nothing blocks."""
    lease = a_store_lease()
    october(lease, paid="2000.00")
    deposit(lease, "2000.00")
    return lease


def ready(user: User) -> LeaseTransfer:
    """A clean transfer up to the confirm: the refund settled, the incoming lease set."""
    row = begin(a_clean_lease(), user)
    end = fresh(row).lease_end
    assert settle(end, RefundMethod.ZELLE, actor=user).ok
    outcome = incoming(row, user)
    assert outcome.ok, outcome.message
    return LeaseTransfer.objects.get(pk=row.pk)


def snapshot() -> tuple[int, ...]:
    return (
        Lease.objects.count(),
        Lease.objects.filter(ended_on__isnull=False).count(),
        InvoiceDraft.objects.count(),
        Payment.objects.count(),
        OneOffCharge.objects.count(),
        Job.objects.count(),
        LeaseEnd.objects.filter(status=LeaseEndStatus.CONFIRMED).count(),
    )


# --- Start, operator ----------------------------------------------------------------------


def test_start_makes_one_transfer_with_a_lease_end_whose_reason_is_transfer() -> None:
    lease = a_clean_lease()
    row = transfer.start(lease.store, None, today=TODAY)
    assert transfer.start(lease.store, None, today=TODAY).pk == row.pk
    assert row.status == LeaseTransferStatus.DRAFT and row.outgoing_lease_id == lease.pk
    end = LeaseEnd.objects.get(lease=lease)
    assert row.lease_end_id == end.pk and end.reason == LeaseEndReason.TRANSFER


def test_start_is_refused_while_a_plain_lease_end_is_in_progress() -> None:
    lease = a_clean_lease()
    lease_end.start(lease, None)
    with pytest.raises(ValueError, match="lease end is in progress"):
        transfer.start(lease.store, None, today=TODAY)
    assert not LeaseTransfer.objects.exists()


def test_the_reason_is_fixed_and_the_lease_end_cannot_be_confirmed_or_cancelled_alone() -> None:
    row = begin(a_clean_lease())
    end = fresh(row).lease_end
    refused = lease_end.set_terms(end, ENDED, "moved_out", True, actor=None, today=TODAY)
    assert not refused.ok and refused.message == lease_end.TRANSFER_REASON_FIXED
    assert settle(end).ok
    confirmed = lease_end.confirm(end, None, confirmed=True, today=TODAY)
    assert not confirmed.ok and confirmed.message == lease_end.TRANSFER_CONFIRMS
    cancelled = lease_end.cancel(end, None)
    assert not cancelled.ok and cancelled.message == lease_end.TRANSFER_CANCELS
    assert fresh(row.outgoing_lease).ended_on is None


def test_an_archived_or_the_outgoing_operator_cannot_be_chosen() -> None:
    lease = a_clean_lease()
    row = transfer.start(lease.store, None, today=TODAY)
    same = transfer.set_operator(row, lease.operator, actor=None)
    assert not same.ok and same.message == transfer.OPERATOR_SAME
    archived = incoming_operator()
    Operator.objects.filter(pk=archived.pk).update(archived_at="2026-10-01T00:00:00Z")
    gone = transfer.set_operator(row, fresh(archived), actor=None)
    assert not gone.ok and gone.message == transfer.OPERATOR_ARCHIVED
    assert fresh(row).incoming_operator_id is None


def test_the_search_finds_operators_by_name_but_not_archived_ones_or_the_outgoing_one() -> None:
    lease = a_clean_lease()
    row = transfer.start(lease.store, None, today=TODAY)
    found = incoming_operator("Southwind Stores")
    archived = incoming_operator("Southwind Archive")
    Operator.objects.filter(pk=archived.pk).update(archived_at="2026-10-01T00:00:00Z")
    assert [item.pk for item in transfer.search_operators("southwind", row)] == [found.pk]
    assert transfer.search_operators("Northwind", row) == []
    assert transfer.search_operators("", row) == []


# --- Blockers --------------------------------------------------------------------------------


def test_an_open_balance_blocks_listed_per_charge_with_links() -> None:
    lease = a_store_lease()
    rent = october(lease)
    other = OneOffCharge.objects.create(
        operator=lease.operator,
        lease=lease,
        category="damage",
        collection="manual",
        description="Broken shelf",
        price=D("300.00"),
        due_date=date(2026, 10, 5),
    )
    row = begin(lease)
    found = transfer.blockers(row, TODAY)
    assert [item.kind for item in found] == [transfer.OPEN_BALANCE, transfer.OPEN_BALANCE]
    assert {item.url for item in found} == {
        f"/charges/{rent.pk}/",
        f"/one-off-charges/{other.pk}/",
    }
    # The last month as the end leaves it: 19 of 31 days are owed, not the whole month.
    assert any("$1,225.81 is still owed" in item.text for item in found)
    assert any("Broken shelf" in item.text and "$300.00" in item.text for item in found)


def test_before_the_end_date_the_open_balances_are_listed_as_they_stand() -> None:
    lease = a_store_lease()
    october(lease)
    row = transfer.start(lease.store, None, today=TODAY)
    [found] = transfer.blockers(row, TODAY)
    assert found.kind == transfer.OPEN_BALANCE and "$2,000.00 is still owed" in found.text


def test_a_deposit_held_and_a_paid_lease_do_not_block() -> None:
    row = begin(a_clean_lease())
    assert transfer.blockers(row, TODAY) == []


@pytest.mark.parametrize("kind", ["live_invoice", "autopay", "pending_payment"])
def test_each_lease_end_blocker_blocks_the_transfer(kind: str) -> None:
    lease = a_clean_lease()
    if kind == "live_invoice":
        charge = an_email_charge(lease)
        # Paid, so only the live invoice is listed (not the balance).
        Payment.objects.create(
            one_off_charge=charge,
            kind=PaymentKind.ONE_OFF,
            method=PaymentMethod.WIRE,
            amount=D("300.00"),
            status=PaymentStatus.SETTLED,
            initiated_on=TODAY,
            settled_on=TODAY,
            note="Wire received.",
        )
        OneOffCharge.objects.filter(pk=charge.pk).update(
            status=OneOffChargeStatus.PARTIAL, amount_paid=D("300.00")
        )
    elif kind == "autopay":
        a_schedule(lease)
    else:
        Payment.objects.create(
            rent_charge=RentCharge.objects.get(lease=lease),
            kind=PaymentKind.RENT,
            method=PaymentMethod.ACH,
            amount=D("10.00"),
            status=PaymentStatus.PENDING,
            initiated_on=TODAY,
        )
    row = begin(lease)
    assert kind in {item.kind for item in transfer.blockers(row, TODAY)}


def test_a_live_installment_plan_blocks_the_transfer() -> None:
    lease = a_wire_lease()
    plan = a_live_plan(a_manual_charge(lease))
    row = begin(lease)
    found = [item for item in transfer.blockers(row, TODAY) if item.url == f"/plans/{plan.pk}/"]
    assert [item.kind for item in found] == [lease_end.INSTALLMENT_PLAN]


def test_confirm_reads_the_blockers_again_and_writes_nothing(admin: Any) -> None:
    _, user = admin
    row = ready(user)
    an_email_charge(row.outgoing_lease)  # appears between the steps
    before = snapshot()
    outcome = confirm(row, user)
    assert not outcome.ok and outcome.message.startswith("The store cannot be transferred yet")
    assert snapshot() == before
    assert fresh(row).status == LeaseTransferStatus.DRAFT


# --- The incoming lease ---------------------------------------------------------------------


@pytest.mark.parametrize("started_on", [ENDED, ENDED - timedelta(days=30)])
def test_an_incoming_start_on_or_before_the_end_date_is_refused(started_on: date) -> None:
    row = begin(a_clean_lease())
    outcome = incoming(row, started_on=started_on)
    assert not outcome.ok and outcome.field == "started_on"
    assert outcome.message == transfer.START_TOO_EARLY.format(
        end=ENDED.isoformat(), first=STARTS.isoformat()
    )
    assert fresh(row).incoming_started_on is None


def test_confirm_refuses_when_the_end_date_moved_past_the_incoming_start(admin: Any) -> None:
    _, user = admin
    row = ready(user)
    end = fresh(row).lease_end
    assert lease_end.set_terms(end, STARTS, "transfer", True, actor=user, today=TODAY).ok
    before = snapshot()
    outcome = confirm(row, user)
    assert not outcome.ok and outcome.message.startswith("The incoming lease must start after")
    assert snapshot() == before


def test_the_incoming_terms_are_checked() -> None:
    row = begin(a_clean_lease())
    assert incoming(row, rent=D("0")).field == "rent"
    assert incoming(row, debit_day=None).field == "debit_day"
    assert incoming(row, second_debit_day=1).field == "second_debit_day"
    assert incoming(row, channel="unknown").field == "channel"
    assert incoming(row, deposit_amount=D("-1")).field == "deposit_amount"
    sign = fee("sign_fee")
    twice = [transfer.SetupInput(sign, D("1")), transfer.SetupInput(sign, D("2"))]
    assert incoming(row, setup=twice).message == transfer.SETUP_TWICE.format(name=sign.name)
    deposit_item = fee("security_deposit")
    not_setup = [transfer.SetupInput(deposit_item, D("1"))]
    assert not incoming(row, setup=not_setup).ok
    assert fresh(row).incoming_started_on is None


def test_the_deposit_defaults_from_the_catalogue_else_from_the_outgoing_deposit() -> None:
    row = begin(a_clean_lease())
    assert transfer.default_deposit(row) == D("2000.00")
    fee("security_deposit", default_price=D("2750.00"))
    assert transfer.default_deposit(row) == D("2750.00")


def test_a_clean_transfer_ends_the_outgoing_lease_and_starts_the_incoming_one(
    admin: Any,
) -> None:
    _, user = admin
    row = ready(user)
    outgoing = row.outgoing_lease
    outcome = confirm(row, user)
    assert outcome.ok, outcome.message
    row = fresh(row)
    assert row.status == LeaseTransferStatus.CONFIRMED and row.confirmed_at is not None
    # The outgoing lease: ended on the end date with the statement, the refund recorded.
    outgoing = fresh(outgoing)
    assert outgoing.ended_on == ENDED and outgoing.collection_status == CollectionStatus.ENDED
    end = fresh(row.lease_end)
    assert end.status == LeaseEndStatus.CONFIRMED and end.reason == LeaseEndReason.TRANSFER
    assert end.refund_payment is not None
    assert Job.objects.filter(kind=JobKind.LEASE_END_DOCUMENTS).count() == 1
    # The incoming lease: the day after, the new operator, active, its rent.
    new = row.incoming_lease
    assert new is not None and new.store_id == outgoing.store_id
    assert new.started_on == STARTS and new.operator_id == row.incoming_operator_id
    assert new.collection_status == CollectionStatus.ACTIVE and new.prorate_first_month
    assert (new.debit_day, new.payment_channel) == (1, "wire")
    assert list(
        LeaseRentHistory.objects.filter(lease=new).values_list("amount", "effective_from")
    ) == [(D("2500.00"), STARTS)]
    # One draft with the deposit and the setup line; no charge, nothing at the provider.
    draft = row.draft
    assert draft is not None and draft.status == InvoiceDraftStatus.DRAFT
    assert (draft.operator_id, draft.lease_id) == (row.incoming_operator_id, new.pk)
    assert draft.memo == f"Move-in charges for store {outgoing.store.external_label}"
    lines = [(line.item, line.line_total) for line in invoice_builder.lines_of(draft)]
    assert lines == [("Security deposit", D("3000.00")), ("Sign fee", D("450.00"))]
    assert not OneOffCharge.objects.filter(lease=new).exists()
    assert set(Job.objects.values_list("kind", flat=True)) == {JobKind.LEASE_END_DOCUMENTS}
    # Sent, the draft makes the charges and the provider job.
    sent = invoice_builder.send(draft.pk, user, today=TODAY)
    assert sent.ok, sent.message
    made = OneOffCharge.objects.filter(lease=new).order_by("price")
    assert [(charge.price, charge.fee_type.code if charge.fee_type else "") for charge in made] == [
        (D("450.00"), "sign_fee"),
        (D("3000.00"), "security_deposit"),
    ]
    assert Job.objects.filter(kind=JobKind.INVOICE_CREATE).count() == 1


def test_without_a_deposit_or_setup_item_no_draft_is_made(admin: Any) -> None:
    _, user = admin
    row = begin(a_clean_lease(), user)
    assert settle(fresh(row).lease_end, actor=user).ok
    assert incoming(row, user, deposit_amount=None, setup=[]).ok
    outcome = confirm(row, user)
    assert outcome.ok and outcome.message == transfer.CONFIRMED_NO_DRAFT
    assert fresh(row).draft is None and not InvoiceDraft.objects.exists()


def test_a_failed_draft_rolls_the_whole_transfer_back(
    admin: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, user = admin
    row = ready(user)
    before = snapshot()
    monkeypatch.setattr(
        invoice_builder,
        "create_draft",
        lambda *args, **kwargs: invoice_builder.Outcome(False, "Refused for the test."),
    )
    outcome = confirm(row, user)
    assert not outcome.ok and outcome.message == "Refused for the test."
    assert snapshot() == before
    assert fresh(row.outgoing_lease).ended_on is None
    assert fresh(row.lease_end).status == LeaseEndStatus.DRAFT


def test_another_lease_in_effect_after_the_start_refuses(admin: Any) -> None:
    _, user = admin
    row = ready(user)
    Lease.objects.create(
        store=row.store,
        operator=make_operator(email="third@example.com", customer=None),
        debit_day=1,
        payment_channel="wire",
        started_on=date(2026, 12, 1),
        collection_status=CollectionStatus.ACTIVE,
    )
    before = snapshot()
    outcome = confirm(row, user)
    assert not outcome.ok and outcome.message == transfer.OTHER_LEASE.format(
        start=STARTS.isoformat()
    )
    assert snapshot() == before


def held_on(lease: Lease, day: date) -> bool:
    """Whether the lease covers ``day`` by its dates (an ended lease included, unlike
    the attention list's rule, which reads only leases still collecting)."""
    return (
        lease.collection_status != CollectionStatus.PROSPECTIVE
        and (lease.started_on is None or lease.started_on <= day)
        and (lease.ended_on is None or lease.ended_on >= day)
    )


def test_the_hand_over_day_and_one_lease_in_effect_on_every_day(admin: Any) -> None:
    _, user = admin
    row = ready(user)
    assert confirm(row, user).ok
    outgoing, new = fresh(row.outgoing_lease), fresh(row).incoming_lease
    assert new is not None
    # The hand-over: the outgoing lease's last day, then the incoming lease's first.
    assert held_on(outgoing, ENDED) and not held_on(new, ENDED)
    assert held_on(new, STARTS) and not held_on(outgoing, STARTS)
    # Every day of a 400-day window around the transfer is covered by at most one lease,
    # by the dates and by the attention list's rule.
    leases = list(Lease.objects.filter(store=row.store))
    first = ENDED - timedelta(days=200)
    for offset in range(400):
        day = first + timedelta(days=offset)
        assert sum(held_on(lease, day) for lease in leases) <= 1, day
        assert sum(attention._in_effect(lease, day) for lease in leases) <= 1, day


def test_every_step_is_audited(admin: Any) -> None:
    _, user = admin
    row = ready(user)
    assert confirm(row, user).ok
    actions = list(
        AuditLog.objects.filter(entity_type="lease_transfer", entity_id=str(row.pk))
        .order_by("id")
        .values_list("action", flat=True)
    )
    assert actions == [
        "lease_transfer.started",
        "lease_transfer.operator_set",
        "lease_transfer.incoming_set",
        "lease_transfer.confirmed",
    ]
    entry = AuditLog.objects.get(action="lease_transfer.confirmed")
    assert entry.actor_id == user.pk and entry.after is not None
    assert entry.after["incoming_lease"] == str(fresh(row).incoming_lease_id)
    ended = AuditLog.objects.get(action="lease_end.confirmed")
    assert ended.after is not None and ended.after["transfer"] == str(row.pk)
    new = str(fresh(row).incoming_lease_id)
    assert AuditLog.objects.filter(action="lease.created", entity_id=new).exists()


def test_cancel_deletes_the_transfer_and_its_lease_end_only() -> None:
    row = begin(a_clean_lease())
    before = snapshot()
    outcome = transfer.cancel(fresh(row), None)
    assert outcome.ok and outcome.message == transfer.CANCELLED
    assert not LeaseTransfer.objects.exists() and not LeaseEnd.objects.exists()
    assert snapshot()[:6] == before[:6]
    assert AuditLog.objects.filter(action="lease_transfer.cancelled").count() == 1


# --- Pages and roles -------------------------------------------------------------------------


def transfer_paths(row: LeaseTransfer) -> list[str]:
    return [
        f"/stores/{row.store_id}/transfer/",
        *(
            f"/transfers/{row.pk}/{name}/"
            for name in ("operator", "end", "blockers", "incoming", "confirm", "cancel")
        ),
    ]


def test_the_routes_are_admin_only() -> None:
    for name in ("operator", "incoming", "confirm", "cancel"):
        rule = registry.rule_for(f"ledger:transfers:{name}")
        assert rule is not None and rule.min_role is Role.ADMIN and rule.methods == {"POST"}
    rule = registry.rule_for("ledger:stores:transfer")
    assert rule is not None and rule.min_role is Role.ADMIN
    assert {"ledger:transfers:end", "ledger:transfers:blockers"} <= (registry.READ_ONLY_ROUTE_NAMES)


@pytest.mark.parametrize("role", [Role.VIEWER, Role.BILLING])
def test_viewer_and_billing_get_403_on_every_step(role: Role) -> None:
    row = begin(a_clean_lease())
    client, _ = client_for(role)
    for path in transfer_paths(row):
        assert client.get(path).status_code in (403, 405), path
        assert client.post(path, {"confirmed": "on", "action": "create"}).status_code == 403, path
    assert fresh(row).status == LeaseTransferStatus.DRAFT and LeaseTransfer.objects.count() == 1
    store_page = client.get(f"/stores/{row.store_id}/").content.decode()
    assert "/transfer/" not in store_page


def test_unknown_or_malformed_ids_are_404(admin: Any) -> None:
    client, _ = admin
    for key in ("nope", str(uuid.uuid4())):
        assert client.get(f"/transfers/{key}/operator/").status_code == 404
        assert client.get(f"/stores/{key}/transfer/").status_code == 404


def test_an_operator_created_on_the_step_queues_its_sync_job(admin: Any, pinned: None) -> None:
    client, _ = admin
    lease = a_clean_lease()
    client.post(f"/stores/{lease.store_id}/transfer/")
    row = LeaseTransfer.objects.get()
    response = client.post(
        f"/transfers/{row.pk}/operator/",
        {
            "action": "create",
            "creation_key": str(uuid.uuid4()),
            "display_name": "Eastwind Stores",
            "email": "eastwind@example.com",
            "phone": "",
        },
    )
    assert response["Location"] == f"/transfers/{row.pk}/end/"
    created = Operator.objects.get(display_name="Eastwind Stores")
    assert fresh(row).incoming_operator_id == created.pk
    job = Job.objects.get(kind=JobKind.OPERATOR_SYNC)
    assert job.entity_id == created.pk


def test_an_admin_walks_the_transfer_from_the_store_page(admin: Any, pinned: None) -> None:
    client, _ = admin
    lease = a_clean_lease()
    chosen = incoming_operator("Westwind Stores")
    store_page = client.get(f"/stores/{lease.store_id}/").content.decode()
    assert f'href="/stores/{lease.store_id}/transfer/"' in store_page
    assert "Transfer to another operator" in store_page
    start = client.get(f"/stores/{lease.store_id}/transfer/").content.decode()
    assert 'id="step-help"' in start and "Start" in start
    response = client.post(f"/stores/{lease.store_id}/transfer/")
    row = LeaseTransfer.objects.get()
    base = f"/transfers/{row.pk}"
    end = fresh(row).lease_end
    assert response["Location"] == f"{base}/operator/"
    page = client.get(f"{base}/operator/", {"q": "westwind"}).content.decode()
    assert "Westwind Stores" in page and 'id="step-help"' in page
    for step in ("incoming", "confirm"):
        assert client.get(f"{base}/{step}/")["Location"] == f"{base}/operator/"
    response = client.post(f"{base}/operator/", {"action": "pick", "operator": str(chosen.pk)})
    assert response["Location"] == f"{base}/end/"
    assert client.get(f"{base}/end/")["Location"] == f"/lease-ends/{end.pk}/terms/"
    terms_page = client.get(f"/lease-ends/{end.pk}/terms/").content.decode()
    assert "Transfer store" in terms_page and "the reason is fixed" in terms_page
    response = client.post(
        f"/lease-ends/{end.pk}/terms/",
        {"ended_on": ENDED.isoformat(), "reason": "moved_out", "prorate_last_month": "on"},
    )
    assert response["Location"] == f"{base}/blockers/"
    assert fresh(end).reason == LeaseEndReason.TRANSFER
    assert client.get(f"/lease-ends/{end.pk}/blockers/")["Location"] == f"{base}/blockers/"
    assert 'id="no-blockers"' in client.get(f"{base}/blockers/").content.decode()
    response = client.post(f"/lease-ends/{end.pk}/statement/", {})
    assert response["Location"] == f"/lease-ends/{end.pk}/settlement/"
    response = client.post(
        f"/lease-ends/{end.pk}/settlement/",
        {"method": "zelle", "refund_on": TODAY.isoformat(), "reference": "ZL-9", "note": ""},
    )
    assert response["Location"] == f"{base}/incoming/"
    assert client.get(f"/lease-ends/{end.pk}/confirm/")["Location"] == f"{base}/confirm/"
    incoming_page = client.get(f"{base}/incoming/").content.decode()
    assert STARTS.isoformat() in incoming_page and "Sign fee" in incoming_page
    response = client.post(
        f"{base}/incoming/",
        {
            "started_on": ENDED.isoformat(),
            "rent": "2500.00",
            "debit_day": "1",
            "payment_channel": "wire",
            "prorate_first_month": "on",
            "deposit_amount": "3000.00",
        },
    )
    assert response["Location"] == f"{base}/incoming/"
    assert "must start after" in client.get(f"{base}/incoming/").content.decode()
    response = client.post(
        f"{base}/incoming/",
        {
            "started_on": STARTS.isoformat(),
            "rent": "2500.00",
            "debit_day": "1",
            "payment_channel": "wire",
            "prorate_first_month": "on",
            "deposit_amount": "3000.00",
            "setup_sign_fee_tick": "on",
            "setup_sign_fee_price": "450.00",
        },
    )
    assert response["Location"] == f"{base}/confirm/"
    confirm_page = client.get(f"{base}/confirm/").content.decode()
    assert "Are you sure?" in confirm_page and "Move-in charges for store" in confirm_page
    assert "Sign fee: $450.00" in confirm_page and "starts on " + STARTS.isoformat() in confirm_page
    assert client.post(f"{base}/confirm/", {})["Location"] == f"{base}/confirm/"
    assert fresh(row).status == LeaseTransferStatus.DRAFT
    response = client.post(f"{base}/confirm/", {"confirmed": "on"})
    assert response["Location"] == f"{base}/confirm/"
    done = client.get(f"{base}/confirm/").content.decode()
    row = fresh(row)
    assert row.status == LeaseTransferStatus.CONFIRMED and row.draft is not None
    assert f"/invoices/drafts/{row.draft.pk}/" in done
    assert f"/leases/{row.incoming_lease_id}/" in done


def test_cancel_from_the_page_returns_to_the_store(admin: Any, pinned: None) -> None:
    client, _ = admin
    row = begin(a_clean_lease())
    response = client.post(f"/transfers/{row.pk}/cancel/")
    assert response["Location"] == f"/stores/{row.store_id}/"
    assert not LeaseTransfer.objects.exists() and not LeaseEnd.objects.exists()

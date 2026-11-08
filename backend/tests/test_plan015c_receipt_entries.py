"""PLAN-015c rework (Fable's MAJOR finding, 2026-11-07): one receipt per payment entry.

An entry recorded across several charges is stored as one ``Payment`` row per charge sharing
``allocation_group``. Its receipt covers every row on the same lease: one number, the sum,
every invoice and charge in "Applied to", the same bytes from any row. One e-mail per entry,
queued once its rows are all written; a later row of the entry queues nothing. The Payments
lists link the receipt once per entry; the portal's isolation holds for every row's id. A
bank deposit split over two stores shares one group, and each store gets its own receipt.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from django.utils import timezone

from accounts.roles import Role
from ledger import merge
from ledger.models import (
    Lease,
    LeaseBilling,
    MessageKind,
    OneOffCharge,
    OutboundMessage,
    Payment,
    PaymentMethod,
    Store,
)
from ledger.services import invoice_builder, invoice_messages, messaging
from ledger.services.pdf.receipt import (
    entry_of,
    facts,
    receipt_filename,
    receipt_number,
    render_receipt,
)
from mail.memory import MemoryMailSender
from payments.fake import FakeProvider
from tests.job_helpers import run_queued_jobs
from tests.pdf_helpers import billed_store, builder_invoice, charge, company, pdf_text, settle
from tests.receivables_helpers import a_lease, a_rent_charge, a_store
from tests.test_collect_parity import a_world, sign, two_line_draft
from tests.test_operator_views import client_for
from tests.test_plan015c_email import memory as memory  # noqa: PLC0414 (the fixture)
from tests.test_plan015c_email import seeded
from tests.test_portal import World, active_user, portal_client
from tests.test_portal import clock as clock  # noqa: PLC0414 (the fixture, re-exported)
from tests.test_portal import world as world  # noqa: PLC0414 (the fixture, re-exported)

pytestmark = pytest.mark.django_db

D = Decimal
TODAY = date(2026, 11, 30)
PAID_ON = date(2026, 11, 20)
TWO = [
    ("Rent — December 2026", "Store #1991 Istanbul · Dec 1–31", "2000.00"),
    ("Access fee", "One-time", "800.00"),
]


@pytest.fixture(autouse=True)
def window_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """The send window is tested in test_messaging_engine.py; here a message goes at once."""
    monkeypatch.setattr(messaging, "scheduled_for", lambda now: now)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider(pay_invoice_enabled=True)
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


def lines_of(data: bytes) -> list[str]:
    return [line.strip() for line in pdf_text(data).splitlines() if line.strip()]


def paid(billed: OneOffCharge, amount: str) -> None:
    """The charge's stored paid amount, as the payment services recompute it."""
    OneOffCharge.objects.filter(pk=billed.pk).update(amount_paid=D(amount))


def a_two_line_wire() -> tuple[str, list[Payment], Lease]:
    """A $2,800.00 wire recorded against a two-line invoice: two rows, one group."""
    company()
    lease = billed_store()
    invoice, charges = builder_invoice(lease, TWO)
    group = uuid.uuid4()
    rows = [
        settle(
            billed,
            price,
            PAID_ON,
            PaymentMethod.WIRE,
            reference="WIRE-1001",
            allocation_group=group,
        )
        for billed, (_, _, price) in zip(charges, TWO, strict=True)
    ]
    for billed, (_, _, price) in zip(charges, TWO, strict=True):
        paid(billed, price)
    return invoice.invoice_number, rows, lease


# --- The PDF ------------------------------------------------------------------------------------


def test_a_two_line_wire_has_one_receipt_with_both_charges_and_their_sum() -> None:
    number, (first, second), _ = a_two_line_wire()
    data = render_receipt(first, today=TODAY)
    assert render_receipt(second, today=TODAY) == data
    # The entry's first row in write order (a frozen clock leaves the id to decide).
    opening = entry_of(first)[0]
    assert entry_of(second) == entry_of(first) and {row.pk for row in entry_of(first)} == {
        first.pk,
        second.pk,
    }
    assert receipt_number(first) == receipt_number(second) == f"RCPT-{str(opening.pk)[:8].upper()}"
    assert receipt_filename(first) == receipt_filename(second)
    assert facts(first, TODAY).amount == facts(second, TODAY).amount == D("2800.00")
    printed = lines_of(data)
    assert printed.count(receipt_number(first)) == 1
    applied = printed.index("APPLIED TO")
    assert printed[applied + 1 : applied + 6] == [
        f"Invoice {number}",
        "Rent — December 2026",
        "$2,000.00",
        "Access fee",
        "$800.00",
    ]
    assert [line for line in printed if line.startswith("Invoice ")] == [f"Invoice {number}"]
    assert printed[printed.index("Remaining balance on the store") + 1] == "$0.00"


def test_a_payment_alone_keeps_its_own_receipt() -> None:
    company()
    _, charges = builder_invoice(billed_store(), TWO)
    alone = settle(charges[1], "800.00", PAID_ON, PaymentMethod.ZELLE)
    assert entry_of(alone) == [alone]
    assert receipt_number(alone) == f"RCPT-{str(alone.pk)[:8].upper()}"
    assert facts(alone, TODAY).amount == D("800.00")
    grouped = settle(
        charges[0], "2000.00", PAID_ON, PaymentMethod.ZELLE, allocation_group=uuid.uuid4()
    )
    assert entry_of(grouped) == [grouped]
    assert receipt_number(grouped) == f"RCPT-{str(grouped.pk)[:8].upper()}"
    printed = lines_of(render_receipt(grouped, today=TODAY))
    applied = printed.index("APPLIED TO")
    assert printed[applied + 2 : applied + 4] == ["Rent — December 2026", "$2,000.00"]
    assert "Access fee" not in printed


def test_a_credit_applied_in_the_entry_is_not_money_received() -> None:
    from ledger.services import credits

    company()
    lease = billed_store()
    _, charges = builder_invoice(lease, TWO)
    group = uuid.uuid4()
    held = credits.create(
        lease,
        D("800.00"),
        method=PaymentMethod.WIRE,
        received_on=date(2026, 11, 1),
        reference="",
        note="Overpaid in October",
        allocation_group=uuid.uuid4(),
        actor=None,
    )
    wire = settle(charges[0], "2000.00", PAID_ON, PaymentMethod.WIRE, allocation_group=group)
    credit = settle(
        charges[1], "800.00", PAID_ON, PaymentMethod.CREDIT, allocation_group=group, credit=held
    )
    assert entry_of(wire) == entry_of(credit) == [wire]
    assert facts(credit, TODAY).amount == D("2000.00")
    assert render_receipt(credit, today=TODAY) == render_receipt(wire, today=TODAY)


def test_a_deposit_split_over_two_stores_gives_each_store_its_own_receipt() -> None:
    """banking.services gives the stores of one split deposit one group: the lease bounds the
    entry, so neither receipt names the other store's charge."""
    company()
    lease = billed_store()
    assert lease.operator is not None
    other = a_lease(a_store("Paterson", 1992), lease.operator)
    here = charge(lease, "500.00", "Access fee")
    there = charge(other, "300.00", "Sign")
    group = uuid.uuid4()
    first = settle(here, "500.00", PAID_ON, PaymentMethod.WIRE, allocation_group=group)
    second = settle(there, "300.00", PAID_ON, PaymentMethod.WIRE, allocation_group=group)
    assert entry_of(first) == [first] and entry_of(second) == [second]
    assert receipt_number(first) != receipt_number(second)
    printed = lines_of(render_receipt(first, today=TODAY))
    assert "Access fee" in printed and "Sign" not in printed
    assert facts(first, TODAY).amount == D("500.00")


# --- The e-mail ---------------------------------------------------------------------------------


def test_a_two_line_wire_recorded_in_the_builder_queues_one_receipt_and_sends_it_once(
    fake: FakeProvider, memory: MemoryMailSender
) -> None:
    for code in merge.PDF_CODES:
        seeded(code)
    company(support_email="help@example.com")
    operator, lease = a_world(fake)
    LeaseBilling.objects.update_or_create(lease=lease, defaults={"email": "ops@example.com"})
    Store.objects.filter(pk=lease.store_id).update(store_number=1991)
    sign()
    draft = two_line_draft(operator, lease)
    outcome = invoice_builder.collect(
        draft.pk,
        invoice_builder.CollectChoice.RECORD,
        actor=client_for(Role.BILLING)[1],
        today=date(2026, 10, 15),
        payment=invoice_builder.PaymentInput(
            method="wire", paid_on=date(2026, 10, 15), reference="WIRE-1001", note="Wire received."
        ),
    )
    assert outcome.ok, outcome.message
    assert outcome.payment is not None
    rows = list(
        Payment.objects.filter(allocation_group=outcome.payment.allocation_group).order_by(
            "created_at", "pk"
        )
    )
    assert len(rows) == 2
    [message] = OutboundMessage.objects.filter(kind=MessageKind.RECEIPT)
    assert message.charge_entity_id == rows[0].pk
    assert message.rendered_subject == "Receipt for $175.00"
    # A later row of the entry, or the first again, queues nothing.
    assert invoice_messages.queue_receipt(rows[1]) is None
    assert invoice_messages.queue_receipt(rows[0]) is None
    assert OutboundMessage.objects.filter(kind=MessageKind.RECEIPT).count() == 1
    run_queued_jobs()
    assert (fresh_status(message), memory.sent != []) == ("sent", True)
    [delivered] = memory.sent
    [attachment] = delivered.attachments
    assert attachment.filename == receipt_filename(rows[1])
    assert attachment.data == render_receipt(rows[1], today=timezone.localdate())


def test_the_per_row_hook_leaves_an_entry_s_row_to_its_writer() -> None:
    company()
    _, charges = builder_invoice(billed_store(), TWO)
    row = settle(charges[0], "2000.00", PAID_ON, PaymentMethod.WIRE, allocation_group=uuid.uuid4())
    actor = client_for(Role.BILLING)[1]
    assert invoice_messages.queue_receipt_if_due(row, actor) is None
    assert not OutboundMessage.objects.filter(kind=MessageKind.RECEIPT).exists()


# --- The lists ----------------------------------------------------------------------------------


def test_the_store_payments_tab_links_the_entry_s_receipt_once() -> None:
    _, rows, lease = a_two_line_wire()
    viewer = client_for(Role.VIEWER)[0]
    page = viewer.get(f"/stores/{lease.store_id}/?tab=payments").content.decode()
    links = [row.pk for row in rows if f'href="/payments/{row.pk}/receipt.pdf"' in page]
    assert len(links) == 1
    assert page.count(">Receipt PDF</a>") == 1


def test_the_portal_links_the_entry_s_receipt_once_and_every_row_s_id_stays_isolated(
    world: World,
) -> None:
    group = uuid.uuid4()
    october = a_rent_charge(world.lease_a, date(2026, 10, 1), due="1850.00")
    rows: list[Any] = [
        settle_rent(world.rent_a, "1850.00", group),
        settle_rent(october, "1850.00", group),
    ]
    client = portal_client(active_user(world.a))
    page = client.get(f"/portal/stores/{world.store_a.pk}/payments/").content.decode()
    shown = [row.pk for row in rows if f'href="/portal/payments/{row.pk}/receipt.pdf"' in page]
    assert len(shown) == 1
    downloads = [client.get(f"/portal/payments/{row.pk}/receipt.pdf") for row in rows]
    assert [response.status_code for response in downloads] == [200, 200]
    assert downloads[0].content == downloads[1].content
    other = portal_client(active_user(world.b))
    for row in rows:
        assert other.get(f"/portal/payments/{row.pk}/receipt.pdf").status_code == 404


def settle_rent(rent: Any, amount: str, group: uuid.UUID) -> Payment:
    from tests.receivables_helpers import a_payment

    payment = a_payment(rent, amount, date(2026, 9, 20))
    Payment.objects.filter(pk=payment.pk).update(
        allocation_group=group, method=PaymentMethod.WIRE, note="Received by hand"
    )
    return Payment.objects.get(pk=payment.pk)


def fresh_status(message: OutboundMessage) -> str:
    found = OutboundMessage.objects.get(pk=message.pk)
    return f"{found.status}{' ' + found.held_reason if found.held_reason else ''}"

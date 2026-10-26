"""Row-set parity of the invoice builder's "How to collect" (docs/qa/PLAN-012e2-fewer-actions.md
D2; §6 A; Batch A step 1).

Each test drives one path that a separate form took before Batch A and records the rows
it wrote as a plain structure (:func:`rows_since`): the one-off charges (category,
collection, price, status, due date, description, lease, operator, fee type), the
invoices and their lines, the external references' kinds, the audit actions in order
with their ``after`` keys (the draft's own rows apart, under ``draft_audit``), the
payments and their allocation groups, and the queued jobs (the schedule rows of a
debit on a date). The literal expectations below were recorded from the code before
the three forms were removed; a path that moved to
:func:`ledger.services.invoice_builder.collect` asserts the same literal through it,
and where the builder cannot write the same rows the difference is stated next to the
literal.

Identifiers are named, never compared: ``lease``, ``operator``, ``charge-1`` ... (in the
order of the invoice's lines, else by due date and description), ``invoice-1``,
``draft``. Nothing reaches a network.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from django.test import override_settings
from django.urls import reverse

from accounts.roles import Role
from ledger.models import (
    AuditLog,
    FeeType,
    Invoice,
    InvoiceDraft,
    InvoiceLineItem,
    Lease,
    OneOffCharge,
    Operator,
    Payment,
)
from ledger.services import invoice_builder
from ledger.services.invoice_builder import LineInput
from payments.fake import FakeProvider
from payments.models import ExternalRef, Job
from tests.test_charge_now import ready, store_methods
from tests.test_lease_services import make_lease
from tests.test_one_off_charges import an_operator
from tests.test_operator_views import client_for
from tests.test_rent_run import health  # noqa: F401 (autouse fixture)

pytestmark = pytest.mark.django_db

D = Decimal
NOW = datetime(2026, 10, 6, 16, 0, tzinfo=UTC)
TODAY = date(2026, 10, 6)
SIGN_CODE = "sign-parity"


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("django.utils.timezone.now", lambda: NOW)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider(pay_invoice_enabled=True)
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


@pytest.fixture
def enabled() -> Iterator[None]:
    with override_settings(STAX_CALLER="worker", STAX_PAY_INVOICE_ENABLED=True):
        yield


# --- The recorded structure ---------------------------------------------------------------------


def baseline() -> dict[str, Any]:
    """What exists before the path runs; :func:`rows_since` reports only what it adds."""
    return {
        "charges": set(OneOffCharge.objects.values_list("pk", flat=True)),
        "invoices": set(Invoice.objects.values_list("pk", flat=True)),
        "refs": set(ExternalRef.objects.values_list("pk", flat=True)),
        "audit": AuditLog.objects.order_by("-id").values_list("id", flat=True).first() or 0,
        "payments": set(Payment.objects.values_list("pk", flat=True)),
        "jobs": set(Job.objects.values_list("pk", flat=True)),
    }


def _names(base: dict[str, Any], lease: Lease, operator: Operator) -> dict[str, str]:
    names = {str(lease.pk): "lease", str(operator.pk): "operator"}
    invoices = list(Invoice.objects.exclude(pk__in=base["invoices"]).order_by("invoice_number"))
    for number, invoice in enumerate(invoices, start=1):
        names[str(invoice.pk)] = f"invoice-{number}"
    ordered: list[str] = []
    for invoice in invoices:
        for line in InvoiceLineItem.objects.filter(invoice=invoice).order_by("position"):
            if line.one_off_charge_id is not None and str(line.one_off_charge_id) not in ordered:
                ordered.append(str(line.one_off_charge_id))
        if invoice.one_off_charge_id is not None and str(invoice.one_off_charge_id) not in ordered:
            ordered.insert(0, str(invoice.one_off_charge_id))
    rest = OneOffCharge.objects.exclude(pk__in=base["charges"]).order_by("due_date", "description")
    for charge in rest:
        if str(charge.pk) not in ordered:
            ordered.append(str(charge.pk))
    for charge in OneOffCharge.objects.filter(pk__in=base["charges"]).order_by(
        "due_date", "description"
    ):
        if str(charge.pk) not in ordered:
            ordered.append(str(charge.pk))
    for number, key in enumerate(ordered, start=1):
        names[key] = f"charge-{number}"
    for draft in InvoiceDraft.objects.all():
        names[str(draft.pk)] = "draft"
    return names


def _name(names: dict[str, str], value: object) -> str | None:
    return names.get(str(value), "other") if value is not None else None


def rows_since(base: dict[str, Any], lease: Lease, operator: Operator) -> dict[str, Any]:
    """The rows the path wrote, as a plain structure (see the module docstring)."""
    names = _names(base, lease, operator)
    charges = sorted(
        OneOffCharge.objects.exclude(pk__in=base["charges"]).select_related("fee_type"),
        key=lambda row: names[str(row.pk)],
    )
    invoices = Invoice.objects.exclude(pk__in=base["invoices"]).order_by("invoice_number")
    audit = AuditLog.objects.filter(id__gt=base["audit"]).order_by("id")
    payments = Payment.objects.exclude(pk__in=base["payments"]).order_by("created_at", "id")
    return {
        "charges": [
            {
                "name": names[str(charge.pk)],
                "category": charge.category,
                "collection": charge.collection,
                "price": str(charge.price),
                "status": charge.status,
                "due_date": charge.due_date.isoformat(),
                "description": charge.description,
                "lease": _name(names, charge.lease_id),
                "operator": _name(names, charge.operator_id),
                "fee_type": charge.fee_type.code if charge.fee_type is not None else None,
            }
            for charge in charges
        ],
        "invoices": [
            {
                "name": names[str(invoice.pk)],
                "charge": _name(names, invoice.one_off_charge_id),
                "total": str(invoice.total),
                "status": invoice.internal_status,
                "due_date": invoice.due_date.isoformat(),
                "lines": [
                    (
                        line.position,
                        line.description,
                        line.details,
                        str(line.quantity),
                        str(line.unit_price),
                        str(line.line_total),
                        _name(names, line.one_off_charge_id),
                    )
                    for line in InvoiceLineItem.objects.filter(invoice=invoice).order_by("position")
                ],
            }
            for invoice in invoices
        ],
        "refs": sorted(
            (ref.entity_type, ref.external_kind)
            for ref in ExternalRef.objects.exclude(pk__in=base["refs"])
        ),
        "audit": [
            (row.action, _name(names, row.entity_id), sorted(row.after or {}))
            for row in audit
            if row.entity_type != invoice_builder.ENTITY_DRAFT
        ],
        "draft_audit": [
            (row.action, sorted(row.after or {}))
            for row in audit
            if row.entity_type == invoice_builder.ENTITY_DRAFT
        ],
        # By charge name: payments written in one moment have no other stable order.
        "payments": sorted(
            (
                _name(names, payment.one_off_charge_id) or "",
                payment.kind,
                payment.method,
                str(payment.amount),
                payment.status,
                payment.reference,
            )
            for payment in payments
        ),
        "allocation_groups": len(
            {payment.allocation_group for payment in payments if payment.allocation_group}
        ),
        "jobs": sorted(
            (job.kind, _name(names, job.entity_id), sorted(job.params or {}))
            for job in Job.objects.exclude(pk__in=base["jobs"])
        ),
    }


def a_world(fake: FakeProvider) -> tuple[Operator, Lease]:
    operator = an_operator(fake)
    return operator, make_lease(operator=operator)


def sign() -> FeeType:
    return FeeType.objects.create(
        code=SIGN_CODE, name="Sign", active=True, show_in_setup=True, default_price=D("75.00")
    )


def user(role: Role) -> Any:
    return client_for(role)[1]


def two_line_draft(operator: Operator, lease: Lease) -> InvoiceDraft:
    outcome = invoice_builder.create_draft(
        operator,
        lease,
        "Works of October.",
        date(2026, 10, 20),
        [
            LineInput("Sign repair", "North wall", D("2"), D("50.00")),
            LineInput("Sign", "", None, D("75.00")),
        ],
        user(Role.BILLING),
        today=TODAY,
    )
    assert outcome.ok and outcome.draft is not None, outcome.message
    return outcome.draft


# --- The expectations, recorded from the code before Batch A --------------------------------------

#: The ``after`` keys of ``one_off_charge.created``: the charge's state, then what the
#: creation path adds.
CHARGE_STATE_KEYS = [
    "amount_paid",
    "category",
    "collection",
    "cost",
    "description",
    "due_date",
    "fee_type",
    "lease",
    "needs_review",
    "operator",
    "price",
    "status",
    "vendor",
]
CREATED_KEYS = sorted([*CHARGE_STATE_KEYS, "creation_key", "invoice_number", "note"])
LINE_CREATED_KEYS = sorted([*CHARGE_STATE_KEYS, "invoice_number", "note"])

#: ``ledger:leases:add_setup_charge`` before Batch A: one setup charge, collected later
#: (collection ``none``), no invoice, no job.
SETUP_FORM = {
    "charges": [
        {
            "name": "charge-1",
            "category": "setup",
            "collection": "none",
            "price": "75.00",
            "status": "owed",
            "due_date": "2026-10-20",
            "description": "Sign",
            "lease": "lease",
            "operator": "operator",
            "fee_type": SIGN_CODE,
        }
    ],
    "invoices": [],
    "refs": [],
    "audit": [
        (
            "one_off_charge.created",
            "charge-1",
            CHARGE_STATE_KEYS,
        )
    ],
    "draft_audit": [],
    "payments": [],
    "allocation_groups": 0,
    "jobs": [],
}

#: ``ledger:stores:schedule_payment`` before Batch A: one charge collected by autopay on
#: its due date, no invoice, the ``schedule_create`` job.
SCHEDULE_FORM = {
    "charges": [
        {
            "name": "charge-1",
            "category": "other",
            "collection": "autopay_due",
            "price": "150.00",
            "status": "owed",
            "due_date": "2026-10-20",
            "description": "Sign repair: North wall",
            "lease": "lease",
            "operator": "operator",
            "fee_type": None,
        }
    ],
    "invoices": [],
    "refs": [],
    "audit": [("one_off_charge.created", "charge-1", CREATED_KEYS)],
    "draft_audit": [],
    "payments": [],
    "allocation_groups": 0,
    "jobs": [("schedule_create", "charge-1", [])],
}

#: A charge's Charge now confirmation (reached from ``ledger:stores:charge_now_pick``
#: before Batch A, from the charge's page after): the request audited, ``invoice_pay``
#: queued with the confirmation.
CHARGE_NOW_CONFIRM = {
    "charges": [],
    "invoices": [],
    "refs": [],
    "audit": [
        (
            "one_off_charge.charge_now_requested",
            "charge-1",
            ["amount", "invoice_number", "large_debit_confirmed"],
        ),
        # PLAN-012f-b Batch C: the confirmation page runs through debit(), which keeps its
        # own row (the Add payment path's key and note) beside the charge-now one; the
        # builder's Charge now has no such row.
        (
            "one_off_charge.debit_requested",
            "charge-1",
            ["amount", "collection", "debit_key", "note"],
        ),
    ],
    "draft_audit": [],
    "payments": [],
    "allocation_groups": 0,
    "jobs": [("invoice_pay", "charge-1", ["confirmed"])],
}

#: ``invoice_draft.sent``'s ``after`` keys as recorded before Batch A; Batch A adds the
#: choice (D2).
SENT_KEYS_BEFORE = [
    "charges",
    "collection",
    "due_date",
    "invoice_number",
    "lease",
    "lines",
    "memo",
    "operator",
    "rent_charges",
    "status",
    "total",
]
SENT_KEYS = sorted([*SENT_KEYS_BEFORE, "choice"])
DRAFT_KEYS = ["creation_key", "due_date", "lease", "lines", "memo", "operator", "status", "total"]
PAYMENT_KEYS = sorted(
    [*CHARGE_STATE_KEYS, "amount", "creation_key", "method", "paid_on", "payment"]
)


def _builder_lines(collection: str) -> list[dict[str, Any]]:
    return [
        {
            "name": "charge-1",
            "category": "other",
            "collection": collection,
            "price": "100.00",
            "status": "owed",
            "due_date": "2026-10-20",
            "description": "Sign repair (2 × $50.00)",
            "lease": "lease",
            "operator": "operator",
            "fee_type": None,
        },
        {
            "name": "charge-2",
            "category": "setup",
            "collection": collection,
            "price": "75.00",
            "status": "owed",
            "due_date": "2026-10-20",
            "description": "Sign",
            "lease": "lease",
            "operator": "operator",
            "fee_type": SIGN_CODE,
        },
    ]


BUILDER_INVOICE = {
    "name": "invoice-1",
    "charge": "charge-1",
    "total": "175.00",
    "status": "draft",
    "due_date": "2026-10-20",
    "lines": [
        (1, "Sign repair", "North wall", "2.0000", "50.00", "100.00", "charge-1"),
        (2, "Sign", "", "1.0000", "75.00", "75.00", "charge-2"),
    ],
}

#: The builder's Send (``invoice_builder.send``) before Batch A.
BUILDER_SEND = {
    "charges": _builder_lines("email"),
    "invoices": [BUILDER_INVOICE],
    "refs": [],
    "audit": [
        ("one_off_charge.created", "charge-1", LINE_CREATED_KEYS),
        ("one_off_charge.created", "charge-2", LINE_CREATED_KEYS),
    ],
    "draft_audit": [("invoice_draft.sent", SENT_KEYS)],
    "payments": [],
    "allocation_groups": 0,
    "jobs": [("invoice_create", "charge-1", [])],
}

#: The builder's Charge now (``invoice_builder.charge_now``) before Batch A.
BUILDER_CHARGE_NOW = {
    "charges": _builder_lines("charge_now"),
    "invoices": [BUILDER_INVOICE],
    "refs": [],
    "audit": [
        ("one_off_charge.created", "charge-1", LINE_CREATED_KEYS),
        ("one_off_charge.created", "charge-2", LINE_CREATED_KEYS),
        (
            "one_off_charge.charge_now_requested",
            "charge-1",
            ["amount", "invoice_number", "large_debit_confirmed"],
        ),
    ],
    "draft_audit": [("invoice_draft.sent", SENT_KEYS)],
    "payments": [],
    "allocation_groups": 0,
    "jobs": [("invoice_create", "charge-1", ["charge_now", "confirmed"])],
}


# --- Through How to collect (after Batch A) -------------------------------------------------------

#: The setup item through the builder: ``?collect=draft&item=`` saves a draft (no charge,
#: as the setup form's charge was collected by nothing); sent by e-mail it is the setup
#: charge of :data:`SETUP_FORM` but collected by e-mail, with its invoice, the line and the
#: ``invoice_create`` job. The differences from :data:`SETUP_FORM`: ``collection`` ``none``
#: becomes ``email``; the invoice, its line and the job are added; the audit row's
#: ``after`` gains ``invoice_number`` and ``note`` (the builder's line note).
SETUP_THROUGH_BUILDER = {
    **SETUP_FORM,
    "charges": [{**SETUP_FORM["charges"][0], "collection": "email"}],  # type: ignore[index]
    "invoices": [
        {
            "name": "invoice-1",
            "charge": "charge-1",
            "total": "75.00",
            "status": "draft",
            "due_date": "2026-10-20",
            "lines": [(1, "Sign", "", "1.0000", "75.00", "75.00", "charge-1")],
        }
    ],
    "audit": [("one_off_charge.created", "charge-1", LINE_CREATED_KEYS)],
    "draft_audit": [("invoice_draft.created", DRAFT_KEYS), ("invoice_draft.sent", SENT_KEYS)],
    "jobs": [("invoice_create", "charge-1", [])],
}

#: Record payment now (new in Batch A): the builder's lines as :data:`BUILDER_SEND` with
#: collection ``manual``, one settled payment per line in line order at its line total
#: with one allocation group, and the create job alone: its success queues the mark
#: (``payments.jobs.runners``), never the builder.
BUILDER_RECORD = {
    "charges": [{**row, "status": "paid"} for row in _builder_lines("manual")],
    "invoices": [BUILDER_INVOICE],
    "refs": [],
    "audit": [
        ("one_off_charge.created", "charge-1", LINE_CREATED_KEYS),
        ("one_off_charge.created", "charge-2", LINE_CREATED_KEYS),
        ("one_off_charge.payment_recorded", "charge-1", PAYMENT_KEYS),
        ("one_off_charge.payment_recorded", "charge-2", PAYMENT_KEYS),
    ],
    "draft_audit": [("invoice_draft.sent", SENT_KEYS)],
    "payments": [
        ("charge-1", "one_off", "wire", "100.00", "settled", "WIRE-1001"),
        ("charge-2", "one_off", "wire", "75.00", "settled", "WIRE-1001"),
    ],
    "allocation_groups": 1,
    "jobs": [("invoice_create", "charge-1", [])],
}


def builder_data(lines: list[dict[str, str]], **fields: str) -> dict[str, str]:
    """The New invoice page's form: the header, the lines and How to collect."""
    data = {
        "creation_key": "00000000-0000-0000-0000-000000000002",
        "memo": "",
        "due_date": "2026-10-20",
        "lines-TOTAL_FORMS": str(len(lines)),
        "lines-INITIAL_FORMS": "0",
        "lines-MIN_NUM_FORMS": "0",
        "lines-MAX_NUM_FORMS": str(invoice_builder.MAX_LINES),
        **fields,
    }
    for index, row in enumerate(lines):
        data.update({f"lines-{index}-{name}": value for name, value in row.items()})
    return data


# --- The paths ------------------------------------------------------------------------------------


def test_a_setup_charge_through_the_builder(fake: FakeProvider) -> None:
    """``ledger:leases:add_setup_charge`` (the lease form before Batch A) now opens the
    builder with Save draft chosen and the setup item on line 1."""
    operator, lease = a_world(fake)
    sign()
    billing = client_for(Role.BILLING)[0]
    base = baseline()
    response = billing.get(reverse("ledger:leases:add_setup_charge", kwargs={"pk": lease.pk}))
    assert response.status_code == 302
    builder = response["Location"]
    assert builder == (
        reverse("ledger:invoices:new_store", kwargs={"pk": lease.store_id})
        + f"?store={lease.store_id}&lease={lease.pk}&collect=draft&item={SIGN_CODE}"
    )
    page = billing.get(builder).content.decode()
    assert 'value="Sign"' in page and 'id="collect-draft" checked' in page
    saved = billing.post(
        builder,
        builder_data(
            [{"item": "Sign", "unit_amount": "75.00"}],
            lease=str(lease.pk),
            collect="draft",
        ),
    )
    draft = InvoiceDraft.objects.get()
    assert saved["Location"].startswith(f"/invoices/drafts/{draft.pk}/")
    assert rows_since(base, lease, operator) == {
        **SETUP_FORM,
        "charges": [],
        "audit": [],
        "draft_audit": [("invoice_draft.created", DRAFT_KEYS)],
    }
    outcome = invoice_builder.collect(
        draft.pk, invoice_builder.CollectChoice.EMAIL, actor=user(Role.BILLING), today=TODAY
    )
    assert outcome.ok, outcome.message
    assert rows_since(base, lease, operator) == SETUP_THROUGH_BUILDER


def test_a_scheduled_payment_through_the_builder(fake: FakeProvider) -> None:
    """``ledger:stores:schedule_payment`` (the store form before Batch A) now opens the
    builder with Schedule for a date chosen; the rows are the form's, the draft's own
    rows apart."""
    operator, lease = a_world(fake)
    billing = client_for(Role.BILLING)[0]
    base = baseline()
    response = billing.get(reverse("ledger:stores:schedule_payment", kwargs={"pk": lease.store_id}))
    builder = response["Location"]
    assert "collect=schedule" in builder
    posted = billing.post(
        builder,
        builder_data(
            [{"item": "Sign repair", "description": "North wall", "unit_amount": "150.00"}],
            lease=str(lease.pk),
            collect="schedule",
            schedule_on="2026-10-20",
        ),
    )
    assert posted.status_code == 302
    rows = rows_since(base, lease, operator)
    assert {**rows, "draft_audit": []} == SCHEDULE_FORM
    assert rows["draft_audit"] == [
        ("invoice_draft.created", DRAFT_KEYS),
        ("invoice_draft.sent", SENT_KEYS),
    ]
    assert not InvoiceDraft.objects.exists()  # the draft became the charge


def test_a_charges_charge_now_confirmation(fake: FakeProvider, enabled: None) -> None:
    """The store page's Charge now list (before Batch A) linked each charge to its
    confirmation page, which stays on the charge's page; the list now opens the builder
    with Charge now chosen."""
    charge = ready(fake, lease=True)
    lease, operator = charge.lease, charge.operator
    assert lease is not None and operator is not None
    admin = client_for(Role.ADMIN)[0]
    pick = admin.get(reverse("ledger:stores:charge_now_pick", kwargs={"pk": lease.store_id}))
    assert pick.status_code == 302 and "collect=charge_now" in pick["Location"]
    detail = admin.get(reverse("ledger:one_off_charges:detail", kwargs={"pk": charge.pk}))
    assert reverse("ledger:one_off_charges:charge_now", kwargs={"pk": charge.pk}) in (
        detail.content.decode()
    )
    base = baseline()
    response = admin.post(reverse("ledger:one_off_charges:charge_now", kwargs={"pk": charge.pk}))
    assert response.status_code == 302
    assert rows_since(base, lease, operator) == CHARGE_NOW_CONFIRM


def test_the_builders_send(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    sign()
    draft = two_line_draft(operator, lease)
    base = baseline()
    outcome = invoice_builder.collect(
        draft.pk, invoice_builder.CollectChoice.EMAIL, actor=user(Role.BILLING), today=TODAY
    )
    assert outcome.ok, outcome.message
    assert rows_since(base, lease, operator) == BUILDER_SEND
    sent = AuditLog.objects.get(action=invoice_builder.SENT_ACTION)
    assert sent.after is not None and sent.after["choice"] == "email"


def test_the_builders_send_wrapper_writes_the_same_rows(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    sign()
    draft = two_line_draft(operator, lease)
    base = baseline()
    outcome = invoice_builder.send(draft.pk, user(Role.BILLING), today=TODAY)
    assert outcome.ok, outcome.message
    assert rows_since(base, lease, operator) == BUILDER_SEND


def test_the_builders_charge_now(fake: FakeProvider, enabled: None) -> None:
    operator, lease = a_world(fake)
    store_methods(operator)
    sign()
    draft = two_line_draft(operator, lease)
    base = baseline()
    outcome = invoice_builder.collect(
        draft.pk,
        invoice_builder.CollectChoice.CHARGE_NOW,
        actor=user(Role.ADMIN),
        today=TODAY,
        confirmed=True,
    )
    assert outcome.ok, outcome.message
    assert rows_since(base, lease, operator) == BUILDER_CHARGE_NOW


def test_the_builders_charge_now_wrapper_writes_the_same_rows(
    fake: FakeProvider, enabled: None
) -> None:
    operator, lease = a_world(fake)
    store_methods(operator)
    sign()
    draft = two_line_draft(operator, lease)
    base = baseline()
    outcome = invoice_builder.charge_now(draft.pk, user(Role.ADMIN), confirmed=True, today=TODAY)
    assert outcome.ok, outcome.message
    assert rows_since(base, lease, operator) == BUILDER_CHARGE_NOW


def test_the_builders_record(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    sign()
    draft = two_line_draft(operator, lease)
    base = baseline()
    outcome = invoice_builder.collect(
        draft.pk,
        invoice_builder.CollectChoice.RECORD,
        actor=user(Role.BILLING),
        today=TODAY,
        payment=invoice_builder.PaymentInput(
            method="wire", paid_on=TODAY, reference="WIRE-1001", note="Wire received."
        ),
    )
    assert outcome.ok, outcome.message
    assert rows_since(base, lease, operator) == BUILDER_RECORD

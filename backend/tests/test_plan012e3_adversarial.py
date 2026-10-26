"""Adversarial cases for PLAN-012e-3 ("Check" as a manual payment method; the builder's lines
table in a scroll region), by the QA owner (plan §6). A check number that is a card number or
a routing-like run is refused with nothing written; a check number with markup is escaped on
the payment page and the ledger; the builder's record path writes the same rows for a check as
for a Zelle payment; a method outside the manual set is refused everywhere; the lines table sits
inside the scroll region on the new and the draft pages."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.urls import reverse

from accounts.roles import Role
from ledger.models import AuditLog, Lease, LeaseCredit, OneOffCharge, Payment, PaymentMethod
from ledger.services import invoice_builder
from ledger.services.invoice_builder import PaymentInput
from payments.fake import FakeProvider
from tests import test_invoice_builder as builder_tests
from tests import test_plan012d_b_adversarial as payment_tests
from tests.test_invoice_builder import TODAY, a_draft, a_world, line, user
from tests.test_lease_services import make_lease
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

MARKUP = '<script>alert("x")</script>'
LUHN_CARD = "4111111111111111"
ROUTING_LIKE = "021000021 123456789012"
# The builder file's fixtures, re-exported so pytest finds them here.
fixed_now = builder_tests.fixed_now
fake = builder_tests.fake
worker = builder_tests.worker


def _snapshot() -> tuple[int, int, int]:
    return (Payment.objects.count(), LeaseCredit.objects.count(), AuditLog.objects.count())


# --- the check number is guarded like every reference --------------------------------------


@pytest.mark.parametrize("reference", [LUHN_CARD, f"check {LUHN_CARD}", ROUTING_LIKE])
def test_a_check_number_that_looks_like_an_account_is_refused_with_nothing_written(
    reference: str,
) -> None:
    lease = payment_tests._lease()  # noqa: SLF001
    rent, _ = payment_tests._charges(lease)  # noqa: SLF001
    billing, _ = client_for(Role.BILLING)
    before = _snapshot()
    response = billing.post(
        payment_tests._url(lease),  # noqa: SLF001
        payment_tests._post("1000.00", {rent.pk: "1000.00"}, method="check", reference=reference),  # noqa: SLF001
    )
    assert response.status_code in (200, 302), reference
    assert _snapshot() == before, reference
    # Nothing is stored: the ledger and the payment list never show the number (the held
    # form shows the typist their own text back, which is not storage).
    for path in (
        reverse("ledger:payments:list"),
        reverse("ledger:stores:detail", kwargs={"pk": lease.store_id}),
    ):
        shown = billing.get(path).content.decode()
        assert LUHN_CARD not in shown and "021000021" not in shown, path


def test_a_check_number_with_markup_is_escaped_on_the_payment_page_and_the_ledger() -> None:
    lease = payment_tests._lease()  # noqa: SLF001
    rent, _ = payment_tests._charges(lease)  # noqa: SLF001
    billing, _ = client_for(Role.BILLING)
    response = billing.post(
        payment_tests._url(lease),  # noqa: SLF001
        payment_tests._post(  # noqa: SLF001
            "1000.00", {rent.pk: "1000.00"}, method="check", reference=f"1042 {MARKUP}"
        ),
    )
    assert response.status_code in (200, 302)
    payment = Payment.objects.first()
    if payment is None:
        # Refused by the text guard: nothing written and the held form carries no raw markup.
        held = billing.get(payment_tests._url(lease)).content.decode()  # noqa: SLF001
        assert MARKUP not in held
        return
    assert payment.method == PaymentMethod.CHECK
    for path in (
        reverse("ledger:payments:detail", kwargs={"pk": payment.pk}),
        reverse("ledger:payments:list"),
        reverse("ledger:operators:statement", kwargs={"pk": lease.operator_id}),
    ):
        body = billing.get(path).content.decode()
        assert MARKUP not in body, path
        assert "Check" in body, path


# --- the builder's record path treats a check like any manual payment --------------------


def _rows(lease: Lease) -> list[tuple[str, str, str]]:
    payments = Payment.objects.filter(one_off_charge__lease=lease)
    charges = OneOffCharge.objects.filter(lease=lease)
    return sorted((str(p.method), str(p.amount), str(p.status)) for p in payments) + sorted(
        (str(c.collection), str(c.price), str(c.status)) for c in charges
    )


def test_the_record_path_writes_the_same_rows_for_a_check_as_for_zelle(
    fake: FakeProvider, worker: None
) -> None:
    seen: dict[str, list[tuple[str, str, str]]] = {}
    actions: dict[str, int] = {}
    operator, first_lease = a_world(fake)
    leases = {"zelle": first_lease, "check": make_lease(operator=operator)}
    for method in ("zelle", "check"):
        lease = leases[method]
        before = AuditLog.objects.count()
        draft = a_draft(operator, lease, [line("Sign repair", "100.00"), line("Keys", "20.00")])
        outcome = invoice_builder.collect(
            draft.pk,
            "record",
            actor=user(Role.BILLING),
            today=TODAY,
            payment=PaymentInput(method=method, paid_on=TODAY, reference="1042", note="Paid."),
        )
        assert outcome.ok, (method, outcome.message)
        seen[method] = [(m.replace(method, "M"), a, s) for m, a, s in _rows(lease)]
        actions[method] = AuditLog.objects.count() - before
    assert seen["check"] == seen["zelle"]
    assert actions["check"] == actions["zelle"]
    assert Payment.objects.filter(method=PaymentMethod.CHECK).count() == 2
    assert sum(
        (p.amount for p in Payment.objects.filter(method=PaymentMethod.CHECK)), Decimal("0")
    ) == Decimal("120.00")


# "cash" is a manual method since PLAN-012f-b Batch C.
@pytest.mark.parametrize("method", ["ach", "credit", "card", "CHECK", "check ", MARKUP])
def test_a_method_outside_the_manual_set_is_refused_everywhere(
    fake: FakeProvider, method: str
) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, [line("Sign repair", "100.00")])
    before = _snapshot()
    outcome = invoice_builder.collect(
        draft.pk,
        "record",
        actor=user(Role.BILLING),
        today=TODAY,
        payment=PaymentInput(method=method, paid_on=TODAY, reference="1042", note="Paid."),
    )
    assert not outcome.ok, method
    assert MARKUP not in outcome.message
    assert _snapshot() == before
    billing, _ = client_for(Role.BILLING)
    response = billing.post(
        reverse("ledger:stores:record_payment", kwargs={"pk": lease.store_id}),
        payment_tests._post("100.00", {}, method=method),  # noqa: SLF001
    )
    assert response.status_code in (200, 302)
    assert _snapshot() == before


# --- the lines table sits inside the scroll region ---------------------------------------------


def test_the_lines_table_is_inside_the_scroll_region_on_both_builder_pages(
    fake: FakeProvider,
) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, [line("Sign repair", "100.00")])
    billing, _ = client_for(Role.BILLING)
    for path in (
        reverse("ledger:invoices:new_store", kwargs={"pk": lease.store_id}),
        reverse("ledger:invoices:draft", kwargs={"pk": draft.pk}),
    ):
        body = billing.get(path).content.decode()
        table = (
            body.index('id="invoice-lines"')
            if 'id="invoice-lines"' in body
            else body.index("<table")
        )
        region = body.rfind('class="table-scroll', 0, table)
        assert region != -1, path
        # Nothing closes the region before the table opens: the table is inside it.
        assert "</div>" not in body[region:table], path

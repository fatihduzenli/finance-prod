"""Adversarial cases for PLAN-012c Batch D (the copy pass, D9), by the QA owner (plan §6 D).
The copy change must not touch what is stored or addressed: every stored choice value and every
route name is frozen here as it was before the batch. The money filter never raises and never
prints "$-", "nan" or "inf" whatever it is given. The provider's display name with markup is
escaped in every sentence that now names it. User data that happens to contain a forbidden word
("Legacy", "Sheet", "Webhook") is shown as typed, never rewritten. The sandbox banner carries
the approved wording on every page, the sign-in and the error pages included."""

from __future__ import annotations

import hashlib
import inspect
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from django.db import models
from django.template import engines
from django.test import Client
from django.urls import get_resolver, reverse

import ledger.models as ledger_models
from accounts.roles import Role
from ledger.models import CompanySettings, PaymentChannel
from tests.receivables_helpers import a_lease, a_rent_charge, a_store, an_operator
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

MARKUP = '<script>alert("x")</script>'
BANNER = "SANDBOX — test system, no real money"

#: Every stored choice value of ledger.models before Batch D (2026-10-17). Labels may change;
#: values never.
FROZEN_CHOICES: dict[str, list[str]] = {
    "AdjustmentReason": ["carry_forward", "credit", "cross_store_balance", "discount"],
    # PLAN-012f-a Batch E (ADR-0014): the repeating debit plan's three stored choices.
    "AutopayCadence": ["monthly", "weekly", "yearly"],
    "AutopayEndRule": ["count", "date", "never"],
    "AutopayPlanState": ["active", "finished", "paused", "stopped"],
    "CalendarEntryKind": ["note", "reminder"],
    "ChargeEntityType": ["lease_end", "one_off_charge", "rent_charge"],
    "CollectionMethod": ["autopay_due", "charge_now", "email", "manual", "none"],
    "CollectionStatus": ["active", "ended", "paused", "prospective"],
    "CompanyTimeZone": [
        "America/Anchorage",
        "America/Chicago",
        "America/Denver",
        "America/Los_Angeles",
        "America/New_York",
        "America/Phoenix",
        "Pacific/Honolulu",
    ],
    "ContactKind": ["call", "email", "promise", "visit"],
    "ContactStatus": ["broken", "kept", "open", "rescheduled"],
    "DeductionKind": ["cleaning", "damage", "other", "unpaid_balance"],
    "DepositResolutionKind": ["refunded", "transferred", "withheld"],
    "DisputeEvidenceStatus": ["failed", "queued", "sent"],
    "DocumentContentType": [
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "image/heic",
        "image/jpeg",
        "image/png",
        "image/tiff",
    ],
    "DocumentKind": [
        "amendment",
        "dispute_evidence",
        "final_statement",
        "floor_plan",
        "lease_agreement",
        "notice",
        "other",
        "photo",
    ],
    "DocumentSource": ["docusign", "upload"],
    "EnrolmentStatus": ["enrolled", "not_enrolled"],
    # "yearly" added by PLAN-012f-b Batch D (settlements).
    "InstallmentCadence": ["biweekly", "monthly", "weekly", "yearly"],
    "InstallmentOrigin": ["settlement", "split"],  # PLAN-012f-b Batch D
    "InstallmentCollection": ["autopay_due", "email", "manual"],
    # "paused" added by PLAN-012f-b Batch D (D-3).
    "InstallmentPlanStatus": ["active", "attention", "cancelled", "completed", "paused"],
    "InvoiceDraftStatus": ["cancelled", "draft", "sent"],
    "InvoiceLineKind": ["charge", "discount", "fee", "surcharge", "tax"],
    "InvoiceStatus": ["closed", "draft", "ready", "sent", "void"],
    "LeaseEndReason": ["moved_out", "other", "terminated", "transfer"],
    "LeaseEndStatus": ["confirmed", "draft"],
    "LeaseTransferStatus": ["confirmed", "draft"],
    "LegacyStatus": ["paid", "unpaid"],
    "MessageKind": ["final_statement", "ladder"],
    "MessageStatus": ["catch_all", "failed", "queued", "sent", "suppressed"],
    # "check" added by PLAN-012e-3, "cash" by PLAN-012f-b Batch C.
    "MoneyMethod": ["ach", "cash", "check", "other", "wire", "zelle"],
    "NoteEntityType": ["lease", "operator", "store"],
    # PLAN-012f-b Batch B (D9): the operator's kind.
    "OperatorKind": ["company", "person"],
    "OneOffCategory": [
        "adjustment",
        "arrears",
        "autopay_plan",  # PLAN-012f-a Batch E
        "damage",
        "installment",
        "nsf_fee",
        "other",
        "redebit",
        "setup",
    ],
    "OneOffChargeStatus": ["owed", "paid", "partial", "voided", "waived"],
    "PaymentChannel": ["other", "stax", "unknown", "wire", "zelle"],
    "PaymentKind": [
        "adjustment",
        "credit_refund",
        "insurance",
        "one_off",
        "refund_deposit",
        "rent",
    ],
    # "check" added by PLAN-012e-3 (owner, 2026-10-22): the one deliberate change since the freeze.
    "PaymentMethod": ["ach", "cash", "check", "credit", "other", "wire", "zelle"],
    "PaymentStatus": ["pending", "returned", "settled", "voided"],
    "RefundMethod": ["check", "wire", "zelle"],
    "RentChargeStatus": ["due", "paid", "partial", "pending", "unpaid", "waived"],
    "RentSource": ["sheet_current", "sheet_inferred", "staff"],
    "ResolutionKind": ["note", "rescheduled"],
    "ReviewReason": ["closed", "dormant", "unknown", "wire"],
    "ScheduleKind": ["insurance", "rent"],
    "ScheduleState": ["active", "ended", "failed", "none", "paused"],
    "StoreBalanceKind": ["deposit_or_access", "insurance_opening", "sign"],
    "StoreCostSource": ["import", "staff"],
    "StoreStatus": ["active", "closed", "prospective", "terminated"],
    "SyncFailure": [
        "AlreadyLinkedElsewhere",
        "CircuitOpen",
        "FailureBudgetExhausted",
        "FeatureDisabled",
        "IdentityMismatch",
        "ProviderError",
        "ProviderNotPermittedHere",
        "ProviderRejected",
        "ProviderUnavailable",
        "RateLimitWaitExceeded",
        "TransportError",
        "ValidationFailed",
    ],
    "SyncStatus": ["awaiting_link", "failed", "linked", "not_applicable", "pending"],
}
#: The route names as one digest (sorted, newline-joined, sha256): 255 before Batch D of
#: PLAN-012c; 257 since PLAN-012e-2 Batch C; 261 since PLAN-012f-a Batch E; 262 since
#: PLAN-012f-b Batch A; 268 since Batch B; 272 since Batch D (ledger:invoices:saved and
#: settlement_preview, ledger:plans:pause and resume; checked by hand on 2026-10-25); 273
#: since slice C2 (ledger:charges:charge_now, PLAN-012f-b §10 D19).
FROZEN_ROUTES = (273, "2e31da22621a0ea90de62a340a0dea045eacc87eb2995f49a514fba07e50326f")


# --- what is stored or addressed never changes ------------------------------------------


def test_every_stored_choice_value_is_what_it_was_before_the_copy_pass() -> None:
    found = {
        name: sorted(obj.values)
        for name, obj in vars(ledger_models).items()
        if inspect.isclass(obj)
        and issubclass(obj, models.Choices)
        and obj.__module__ == ledger_models.__name__
    }
    assert found == FROZEN_CHOICES


def _route_names(resolver: Any, prefix: str = "") -> set[str]:
    names: set[str] = set()
    for pattern in resolver.url_patterns:
        if hasattr(pattern, "url_patterns"):
            inner = f"{prefix}{pattern.namespace}:" if pattern.namespace else prefix
            names |= _route_names(pattern, inner)
        elif pattern.name:
            names.add(prefix + pattern.name)
    return names


def test_every_route_name_is_what_it_was_before_the_copy_pass() -> None:
    names = sorted(_route_names(get_resolver()))
    digest = hashlib.sha256("\n".join(names).encode()).hexdigest()
    assert (len(names), digest) == FROZEN_ROUTES, names


# --- the money filter under hostile input ---------------------------------------------------


def _money_filter() -> Any:
    engine: Any = engines["django"].engine  # type: ignore[attr-defined]  # DjangoTemplates
    for library in engine.template_libraries.values():
        if "money" in library.filters:
            return library.filters["money"]
    for library in engine.template_builtins:
        if "money" in library.filters:
            return library.filters["money"]
    pytest.fail("no template filter named money (plan §4.8)")


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "abc",
        "1,000",
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        Decimal("1E+400"),
        Decimal("-0.00"),
        Decimal("-0.001"),
        Decimal("0.005"),
        -0.001,
        10**15,
        float("nan"),
        [],
        {"amount": 1},
    ],
)
def test_the_money_filter_never_raises_and_never_prints_a_broken_amount(value: object) -> None:
    rendered = str(_money_filter()(value))
    lowered = rendered.lower()
    assert "$-" not in rendered, rendered
    assert "nan" not in lowered and "inf" not in lowered, rendered
    assert "-$0.00" != rendered, rendered  # a negative zero is zero
    assert MARKUP not in rendered


def test_the_money_filter_formats_the_plain_cases() -> None:
    money = _money_filter()
    assert str(money(Decimal("2000.00"))) == "$2,000.00"
    assert str(money(Decimal("1234567.891"))) in ("$1,234,567.89", "$1,234,567.891")
    assert str(money(Decimal("-2000"))) == "-$2,000.00"
    assert str(money(0)) == "$0.00"
    assert str(money("2000")) == "$2,000.00"


def test_an_amount_with_markup_is_escaped_by_the_filter() -> None:
    rendered = str(_money_filter()(MARKUP))
    assert "<script" not in rendered


def test_the_grid_cell_and_the_stores_list_show_thousands_separators() -> None:
    lease = a_lease(a_store("2501"), an_operator("Grand Co"), channel=PaymentChannel.WIRE)
    a_rent_charge(lease, date(2026, 9, 1), due="1234567.89")
    viewer, _ = client_for(Role.VIEWER)
    body = viewer.get(reverse("ledger:grid:page")).content.decode()
    assert "$1,234,567.89" in body
    assert "$1234567.89" not in body and "1234567.89" not in body


# --- the provider's name with markup in the reworded sentences -------------------------------


def test_the_provider_name_with_markup_is_escaped_in_every_sentence_that_names_it() -> None:
    row = CompanySettings.current()
    row.processor_name = f"Stax {MARKUP}"
    row.save()
    lease = a_lease(a_store("2502"), an_operator("Named Co"), channel=PaymentChannel.WIRE)
    a_rent_charge(lease, date(2026, 9, 1), due="1000.00")
    admin, _ = client_for(Role.ADMIN)
    for path in (
        "/",
        reverse("ledger:payments:list"),
        reverse("ledger:webhooks:list"),
        reverse("ledger:reconciliation:page"),
        reverse("ledger:returns:list"),
        reverse("ledger:operators:detail", kwargs={"pk": lease.operator_id}),
        reverse("ledger:stores:detail", kwargs={"pk": lease.store_id}),
        reverse("ledger:leases:detail", kwargs={"pk": lease.pk}),
    ):
        response = admin.get(path)
        assert response.status_code == 200, path
        body = response.content.decode()
        assert MARKUP not in body, path


# --- user data is shown as typed, never rewritten by the copy rules -------------------------


def test_user_data_with_forbidden_words_is_shown_as_typed() -> None:
    names = ("Legacy Sheet Store", "Webhook Reconciliation LLC", "Provider Sync Ltd")
    lease = a_lease(a_store("2503"), an_operator(names[1]), channel=PaymentChannel.WIRE)
    second = a_lease(a_store("2504"), an_operator(names[2]), channel=PaymentChannel.WIRE)
    assert lease.operator is not None and second.operator is not None
    lease.store.city = names[0]
    lease.store.save(update_fields=["city"])
    viewer, _ = client_for(Role.VIEWER)
    stores = viewer.get(reverse("ledger:stores:list")).content.decode()
    operators = viewer.get(reverse("ledger:operators:list")).content.decode()
    for name in names:
        assert name in stores or name in operators, name


# --- the sandbox banner on every page -----------------------------------------------------


def test_the_banner_wording_is_on_the_sign_in_the_error_and_the_admin_pages() -> None:
    anonymous = Client()
    for path in ("/account/login/", "/", "/no-such-page/"):
        body = anonymous.get(path).content.decode()
        assert BANNER in body, path
    admin, _ = client_for(Role.ADMIN)
    for path in ("/", "/admin/", reverse("ledger:stores:list")):
        body = admin.get(path).content.decode()
        assert BANNER in body, path
        assert body.count("sandbox-banner") == 1, path  # once, never twice
        assert "<title>[SANDBOX]" in body, path


# --- lazy provider sentences never reach a stored row as proxies ------------------------------


def test_an_insurance_payment_whose_invoice_stays_open_writes_a_plain_text_reason() -> None:
    """The audit row of an insurance payment that Stax still has to mark (slice D2b fix) holds
    the sentence as text: a lazy proxy would raise on JSON and lose the payment's audit."""
    import uuid as _uuid

    from ledger.models import (
        AuditLog,
        InsuranceCharge,
        Invoice,
        InvoiceStatus,
        PaymentMethod,
    )
    from ledger.services import insurance
    from tests.test_lease_end import provider_ref

    row = CompanySettings.current()
    row.processor_name = f"PayCo {MARKUP}"
    row.save()
    lease = a_lease(a_store("2505"), an_operator("Insured Co"), channel=PaymentChannel.WIRE)
    assert lease.operator is not None
    charge = InsuranceCharge.objects.create(
        lease=lease, policy_year=2026, due_on=date(2026, 10, 1), amount=Decimal("120.00")
    )
    invoice = Invoice.objects.create(
        invoice_number="INV-2026-9101",
        operator=lease.operator,
        insurance_charge=charge,
        issue_date=date(2026, 10, 1),
        due_date=date(2026, 10, 1),
        subtotal=Decimal("120.00"),
        total=Decimal("120.00"),
        internal_status=InvoiceStatus.SENT,
    )
    provider_ref("invoice", invoice.pk, "invoice")
    outcome = insurance.record_manual_payment(
        charge.pk,
        method=PaymentMethod.ZELLE,
        amount=Decimal("120.00"),
        paid_on=date(2026, 10, 2),
        note="Zelle received.",
        actor=None,
        creation_key=_uuid.uuid4(),
        today=date(2026, 10, 2),
    )
    assert outcome.ok, outcome.message
    entry = AuditLog.objects.get(action=insurance.NOT_UPDATED_ACTION)
    reason = entry.after["reason"]
    assert type(reason) is str and reason == str(insurance.NOT_MARKED)
    assert "PayCo" in reason and MARKUP in reason  # the name as data in the row, never markup
    # The message shown to staff is escaped on the page, the name resolved at render time.
    assert str(outcome.provider.message) == reason if outcome.provider else True

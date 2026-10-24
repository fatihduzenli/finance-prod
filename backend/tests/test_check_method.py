"""Check as a manual payment method (docs/qa/PLAN-012e3-check-method.md D1 to D5, test plan
§6, Opus's part).

- A check recorded through each manual payment form (the store's Add payment, the operator
  workspace's Add payment on a rent charge, the one-off charge's Add payment, the insurance
  charge through the store's form, the builder's Record payment now) stores
  ``method == "check"`` and shows "Check" on the payment page, the payments ledger, the
  operator's statement and the store's Payments tab.
- The reference field: the label "Reference" without script, the help naming the check
  number, the ``data-label-when`` switch to "Check number" on every manual payment form and
  ``ledger/shown_when.js`` loaded with it.
- The CSV exports: the payments ledger and the statement write the stored method in their
  Method column as they do for the others; Payments by method has its Check column.
- The builder's lines table sits in the ``.table-scroll`` region with its line labels.

The label switch in a browser, and the builder page's width at 1,280 px, are in
``tests/e2e/test_dependent_fields.py``. The adversarial cases are the QA owner's.
"""

from __future__ import annotations

import csv
import io
import re
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from django.test import Client, override_settings
from django.urls import reverse

from accounts.roles import Role
from ledger import forms as ledger_forms
from ledger.models import (
    DepositResolution,
    InvoiceDraft,
    Lease,
    MoneyMethod,
    Operator,
    Payment,
    PaymentChannel,
    PaymentMethod,
)
from ledger.services import invoice_builder, rent_run, reports, workspace
from ledger.services.invoice_builder import LineInput
from payments.fake import FakeProvider
from tests.receivables_helpers import (
    a_lease,
    a_one_off_charge,
    a_rent_charge,
    a_store,
    an_insurance_charge,
    an_operator,
)
from tests.test_operator_views import client_for
from tests.test_rent_run import health  # noqa: F401 (autouse fixture)

pytestmark = pytest.mark.django_db

D = Decimal
TODAY = date(2026, 10, 15)
NOW = datetime(2026, 10, 15, 16, 0, tzinfo=UTC)
CHECK_NUMBER = "1043"
NOTE = "Check 1043 from the operator, deposited 15 October."
RANGE = "?from=2026-10&to=2026-10"
#: The control the reference's label follows, and the text it switches to.
LABEL_SWITCH = 'data-label-when="id_method=check" data-label="Check number"'
#: A table cell naming the method (the responsive tables label their cells).
CHECK_CELL = re.compile(r"<td(?: data-role=\"meta\")?(?: data-label=\"Method\")?>Check</td>")


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *a, **k: TODAY)
    monkeypatch.setattr("django.utils.timezone.now", lambda: NOW)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeProvider]:
    provider = FakeProvider(pay_invoice_enabled=True)
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    with override_settings(STAX_CALLER="worker", STAX_PAY_INVOICE_ENABLED=True):
        yield provider


def a_world(label: str = "") -> Lease:
    """A store with a wire lease from 1 August 2026 and its operator."""
    return a_lease(
        a_store(label), an_operator(), started=date(2026, 8, 1), channel=PaymentChannel.WIRE
    )


def a_connected_world(fake: FakeProvider) -> Lease:
    """A lease of an operator known to the provider, as the builder needs."""
    from tests.test_lease_services import make_lease
    from tests.test_one_off_charges import an_operator as a_connected_operator

    return make_lease(operator=a_connected_operator(fake))


def operator_of(lease: Lease) -> Operator:
    assert lease.operator is not None
    return lease.operator


def billing() -> Client:
    return client_for(Role.BILLING)[0]


def check_form(**fields: str) -> dict[str, str]:
    return {
        "creation_key": str(uuid.uuid4()),
        "method": "check",
        "paid_on": TODAY.isoformat(),
        "reference": CHECK_NUMBER,
        "note": NOTE,
        **fields,
    }


def text(html: str) -> str:
    """The page's text, tags dropped and spaces collapsed."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def assert_shown_as_check(client: Client, payment: Payment, lease: Lease) -> None:
    """The payment page, the payments ledger, the operator's statement and the store's
    Payments tab name the method "Check"."""
    detail = client.get(reverse("ledger:payments:detail", kwargs={"pk": payment.pk}))
    assert "<dt>Method</dt><dd>Check</dd>" in detail.content.decode()
    ledger = client.get(reverse("ledger:payments:list") + "?method=check").content.decode()
    assert CHECK_CELL.search(ledger) and str(payment.pk) in ledger
    statement = client.get(
        reverse("ledger:operators:statement", kwargs={"pk": operator_of(lease).pk}) + RANGE
    ).content.decode()
    assert CHECK_CELL.search(statement)
    tab = client.get(workspace.tab_url(lease.store_id, workspace.TAB_PAYMENTS)).content.decode()
    assert CHECK_CELL.search(tab)


# --- D1, D2: the stored value and the constants ---------------------------------------------


def test_check_is_a_manual_method_between_wire_and_other() -> None:
    assert (PaymentMethod.CHECK.value, PaymentMethod.CHECK.label) == ("check", "Check")
    assert (MoneyMethod.CHECK.value, MoneyMethod.CHECK.label) == ("check", "Check")
    assert rent_run.MANUAL_METHODS == ("zelle", "wire", "check", "other")
    assert ledger_forms.MANUAL_METHOD_CHOICES == (
        ("zelle", "Zelle"),
        ("wire", "Wire"),
        ("check", "Check"),
        ("other", "Other"),
    )
    # The deposit refund offers the same methods after its empty choice.
    assert ledger_forms.RESOLUTION_METHOD_CHOICES[1:] == ledger_forms.MANUAL_METHOD_CHOICES
    # Every manual payment form offers them through the one constant.
    for form in (
        ledger_forms.ManualPaymentForm(),
        ledger_forms.AllocationPaymentForm(),
        ledger_forms.CollectForm(can_charge_now=False),
    ):
        offered = [value for value, _ in form.fields["method"].choices if value]  # type: ignore[attr-defined]
        assert offered == ["zelle", "wire", "check", "other"], type(form).__name__


def test_no_form_lists_the_manual_methods_by_hand() -> None:
    from pathlib import Path

    source = Path(ledger_forms.__file__).read_text()
    for method in ("ZELLE", "WIRE", "OTHER"):
        assert f"PaymentMethod.{method}" not in source, method
    for value in ("zelle", "wire", "check"):
        assert f'"{value}"' not in source, value


# --- Each form records a check ---------------------------------------------------------------


def test_the_store_add_payment_records_a_check() -> None:
    lease = a_world("3101")
    rent = a_rent_charge(lease, date(2026, 9, 1), due="1000.00")
    client = billing()
    response = client.post(
        reverse("ledger:stores:record_payment", kwargs={"pk": lease.store_id}),
        check_form(amount="1000.00", **{f"alloc_{rent.pk}": "1000.00"}),
    )
    assert response.status_code == 302
    payment = Payment.objects.get()
    assert (payment.method, payment.reference, payment.rent_charge_id) == (
        "check",
        CHECK_NUMBER,
        rent.pk,
    )
    assert_shown_as_check(client, payment, lease)


def test_the_store_add_payment_records_a_check_on_an_insurance_charge() -> None:
    lease = a_world("3102")
    insurance = an_insurance_charge(lease, 2026, date(2026, 10, 1))
    client = billing()
    response = client.post(
        reverse("ledger:stores:record_payment", kwargs={"pk": lease.store_id}),
        check_form(amount="695.00", **{f"alloc_{insurance.pk}": "695.00"}),
    )
    assert response.status_code == 302
    payment = Payment.objects.get()
    assert (payment.method, payment.kind, payment.insurance_charge_id) == (
        "check",
        "insurance",
        insurance.pk,
    )
    assert_shown_as_check(client, payment, lease)


def test_a_check_kept_as_a_credit_names_check_on_the_payments_tab() -> None:
    lease = a_world("3103")
    client = billing()
    response = client.post(
        reverse("ledger:stores:record_payment", kwargs={"pk": lease.store_id}),
        check_form(amount="40.00", keep_credit="on"),
    )
    assert response.status_code == 302
    credit = lease.credits.get()
    assert (credit.method, credit.reference) == ("check", CHECK_NUMBER)
    tab = client.get(workspace.tab_url(lease.store_id, workspace.TAB_PAYMENTS)).content.decode()
    assert CHECK_CELL.search(tab)


def test_the_operator_workspace_add_payment_records_a_check() -> None:
    lease = a_world("3104")
    rent = a_rent_charge(lease, date(2026, 9, 1), due="1000.00")
    client = billing()
    response = client.post(
        reverse("ledger:charges:record_payment", kwargs={"pk": rent.pk}),
        check_form(amount="400.00"),
    )
    assert response.status_code == 302
    payment = Payment.objects.get()
    assert (payment.method, payment.reference, payment.amount) == (
        "check",
        CHECK_NUMBER,
        D("400.00"),
    )
    assert_shown_as_check(client, payment, lease)


def test_the_one_off_add_payment_records_a_check() -> None:
    lease = a_world("3105")
    charge = a_one_off_charge(operator_of(lease), "250.00", date(2026, 9, 20), lease=lease)
    client = billing()
    response = client.post(
        reverse("ledger:one_off_charges:payment", kwargs={"pk": charge.pk}),
        check_form(amount="250.00"),
    )
    assert response.status_code == 302
    payment = Payment.objects.get()
    assert (payment.method, payment.one_off_charge_id) == ("check", charge.pk)
    assert_shown_as_check(client, payment, lease)


def test_the_builder_record_payment_now_records_a_check(fake: FakeProvider) -> None:
    lease = a_connected_world(fake)
    client = billing()
    data = {
        "creation_key": str(uuid.uuid4()),
        "lease": str(lease.pk),
        "memo": "",
        "due_date": "",
        "lines-TOTAL_FORMS": "1",
        "lines-INITIAL_FORMS": "0",
        "lines-MIN_NUM_FORMS": "0",
        "lines-MAX_NUM_FORMS": "50",
        "lines-0-item": "Sign repair",
        "lines-0-unit_amount": "75.00",
        "collect": "record",
        "method": "check",
        "paid_on": TODAY.isoformat(),
        "reference": CHECK_NUMBER,
        "payment_note": NOTE,
    }
    response = client.post(f"/invoices/new/store/{lease.store_id}/", data)
    draft = InvoiceDraft.objects.get()
    assert response["Location"] == f"/invoices/drafts/{draft.pk}/"
    payment = Payment.objects.get()
    assert (payment.method, payment.reference, payment.amount) == (
        "check",
        CHECK_NUMBER,
        D("75.00"),
    )
    assert_shown_as_check(client, payment, lease)


def test_a_check_recorded_in_the_builder_is_marked_paid_by_the_worker(
    fake: FakeProvider,
) -> None:
    """The mark job of a check payment runs to done, and the memo names the check."""
    from payments.models import Job, JobStatus
    from tests.job_helpers import run_queued_jobs
    from tests.test_manual_payment_marking import marks

    lease = a_connected_world(fake)
    data = {
        "creation_key": str(uuid.uuid4()),
        "lease": str(lease.pk),
        "memo": "",
        "due_date": "",
        "lines-TOTAL_FORMS": "1",
        "lines-INITIAL_FORMS": "0",
        "lines-MIN_NUM_FORMS": "0",
        "lines-MAX_NUM_FORMS": "50",
        "lines-0-item": "Sign repair",
        "lines-0-unit_amount": "75.00",
        "collect": "record",
        "method": "check",
        "paid_on": TODAY.isoformat(),
        "reference": CHECK_NUMBER,
        "payment_note": NOTE,
    }
    response = billing().post(f"/invoices/new/store/{lease.store_id}/", data)
    assert response.status_code == 302
    run_queued_jobs()
    statuses = {job.kind: job.status for job in Job.objects.all()}
    assert set(statuses.values()) == {JobStatus.DONE}, statuses
    [sent] = marks(fake)
    assert sent.method == "cash" and sent.check_number == CHECK_NUMBER
    assert sent.note == f"Paid by check on 10/15/2026. {NOTE}"
    assert Payment.objects.get().method == "check"


def test_the_builder_service_accepts_check_as_it_accepts_zelle(fake: FakeProvider) -> None:
    lease = a_connected_world(fake)
    actor = client_for(Role.BILLING)[1]
    outcome = invoice_builder.create_draft(
        operator_of(lease),
        lease,
        "",
        None,
        [LineInput("Sign repair", "", None, D("75.00"))],
        actor,
        today=TODAY,
    )
    assert outcome.ok and outcome.draft is not None
    payment = invoice_builder.PaymentInput("check", TODAY, CHECK_NUMBER, NOTE)
    collected = invoice_builder.collect(
        outcome.draft.pk, "record", actor=actor, today=TODAY, payment=payment
    )
    assert collected.ok, collected.message
    assert list(Payment.objects.values_list("method", "reference")) == [("check", CHECK_NUMBER)]


def test_a_deposit_refunded_by_check_writes_a_check_refund() -> None:
    from tests.test_deposits import held_deposit, resolve

    charge = held_deposit()
    outcome = resolve(charge, "refunded", "2500.00", method="check")
    assert outcome.ok, outcome.message
    assert DepositResolution.objects.get().method == "check"
    refund = Payment.objects.get(kind="refund_deposit")
    assert (refund.method, refund.get_method_display()) == ("check", "Check")
    form = ledger_forms.DepositResolutionForm(
        {
            "kind": "refunded",
            "amount": "500.00",
            "resolved_on": "2026-10-15",
            "method": "check",
            "note": "Paid back by check 1044.",
        }
    )
    assert form.is_valid(), form.errors
    assert form.cleaned_data["method"] == "check"


# --- D3: the reference field ------------------------------------------------------------------


@pytest.mark.parametrize(
    "form",
    [
        lambda: ledger_forms.ManualPaymentForm(),
        lambda: ledger_forms.AllocationPaymentForm(),
        lambda: ledger_forms.CollectForm(can_charge_now=False),
    ],
    ids=["manual", "allocation", "collect"],
)
def test_the_reference_is_labelled_reference_and_its_help_names_the_check_number(
    form: object,
) -> None:
    built = form()  # type: ignore[operator]
    reference = built["reference"]
    assert reference.label == "Reference"
    assert reference.help_text.startswith(ledger_forms.REFERENCE_HELP)
    assert "check number" in reference.help_text and "never an account number" in (
        reference.help_text
    )
    rendered = str(reference)
    assert LABEL_SWITCH in rendered


@pytest.mark.parametrize("page", ["store", "charge", "one_off", "builder"])
def test_every_manual_payment_page_switches_the_reference_label_with_the_script(
    page: str, fake: FakeProvider
) -> None:
    lease = a_world(f"32{len(page)}")
    if page == "store":
        url = reverse("ledger:stores:record_payment", kwargs={"pk": lease.store_id})
    elif page == "charge":
        rent = a_rent_charge(lease, date(2026, 9, 1), due="1000.00")
        url = reverse("ledger:charges:record_payment", kwargs={"pk": rent.pk})
    elif page == "one_off":
        charge = a_one_off_charge(operator_of(lease), "250.00", date(2026, 9, 20), lease=lease)
        url = reverse("ledger:one_off_charges:payment", kwargs={"pk": charge.pk})
    else:
        url = f"/invoices/new/store/{lease.store_id}/"
    html = billing().get(url).content.decode()
    # Without script the label reads "Reference"; the help names the check number.
    assert '<label for="id_reference">Reference</label>' in html
    assert ledger_forms.REFERENCE_HELP in html
    # The script switches it while Check is chosen in the method select.
    tag = re.search(r"<input[^>]*name=\"reference\"[^>]*>", html)
    assert tag is not None and LABEL_SWITCH in tag.group(0)
    assert 'id="id_method"' in html and '<option value="check">Check</option>' in html
    assert "ledger/shown_when.js" in html


def test_the_label_switch_lives_in_the_dependent_fields_script() -> None:
    from pathlib import Path

    script = (
        Path(ledger_forms.__file__).parent / "static" / "ledger" / "shown_when.js"
    ).read_text()
    assert "data-label-when" in script and "data-label" in script
    # No library: the script is the page's own.
    assert "import " not in script and "require(" not in script


# --- D4: exports ---------------------------------------------------------------------------


def a_check_payment(lease: Lease) -> Payment:
    rent = a_rent_charge(lease, date(2026, 10, 1), due="1000.00")
    outcome = rent_run.record_manual_payment(
        rent.pk,
        method="check",
        amount=D("300.00"),
        paid_on=TODAY,
        note=NOTE,
        actor=None,
        reference=CHECK_NUMBER,
        today=TODAY,
    )
    assert outcome.ok and outcome.payment is not None, outcome.message
    return outcome.payment


def csv_rows(client: Client, url: str) -> list[dict[str, str]]:
    response = client.get(url)
    assert response.status_code == 200
    body = b"".join(response.streaming_content).decode()  # type: ignore[attr-defined]
    return list(csv.DictReader(io.StringIO(body.lstrip("﻿"))))


def test_the_csv_exports_write_the_check_method() -> None:
    lease = a_world("3301")
    payment = a_check_payment(lease)
    client = billing()
    ledger_rows = csv_rows(client, reverse("ledger:payments:export"))
    assert [(row["Payment ID"], row["Method"]) for row in ledger_rows] == [
        (str(payment.pk), "check")
    ]
    statement = csv_rows(
        client,
        reverse("ledger:operators:statement_export", kwargs={"pk": operator_of(lease).pk}) + RANGE,
    )
    assert "check" in {row["Method"] for row in statement}
    by_method = csv_rows(client, reverse("ledger:reports:payments_by_method_export") + RANGE)
    assert list(by_method[0])[:6] == [
        "Month",
        "ACH (USD)",
        "Zelle (USD)",
        "Wire (USD)",
        "Check (USD)",
        "Other (USD)",
    ]
    october = by_method[0]
    assert (october["Month"], october["Check (USD)"], october["Total (USD)"]) == (
        "2026-10",
        "300.00",
        "300.00",
    )
    assert reports.PAYMENTS_BY_METHOD_CSV_HEADER[1 : 1 + len(reports.METHODS)] == tuple(
        f"{MoneyMethod(method).label if method != 'ach' else 'ACH'} (USD)"
        for method in reports.METHODS
    )


def test_the_payments_by_method_page_has_a_check_column() -> None:
    lease = a_world("3302")
    a_check_payment(lease)
    html = billing().get(reverse("ledger:reports:payments_by_method") + RANGE).content.decode()
    assert '<th class="money">Wire</th><th class="money">Check</th>' in html
    row = re.search(r'<tr class="method-month" data-month="2026-10">(.*?)</tr>', html, re.S)
    assert row is not None
    cells = re.findall(r'<td class="money"[^>]*>([^<]*)</td>', row.group(1))
    assert cells[:5] == ["$0.00", "$0.00", "$0.00", "$300.00", "$0.00"]


# --- D5: the builder's lines table in the scroll region -----------------------------------------


def test_the_builder_lines_table_is_inside_the_scroll_region(fake: FakeProvider) -> None:
    lease = a_world("3401")
    html = billing().get(f"/invoices/new/store/{lease.store_id}/").content.decode()
    region = re.search(
        r'<div class="table-scroll" tabindex="0">\s*<table id="draft-lines"(.*?)</table>\s*</div>',
        html,
        re.S,
    )
    assert region is not None
    table = region.group(1)
    # The builder's script still finds its table and rows; each control keeps its
    # visually hidden label inside the table.
    assert 'class="draft-line"' in table and 'class="line-number" data-label="Line"' in table
    assert 'class="visually-hidden"' in table
    assert 'class="table-cards"' not in html.split('id="draft-lines"')[0][-200:]
    assert "ledger/invoice_lines.js" in html
    assert text(html).count("Lines") >= 1

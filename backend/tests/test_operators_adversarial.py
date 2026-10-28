"""Adversarial cases for PLAN-006, written by the QA owner (plan §6 rows 11 to 13)."""

from __future__ import annotations

import threading
import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest
from django.db import connections
from django.test import Client, override_settings

from accounts.roles import Role
from ledger.models import Operator, SyncStatus
from payments import operator_sync
from payments.errors import ProviderError
from payments.fake import FakeProvider
from payments.models import ExternalRef
from payments.provider import CustomerInput
from payments.stax import mapping
from tests.auth_helpers import make_user, verified_client
from tests.job_helpers import worker_between_requests  # noqa: F401
from tests.stax_helpers import make_rig
from tests.test_operator_sync import (
    add_provider_customer,
    call_names,
    customer_refs,
    fields,
    requires_postgresql,
)
from tests.test_operator_views import form_data
from tests.text_helpers import has_token

pytestmark = pytest.mark.django_db

CARD = "4111111111111111"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


def _client(role: Role) -> Client:
    user, device = make_user(f"adv-{role.value}-{uuid.uuid4().hex[:6]}", role)
    assert device is not None
    return verified_client(user, device)


@pytest.fixture
def billing() -> Client:
    return _client(Role.BILLING)


@pytest.fixture(autouse=True)
def _worker_runs_between_requests(request: pytest.FixtureRequest) -> None:
    """PLAN-010: views queue provider work; these cases check the outcome after the worker ran."""
    request.getfixturevalue("worker_between_requests")


# --- 11: forms and search --------------------------------------------------------------------


def test_replaying_a_used_creation_key_with_other_values_changes_nothing(
    billing: Client, fake: FakeProvider
) -> None:
    data = form_data(company="First Name")
    billing.post("/operators/new/", data)
    replay = {**data, "company": "Overwritten", "email": "other@example.com"}
    response = billing.post("/operators/new/", replay)

    assert response.status_code in (200, 302)
    assert Operator.objects.count() == 1
    operator = Operator.objects.get()
    assert operator.display_name == "First Name"
    assert operator.email == fields()["email"]
    assert call_names(fake).count("create_customer") == 1


def test_a_creation_key_from_another_user_cannot_overwrite_or_leak(fake: FakeProvider) -> None:
    first = _client(Role.BILLING)
    second = _client(Role.BILLING)
    data = form_data(company="Owner One", email="one@example.com")
    first.post("/operators/new/", data)
    stolen = {**data, "company": "Owner Two", "email": "two@example.com"}
    response = second.post("/operators/new/", stolen)

    assert Operator.objects.count() == 1
    assert Operator.objects.get().display_name == "Owner One"
    assert "two@example.com" not in response.content.decode(errors="replace")


@pytest.mark.parametrize(
    "query",
    [
        "' OR 1=1 --",
        "{{ settings.SECRET_KEY }}",
        "<script>alert(1)</script>",
        "%",
        "_",
        "\\",
        "a" * 5000,
        "\u0000",
        "🙂",
    ],
)
def test_search_input_is_inert(billing: Client, fake: FakeProvider, query: str) -> None:
    Operator.objects.create(display_name="Plain Operator")
    response = billing.get("/operators/", {"q": query})
    body = response.content.decode(errors="replace")
    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in body
    assert "placeholder-not-secret" not in body  # the setting's value, never rendered
    assert "Traceback" not in body


@pytest.mark.parametrize("page", ["0", "-1", "99999", "abc", "1e9", ""])
def test_paging_past_the_end_or_nonsense_still_renders(billing: Client, page: str) -> None:
    for index in range(30):
        Operator.objects.create(display_name=f"Operator {index:02d}")
    response = billing.get("/operators/", {"page": page})
    assert response.status_code == 200
    assert has_token(response.content.decode(), "Operator")


def test_an_archived_operator_refuses_edit_retry_and_link_through_every_route(
    billing: Client, fake: FakeProvider
) -> None:
    operator = Operator.objects.create(display_name="Gone", sync_status=SyncStatus.FAILED)
    operator_sync.archive_operator(operator.pk, actor=None)
    before = Operator.objects.get(pk=operator.pk)

    billing.post(f"/operators/{operator.pk}/edit/", form_data(display_name="Back"))
    billing.post(f"/operators/{operator.pk}/retry-sync/")
    billing.post(
        f"/operators/{operator.pk}/link/",
        {"customer_external_id": "cus_1", "payment_method_external_id": "pm_1"},
    )

    after = Operator.objects.get(pk=operator.pk)
    assert after.display_name == before.display_name == "Gone"
    assert after.sync_status == before.sync_status
    assert call_names(fake) == []
    assert customer_refs(after) == []


def test_unknown_operator_ids_are_404_not_500(billing: Client) -> None:
    for pk in ("not-a-uuid", str(uuid.uuid4()), "../", "%00", "1' OR '1'='1"):
        response = billing.get(f"/operators/{pk}/")
        assert response.status_code == 404, pk


# --- 12: what the provider sends back ----------------------------------------------------------


@pytest.mark.parametrize(
    ("stored", "returned", "should_match"),
    [
        ("ops@example.com", "OPS@EXAMPLE.COM", True),
        ("ops@example.com", "  ops@example.com  ", True),
        ("ops@example.com", "ops@example.com.", False),
        ("ops@example.com", "ops@example.co", False),
        ("ops@example.com", "ops+rent@example.com", False),
        ("ops@example.com", "оps@example.com", False),  # Cyrillic о
        ("straße@example.com", "STRASSE@example.com", False),
    ],
)
def test_email_matching_is_exact_after_lower_case_and_strip(
    fake: FakeProvider,
    stored: str,
    returned: str,
    should_match: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whatever the provider's filter returns (TODO-VERIFY-031), our own comparison decides."""
    add_provider_customer(fake, "cus_existing", returned)
    # Simulate a fuzzy provider filter that returns every customer for any query.
    monkeypatch.setattr(fake, "find_customers", lambda query: list(fake.customers.values()))
    operator, _ = operator_sync.create_operator(
        fields(email=stored), creation_key=uuid.uuid4(), actor=None
    )
    operator_sync.sync_operator(operator.pk, provider=fake, actor=None)
    refs = customer_refs(operator)
    if should_match:
        assert [ref.external_id for ref in refs] == ["cus_existing"]
        assert "create_customer" not in call_names(fake)
    else:
        assert [ref.external_id for ref in refs] != ["cus_existing"]
        assert "create_customer" in call_names(fake)


def test_bank_fields_in_a_provider_payload_never_survive_the_summary() -> None:
    payload = {
        "id": "cus_1",
        "email": "ops@example.com",
        "bank_account": "000123456789",
        "routing_number": "021000021",
        "account_number": "123456789012",
        "card_number": CARD,
        "payment_method": {
            "id": "pm_1",
            "method": "bank",
            "bank_account": "000123456789",
            "routing": "021000021",
            "card_last_four": "1111",
            "nickname": "checking",
            "person_name": "Should Drop",
        },
        "note": "pay to routing 021000021 acct 12345678",
        "nested": {"account": "hidden"},
    }
    trimmed = mapping.summary(payload)
    text = str(trimmed)
    for forbidden in ("000123456789", "021000021", "123456789012", CARD, "Should Drop", "hidden"):
        assert forbidden not in text, forbidden
    method = trimmed["payment_method"]
    assert isinstance(method, dict)
    assert set(method) <= {"id", "method", "card_last_four", "nickname"}


def test_a_provider_customer_record_with_bank_fields_is_stored_trimmed(fake: FakeProvider) -> None:
    """End to end through the fake: the raw summary that reaches external refs is clean."""
    record = add_provider_customer(fake, "cus_dirty", "ops@example.com")
    object.__setattr__(record, "raw_summary", {"bank_account": "000123456789", "id": "cus_dirty"})
    operator, _ = operator_sync.create_operator(
        fields(email="ops@example.com"), creation_key=uuid.uuid4(), actor=None
    )
    operator_sync.sync_operator(operator.pk, provider=fake, actor=None)
    from payments.models import ProviderSnapshot

    for snapshot in ProviderSnapshot.objects.all():
        assert "000123456789" not in str(snapshot.payload)


# --- 13: concurrency and the boot check under a hostile body -----------------------------------


@requires_postgresql
@pytest.mark.django_db(transaction=True)
def test_retry_and_link_at_the_same_moment_leave_one_customer_reference(fake: FakeProvider) -> None:
    add_provider_customer(fake, "cus_dash", "race@example.com")
    operator, _ = operator_sync.create_operator(
        fields(email="race@example.com"), creation_key=uuid.uuid4(), actor=None
    )
    Operator.objects.filter(pk=operator.pk).update(sync_status=SyncStatus.FAILED)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def retry() -> None:
        try:
            barrier.wait(timeout=10)
            operator_sync.sync_operator(operator.pk, provider=fake, actor=None, retry=True)
        except BaseException as error:
            errors.append(error)
        finally:
            connections.close_all()

    def link() -> None:
        try:
            barrier.wait(timeout=10)
            operator_sync.link_operator(
                operator.pk,
                customer_external_id="cus_dash",
                payment_method_external_id="pm_dash",
                provider=fake,
                actor=None,
            )
        except (ProviderError, ValueError) as error:
            errors.append(error)
        except BaseException as error:  # a refusal by the service is fine; a crash is not
            errors.append(error)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=retry), threading.Thread(target=link)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    current = ExternalRef.objects.filter(
        entity_type="operator", entity_id=operator.pk, external_kind="customer", superseded_at=None
    )
    assert current.count() == 1
    assert current.get().external_id == "cus_dash"
    assert all(not isinstance(error, AssertionError) for error in errors)


def test_boot_check_never_prints_a_hostile_body() -> None:
    from tests.test_provider_environment_check import run

    body = {"error": f"token FAKE-CI-KEY-XYZ123 card {CARD} BODY-MARKER"}
    rig = make_rig(lambda request: httpx.Response(500, json=body))
    with override_settings(STAX_CALLER="none"):
        code, out, err = run(rig)
    assert code == 2
    for marker in ("FAKE-CI-KEY-XYZ123", CARD, "BODY-MARKER"):
        assert marker not in out + err


def test_boot_check_with_a_lying_success_body() -> None:
    """A 200 whose merchant block says live while STAX_ENV is sandbox must refuse to start."""
    from tests.test_provider_environment_check import run

    body = {"merchant": {"id": "m-1", "company_name": "X", "is_sandbox": "true"}, "user": {}}
    rig = make_rig(lambda request: httpx.Response(200, json=body))
    code, _, _ = run(rig)
    assert code == 3  # a string is not a boolean flag


def test_outstanding_balance_is_exact_money(billing: Client, fake: FakeProvider) -> None:
    from datetime import date

    from ledger.models import Lease, RentCharge, RentChargeStatus, Store

    operator = Operator.objects.create(display_name="Balance Op")
    store = Store.objects.create(external_label="2475", city="Conyers", state="GA", status="active")
    lease = Lease.objects.create(
        store=store,
        operator=operator,
        debit_day=1,
        collection_status="active",
        payment_channel="stax",
    )
    for due, paid in (("0.10", "0.00"), ("0.20", "0.00"), ("3800.00", "3799.99")):
        RentCharge.objects.create(
            lease=lease,
            period_month=date(2026, 1 + len(RentCharge.objects.filter(lease=lease)), 1),
            seq=1,
            due_on=date(2026, 1, 5),
            amount_due=Decimal(due),
            amount_paid=Decimal(paid),
            status=RentChargeStatus.UNPAID,
            local_ref=uuid.uuid4(),
        )
    response = billing.get(f"/operators/{operator.pk}/")
    assert "0.31" in response.content.decode()


# --- PLAN-006b row 6: the phone rule under hostile input ---------------------------------------


@pytest.mark.parametrize(
    "phone",
    [
        "".join(chr(0xFF10 + int(d)) for d in "4155550132"),  # full-width digits
        "".join(chr(0x0660 + int(d)) for d in "4155550132"),  # Arabic-Indic digits
        "415+555+0132",
        "++14155550132",
        "4111111111111111",  # Luhn-valid 16-digit run: too long for a phone
        "4111 1111 1111 1111",
        "0" * 9,
        "1" * 16,
        "415-555-0132 ext 44",
        "tel:4155550132",
        "4155550132​",
    ],
)
def test_hostile_phones_are_refused_without_a_call_and_without_the_card_message(
    billing: Client, fake: FakeProvider, phone: str
) -> None:
    response = billing.post("/operators/new/", form_data(phone=phone), follow=True)
    body = response.content.decode(errors="replace")
    assert Operator.objects.count() == 0
    assert call_names(fake) == []
    assert "Enter 10 to 15 digits" in body
    assert "card or account numbers" not in body
    assert "4111111111111111" not in body


@pytest.mark.parametrize("phone", ["0000000000", "+1 (415) 555-0132", "415.555.0132", "9" * 15])
def test_accepted_phones_reach_the_provider_as_digits_only(
    billing: Client, fake: FakeProvider, phone: str
) -> None:
    billing.post("/operators/new/", form_data(phone=phone, email="digits@example.com"))
    operator = Operator.objects.get()
    assert operator.phone.isdigit() and 10 <= len(operator.phone) <= 15
    created = [args for name, args in fake.calls if name == "create_customer"]
    assert len(created) == 1
    sent = created[0][0]
    assert isinstance(sent, CustomerInput)
    assert sent.phone == operator.phone


# --- PLAN-006c row 7: edits of a linked operator ------------------------------------------------


def _linked_operator(fake: FakeProvider, **fields: Any) -> Operator:
    values = {"email": "linked@example.com", "phone": "4155550132", **fields}
    operator, _ = operator_sync.create_operator(
        fields_for_adversarial(**values), creation_key=uuid.uuid4(), actor=None
    )
    operator_sync.sync_operator(operator.pk, provider=fake, actor=None)
    fake.calls.clear()
    operator.refresh_from_db()
    assert operator.sync_status == SyncStatus.LINKED
    return operator


def fields_for_adversarial(**overrides: Any) -> dict[str, Any]:
    return fields(**overrides)


def test_a_rejected_update_whose_body_names_the_token_leaves_no_trace_of_it(
    fake: FakeProvider, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    from payments.errors import ProviderRejected

    operator = _linked_operator(fake)
    fake.fail_next("update_customer", ProviderRejected(422, "token FAKE-CI-KEY-XYZ123 leaked"))
    caplog.set_level(logging.INFO)
    billing = _client(Role.BILLING)
    billing.post(f"/operators/{operator.pk}/edit/", form_data(**fields(phone="4155550199")))
    operator.refresh_from_db()
    assert operator.sync_status == SyncStatus.FAILED
    assert operator.phone == "4155550199"  # the local change stands
    page = billing.get(f"/operators/{operator.pk}/").content.decode()
    for text in (page, caplog.text, operator.sync_failure, operator.sync_failure_fields):
        assert "FAKE-CI-KEY-XYZ123" not in text
        assert not has_token(text, "leaked")
    from ledger.models import AuditLog

    for row in AuditLog.objects.filter(entity_type="operator"):
        assert "leaked" not in str(row.after) and "FAKE-CI-KEY-XYZ123" not in str(row.after)


def test_a_bad_phone_on_edit_is_refused_by_the_form_before_any_call(fake: FakeProvider) -> None:
    operator = _linked_operator(fake)
    billing = _client(Role.BILLING)
    billing.post(f"/operators/{operator.pk}/edit/", form_data(**fields(phone="555-0132")))
    operator.refresh_from_db()
    assert operator.phone == "4155550132"
    assert call_names(fake) == []
    assert operator.sync_status == SyncStatus.LINKED


def test_an_edit_of_an_archived_linked_operator_calls_nothing(fake: FakeProvider) -> None:
    operator = _linked_operator(fake)
    operator_sync.archive_operator(operator.pk, actor=None)
    fake.calls.clear()
    _client(Role.BILLING).post(
        f"/operators/{operator.pk}/edit/", form_data(**fields(phone="4155550199"))
    )
    operator.refresh_from_db()
    assert operator.phone == "4155550132"
    assert call_names(fake) == []


def test_update_audit_rows_never_carry_the_full_email(fake: FakeProvider) -> None:
    from ledger.models import AuditLog

    operator = _linked_operator(fake, email="private.person@example.com")
    _client(Role.BILLING).post(
        f"/operators/{operator.pk}/edit/", form_data(**fields(email="other.person@example.com"))
    )
    rows = AuditLog.objects.filter(
        entity_type="operator", action__in=["operator.sync_updated", "operator.sync_failed"]
    )
    assert rows.exists()
    for row in rows:
        text = str(row.after) + str(row.before)
        assert "private.person@" not in text and "other.person@" not in text


def test_an_update_after_retries_exhausted_keeps_the_local_change_and_fails_once(
    fake: FakeProvider,
) -> None:
    from payments.errors import ProviderUnavailable

    operator = _linked_operator(fake)
    fake.fail_next("update_customer", ProviderUnavailable(503))
    _client(Role.BILLING).post(
        f"/operators/{operator.pk}/edit/", form_data(**fields(phone="4155550177"))
    )
    operator.refresh_from_db()
    assert operator.phone == "4155550177"
    assert operator.sync_status == SyncStatus.FAILED
    assert call_names(fake).count("update_customer") == 1
    assert "create_customer" not in call_names(fake)

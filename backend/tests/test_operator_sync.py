"""The operator sync service (docs/qa/PLAN-006-operators.md §6 rows 1, 2, 3, 5 and 8).

Every provider here is a :class:`~payments.fake.FakeProvider` or a subclass defined in
this file; nothing opens a socket.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import override_settings
from django.utils import timezone

from accounts.roles import Role
from ledger.models import AuditLog, EnrolmentStatus, Operator, SyncFailure, SyncStatus
from payments import operator_sync
from payments.errors import (
    CircuitOpen,
    ProviderError,
    ProviderRejected,
    ProviderUnavailable,
    TransportError,
)
from payments.fake import FakeProvider
from payments.models import ExternalRef
from payments.operator_sync import Action
from payments.provider import CustomerQuery, CustomerRecord
from payments.stax.adapter import StaxProvider
from tests.auth_helpers import make_user
from tests.stax_helpers import make_rig, sequence

requires_postgresql = pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="row locks and concurrent transactions need PostgreSQL",
)

EMAIL = "ops@example.com"


class FuzzyFakeProvider(FakeProvider):
    """A provider whose e-mail filter is not exact: it returns every customer.

    Stands in for the unknown behaviour of the real filter (TODO-VERIFY-031), so the
    service's own comparison is what decides a match.
    """

    def find_customers(self, query: CustomerQuery) -> list[CustomerRecord]:
        self._call("find_customers", query)
        return list(self.customers.values())


def add_provider_customer(
    provider: FakeProvider, external_id: str, email: str, created_at: str = ""
) -> CustomerRecord:
    """Put a customer on the provider side, as if created in its dashboard."""
    summary: dict[str, Any] = {"created_at": created_at} if created_at else {}
    record = CustomerRecord(
        external_id=external_id,
        raw_status="",
        fetched_at=datetime(2026, 9, 30, tzinfo=UTC),
        operator_email=email,
        raw_summary=summary,
    )
    provider.customers[external_id] = record
    return record


def call_names(provider: FakeProvider) -> list[str]:
    return [name for name, _ in provider.calls]


def fields(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "display_name": "Northwind Stores",
        "legal_name": "Northwind Stores LLC",
        "company": "Northwind",
        "email": EMAIL,
        "phone": "+1 555 010 0001",
        "address_line1": "1 Demo Avenue",
        "address_line2": "",
        "city": "Testville",
        "state": "TX",
        "postal_code": "75001",
        "notes": "",
    }
    return {**values, **overrides}


def customer_refs(operator: Operator) -> list[ExternalRef]:
    return list(
        ExternalRef.objects.filter(
            entity_type="operator", entity_id=operator.pk, external_kind="customer"
        )
    )


@pytest.fixture
def actor() -> User:
    user, _ = make_user("billing-sync", Role.BILLING)
    return user


@pytest.fixture
def fake() -> FakeProvider:
    return FakeProvider()


def create(actor: User, **overrides: Any) -> Operator:
    operator, created = operator_sync.create_operator(
        fields(**overrides), creation_key=uuid.uuid4(), actor=actor
    )
    assert created
    return operator


# --- Row 1: one provider customer however often the create is sent ---------------------


@pytest.mark.django_db
def test_the_same_creation_key_twice_gives_one_operator_and_one_customer(
    actor: User, fake: FakeProvider
) -> None:
    key = uuid.uuid4()
    first, created_first = operator_sync.create_operator(fields(), creation_key=key, actor=actor)
    operator_sync.sync_operator(first.pk, provider=fake, actor=actor)
    second, created_second = operator_sync.create_operator(
        fields(display_name="Replayed with other values"), creation_key=key, actor=actor
    )
    operator_sync.sync_operator(second.pk, provider=fake, actor=actor)

    assert (created_first, created_second) == (True, False)
    assert second.pk == first.pk
    assert Operator.objects.count() == 1
    assert Operator.objects.get().display_name == "Northwind Stores"
    assert call_names(fake).count("create_customer") == 1
    assert len(customer_refs(first)) == 1


@pytest.mark.django_db
def test_a_linked_operator_is_never_synced_again(actor: User, fake: FakeProvider) -> None:
    operator = create(actor)
    operator_sync.sync_operator(operator.pk, provider=fake, actor=actor)
    calls = len(fake.calls)
    for retry in (False, True):
        outcome = operator_sync.sync_operator(operator.pk, provider=fake, actor=actor, retry=retry)
        assert outcome.action is Action.NO_CALL
    assert len(fake.calls) == calls


class SlowFakeProvider(FakeProvider):
    """Holds each lookup long enough for a second request to arrive meanwhile."""

    def find_customers(self, query: CustomerQuery) -> list[CustomerRecord]:
        time.sleep(0.3)
        return super().find_customers(query)


@requires_postgresql
@pytest.mark.django_db(transaction=True)
def test_concurrent_creates_with_one_key_make_one_customer(actor: User) -> None:
    provider = SlowFakeProvider()
    key = uuid.uuid4()
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def submit() -> None:
        try:
            barrier.wait()
            operator, _ = operator_sync.create_operator(fields(), creation_key=key, actor=actor)
            operator_sync.sync_operator(operator.pk, provider=provider, actor=actor)
        except BaseException as error:  # reported below, not swallowed
            errors.append(error)
        finally:
            connection.close()

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert Operator.objects.count() == 1
    assert call_names(provider).count("create_customer") == 1
    assert len(customer_refs(Operator.objects.get())) == 1


@requires_postgresql
@pytest.mark.django_db(transaction=True)
def test_concurrent_syncs_of_one_operator_call_the_provider_once(actor: User) -> None:
    provider = SlowFakeProvider()
    operator = create(actor)
    barrier = threading.Barrier(3)
    outcomes: list[Action] = []

    def sync() -> None:
        try:
            barrier.wait()
            outcomes.append(
                operator_sync.sync_operator(operator.pk, provider=provider, actor=actor).action
            )
        finally:
            connection.close()

    threads = [threading.Thread(target=sync) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == sorted([Action.CREATED, Action.NO_CALL, Action.NO_CALL])
    assert call_names(provider) == ["find_customers", "create_customer"]


# --- Row 2: provider failures -----------------------------------------------------------

FAILURES: list[tuple[str, ProviderError, str]] = [
    ("find_customers", ProviderUnavailable(503), "ProviderUnavailable"),
    ("find_customers", TransportError(), "TransportError"),
    ("find_customers", CircuitOpen("open"), "CircuitOpen"),
    ("create_customer", ProviderRejected(422, "CANARY provider text"), "ProviderRejected"),
    ("create_customer", ProviderUnavailable(500), "ProviderUnavailable"),
]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("method", "error", "name"), FAILURES, ids=[f"{m}-{n}" for m, _, n in FAILURES]
)
def test_a_failure_keeps_the_local_record_and_stores_only_the_class_name(
    actor: User, fake: FakeProvider, method: str, error: ProviderError, name: str
) -> None:
    operator = create(actor)
    fake.fail_next(method, error)

    outcome = operator_sync.sync_operator(operator.pk, provider=fake, actor=actor)

    operator.refresh_from_db()
    assert outcome.action is Action.FAILED
    assert (operator.sync_status, operator.sync_failure) == (SyncStatus.FAILED, name)
    assert operator.sync_attempted_at is not None
    assert customer_refs(operator) == []
    assert "CANARY" not in outcome.message
    assert outcome.message == operator_sync.MESSAGES[name]
    audit = AuditLog.objects.get(action="operator.sync_failed")
    assert "CANARY" not in str(audit.after)


@pytest.mark.django_db
def test_a_failed_operator_is_not_called_again_until_retry(actor: User, fake: FakeProvider) -> None:
    operator = create(actor)
    fake.fail_next("find_customers", ProviderUnavailable(503))
    operator_sync.sync_operator(operator.pk, provider=fake, actor=actor)
    assert len(fake.calls) == 1

    # A plain sync (a page load, an edit) does nothing.
    assert operator_sync.sync_operator(operator.pk, provider=fake, actor=actor).action is (
        Action.NO_CALL
    )
    operator_sync.update_operator(operator.pk, fields(phone="+1 555 010 0002"), actor=actor)
    assert operator_sync.sync_operator(operator.pk, provider=fake, actor=actor).action is (
        Action.NO_CALL
    )
    assert len(fake.calls) == 1

    # The retry, once the lock has passed: one lookup and one create, then linked.
    Operator.objects.filter(pk=operator.pk).update(
        sync_attempted_at=timezone.now() - timedelta(seconds=operator_sync.RETRY_LOCK_SECONDS)
    )
    outcome = operator_sync.sync_operator(operator.pk, provider=fake, actor=actor, retry=True)
    assert outcome.action is Action.CREATED
    assert call_names(fake) == ["find_customers", "find_customers", "create_customer"]
    operator.refresh_from_db()
    assert (operator.sync_status, operator.sync_failure) == (SyncStatus.LINKED, "")


def test_an_unknown_provider_error_subclass_maps_to_its_nearest_known_class() -> None:
    class Unforeseen(ProviderUnavailable):
        pass

    assert operator_sync.failure_name(Unforeseen(502)) == "ProviderUnavailable"
    assert operator_sync.failure_name(ProviderError("x")) == "ProviderError"


# --- Row 3: an existing provider customer with the same e-mail ---------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    "provider_email",
    ["ops@example.com", "OPS@Example.COM", "  ops@example.com  ", "\tOps@example.com\n"],
)
def test_an_existing_customer_is_linked_and_none_is_created(
    actor: User, provider_email: str
) -> None:
    provider = FuzzyFakeProvider()
    add_provider_customer(provider, "cus_existing_0001", provider_email)
    add_provider_customer(provider, "cus_other_0002", "someone-else@example.com")
    operator = create(actor)

    outcome = operator_sync.sync_operator(operator.pk, provider=provider, actor=actor)

    assert outcome.action is Action.LINKED_EXISTING
    assert "create_customer" not in call_names(provider)
    [ref] = customer_refs(operator)
    assert ref.external_id == "cus_existing_0001"
    operator.refresh_from_db()
    assert (operator.sync_status, operator.needs_review) == (SyncStatus.LINKED, False)


@pytest.mark.django_db
def test_two_matches_link_the_oldest_and_flag_for_review(actor: User) -> None:
    provider = FuzzyFakeProvider()
    add_provider_customer(provider, "cus_newer_00001", EMAIL, "2026-05-01 10:00:00")
    add_provider_customer(provider, "cus_oldest_0001", "OPS@example.com", "2024-01-01 09:00:00")
    add_provider_customer(provider, "cus_no_date_001", EMAIL)
    operator = create(actor)

    outcome = operator_sync.sync_operator(operator.pk, provider=provider, actor=actor)

    assert outcome.action is Action.LINKED_EXISTING
    assert outcome.needs_review is True
    [ref] = customer_refs(operator)
    assert ref.external_id == "cus_oldest_0001"
    operator.refresh_from_db()
    assert operator.needs_review is True
    assert "create_customer" not in call_names(provider)
    audit = AuditLog.objects.get(action="operator.sync_linked")
    assert audit.after is not None and audit.after["matches"] == 3


@pytest.mark.django_db
def test_a_near_miss_email_is_not_a_match(actor: User) -> None:
    provider = FuzzyFakeProvider()
    add_provider_customer(provider, "cus_near_000001", "ops@example.com.")
    add_provider_customer(provider, "cus_near_000002", "xops@example.com")
    operator = create(actor)

    outcome = operator_sync.sync_operator(operator.pk, provider=provider, actor=actor)

    assert outcome.action is Action.CREATED
    assert call_names(provider).count("create_customer") == 1


@pytest.mark.django_db
def test_a_match_already_linked_to_another_operator_is_not_reused(actor: User) -> None:
    provider = FuzzyFakeProvider()
    add_provider_customer(provider, "cus_taken_00001", EMAIL)
    other = create(actor, email="other@example.com")
    ExternalRef.objects.create(
        entity_type="operator",
        entity_id=other.pk,
        provider=operator_sync.PROVIDER_KEY,
        external_kind="customer",
        external_id="cus_taken_00001",
    )
    operator = create(actor)

    outcome = operator_sync.sync_operator(operator.pk, provider=provider, actor=actor)

    assert outcome.action is Action.FAILED
    assert outcome.failure == SyncFailure.ALREADY_LINKED_ELSEWHERE
    operator.refresh_from_db()
    assert operator.needs_review is True
    assert "create_customer" not in call_names(provider)


@pytest.mark.django_db
def test_the_create_carries_our_reference_and_the_d5_mapping(
    actor: User, fake: FakeProvider
) -> None:
    operator = create(actor, legal_name="")
    operator_sync.sync_operator(operator.pk, provider=fake, actor=actor)
    [(name, (customer,))] = [call for call in fake.calls if call[0] == "create_customer"]
    assert name == "create_customer"
    assert customer.first_name == "Northwind Stores"  # type: ignore[attr-defined]
    assert customer.last_name == ""  # type: ignore[attr-defined]
    assert customer.company_name == "Northwind Stores"  # type: ignore[attr-defined]
    assert customer.operator_email == EMAIL  # type: ignore[attr-defined]
    assert customer.reference == str(operator.pk)  # type: ignore[attr-defined]


# --- Row 5: no e-mail, and the manual link ----------------------------------------------


@pytest.mark.django_db
def test_no_email_saves_awaiting_link_and_calls_nothing(actor: User, fake: FakeProvider) -> None:
    operator = create(actor, email=None)
    assert operator.sync_status == SyncStatus.AWAITING_LINK
    outcome = operator_sync.sync_operator(operator.pk, provider=fake, actor=actor)
    assert outcome.action is Action.NO_CALL
    assert fake.calls == []


@pytest.mark.django_db
def test_link_stores_both_ids_after_one_read_and_enrols(actor: User, fake: FakeProvider) -> None:
    add_provider_customer(fake, "cus_manual_0001", "")
    operator = create(actor, email=None)

    outcome = operator_sync.link_operator(
        operator.pk,
        customer_external_id="cus_manual_0001",
        payment_method_external_id="pm_bank-0001",
        provider=fake,
        actor=actor,
    )

    assert outcome.action is Action.LINK_SET
    assert fake.calls == [("get_customer", ("cus_manual_0001",))]
    refs = operator_sync.current_refs(operator)
    assert refs == {"customer": "cus_manual_0001", "payment_method": "pm_bank-0001"}
    operator.refresh_from_db()
    assert operator.enrolment_status == EnrolmentStatus.ENROLLED
    assert operator.sync_status == SyncStatus.LINKED


@pytest.mark.django_db
def test_a_bad_customer_id_stores_nothing(actor: User, fake: FakeProvider) -> None:
    operator = create(actor, email=None)

    outcome = operator_sync.link_operator(
        operator.pk,
        customer_external_id="cus_missing_001",
        payment_method_external_id="pm_bank-0001",
        provider=fake,
        actor=actor,
    )

    assert outcome.action is Action.LINK_REFUSED
    assert outcome.failure == "ProviderRejected"
    assert outcome.message == operator_sync.LINK_REJECTED_MESSAGE
    assert operator_sync.current_refs(operator) == {}
    operator.refresh_from_db()
    assert operator.enrolment_status == EnrolmentStatus.NOT_ENROLLED
    assert operator.sync_status == SyncStatus.AWAITING_LINK
    assert not AuditLog.objects.filter(action="operator.link_set").exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("customer_id", "method_id"),
    [
        ("cus 0001", "pm_1"),
        ("cus_1", "pm/1"),
        ("", "pm_1"),
        ("cus_1", "x" * 256),
        ("cus_1\n", "pm_1"),
        ("cus_1", "4111111111111111;DROP"),
    ],
)
def test_link_refuses_a_malformed_identifier_without_a_call(
    actor: User, fake: FakeProvider, customer_id: str, method_id: str
) -> None:
    operator = create(actor, email=None)
    outcome = operator_sync.link_operator(
        operator.pk,
        customer_external_id=customer_id,
        payment_method_external_id=method_id,
        provider=fake,
        actor=actor,
    )
    assert outcome.action is Action.LINK_REFUSED
    assert fake.calls == []
    assert operator_sync.current_refs(operator) == {}


@pytest.mark.django_db
def test_link_to_a_different_customer_than_the_current_one_is_refused(
    actor: User, fake: FakeProvider
) -> None:
    operator = create(actor)
    operator_sync.sync_operator(operator.pk, provider=fake, actor=actor)
    add_provider_customer(fake, "cus_someone_else", "")
    calls = len(fake.calls)

    outcome = operator_sync.link_operator(
        operator.pk,
        customer_external_id="cus_someone_else",
        payment_method_external_id="pm_1",
        provider=fake,
        actor=actor,
    )

    assert outcome.failure == operator_sync.ALREADY_LINKED
    assert len(fake.calls) == calls


@pytest.mark.django_db
def test_link_adds_a_payment_method_to_a_synced_operator_and_replaces_an_old_one(
    actor: User, fake: FakeProvider
) -> None:
    operator = create(actor)
    operator_sync.sync_operator(operator.pk, provider=fake, actor=actor)
    customer = operator_sync.current_refs(operator)["customer"]
    for method in ("pm_first", "pm_second"):
        outcome = operator_sync.link_operator(
            operator.pk,
            customer_external_id=customer,
            payment_method_external_id=method,
            provider=fake,
            actor=actor,
        )
        assert outcome.action is Action.LINK_SET
    assert operator_sync.current_refs(operator)["payment_method"] == "pm_second"
    old = ExternalRef.objects.get(external_id="pm_first")
    assert old.superseded_at is not None


# --- Row 8: audit ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_every_action_writes_one_audit_row_with_actor_and_before_after(
    actor: User, fake: FakeProvider
) -> None:
    with_email = create(actor)
    operator_sync.sync_operator(with_email.pk, provider=fake, actor=actor)
    operator_sync.update_operator(with_email.pk, fields(city="Newtown"), actor=actor)
    operator_sync.archive_operator(with_email.pk, actor=actor)

    failing = create(actor, email="fails@example.com")
    fake.fail_next("find_customers", ProviderUnavailable(503))
    operator_sync.sync_operator(failing.pk, provider=fake, actor=actor)

    provider = FuzzyFakeProvider()
    add_provider_customer(provider, "cus_linked_0001", "linked@example.com")
    linked = create(actor, email="linked@example.com")
    operator_sync.sync_operator(linked.pk, provider=provider, actor=actor)

    add_provider_customer(fake, "cus_manual_0001", "")
    manual = create(actor, email=None)
    operator_sync.link_operator(
        manual.pk,
        customer_external_id="cus_manual_0001",
        payment_method_external_id="pm_1",
        provider=fake,
        actor=actor,
    )

    rows = list(AuditLog.objects.order_by("id"))
    assert [row.action for row in rows] == [
        "operator.created",
        "operator.sync_created",
        "operator.updated",
        "operator.archived",
        "operator.created",
        "operator.sync_failed",
        "operator.created",
        "operator.sync_linked",
        "operator.created",
        "operator.link_set",
    ]
    for row in rows:
        assert row.actor_id == actor.pk
        assert row.entity_type == "operator"
        assert row.after is not None
    created = rows[0]
    assert created.before is None
    assert created.after is not None and created.after["display_name"] == "Northwind Stores"
    updated = rows[2]
    assert updated.before is not None and updated.after is not None
    assert (updated.before["city"], updated.after["city"]) == ("Testville", "Newtown")
    archived = rows[3]
    assert archived.before is not None and archived.after is not None
    assert archived.before["archived_at"] is None and archived.after["archived_at"]
    assert rows[5].after is not None and rows[5].after["sync_failure"] == "ProviderUnavailable"
    # Identifiers in the audit log are masked.
    link = rows[-1]
    assert link.after is not None
    assert link.after["customer"] == "cus_…0001"
    assert "cus_manual_0001" not in str(link.after)


@pytest.mark.django_db
def test_an_edit_that_changes_nothing_writes_no_audit_row(actor: User) -> None:
    operator = create(actor)
    operator_sync.update_operator(operator.pk, fields(), actor=actor)
    assert list(AuditLog.objects.values_list("action", flat=True)) == ["operator.created"]


@pytest.mark.django_db
def test_archived_operator_cannot_be_updated_synced_or_linked(
    actor: User, fake: FakeProvider
) -> None:
    operator = create(actor)
    operator_sync.archive_operator(operator.pk, actor=actor)
    with pytest.raises(operator_sync.OperatorArchived):
        operator_sync.update_operator(operator.pk, fields(city="X"), actor=actor)
    assert operator_sync.sync_operator(
        operator.pk, provider=fake, actor=actor, retry=True
    ).action is (Action.NO_CALL)
    outcome = operator_sync.link_operator(
        operator.pk,
        customer_external_id="cus_1",
        payment_method_external_id="pm_1",
        provider=fake,
        actor=actor,
    )
    assert outcome.failure == operator_sync.ARCHIVED
    assert fake.calls == []


@pytest.fixture(autouse=True)
def _no_real_provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fail loudly if anything here reaches for the configured provider."""

    def refuse() -> None:
        raise AssertionError("tests must pass a fake provider explicitly")

    monkeypatch.setattr("payments.get_provider", refuse)
    yield


# --- PLAN-006a §4 row 4: the retry lock ------------------------------------------------------


def failed_operator(actor: User, fake: FakeProvider, seconds_ago: float) -> Operator:
    """An operator whose last attempt failed ``seconds_ago`` seconds ago."""
    operator = create(actor)
    fake.fail_next("find_customers", ProviderUnavailable(503))
    operator_sync.sync_operator(operator.pk, provider=fake, actor=actor)
    Operator.objects.filter(pk=operator.pk).update(
        sync_attempted_at=timezone.now() - timedelta(seconds=seconds_ago)
    )
    fake.calls.clear()
    return operator


@pytest.mark.django_db
@pytest.mark.parametrize("seconds_ago", [0, 1, 30, 59])
def test_a_retry_within_60_seconds_is_refused_with_no_call(
    actor: User, fake: FakeProvider, seconds_ago: int
) -> None:
    operator = failed_operator(actor, fake, seconds_ago)
    audit_rows = AuditLog.objects.count()

    outcome = operator_sync.sync_operator(operator.pk, provider=fake, actor=actor, retry=True)

    assert outcome.action is Action.RETRY_TOO_SOON
    assert outcome.status == SyncStatus.FAILED
    assert 1 <= outcome.retry_in <= 60 - seconds_ago
    assert outcome.message == f"Retry available in {outcome.retry_in} seconds."
    assert fake.calls == []
    operator.refresh_from_db()
    assert operator.sync_status == SyncStatus.FAILED
    assert AuditLog.objects.count() == audit_rows


@pytest.mark.django_db
@pytest.mark.parametrize("seconds_ago", [60, 61, 3600])
def test_a_retry_at_or_after_60_seconds_is_allowed(
    actor: User, fake: FakeProvider, seconds_ago: int
) -> None:
    operator = failed_operator(actor, fake, seconds_ago)
    outcome = operator_sync.sync_operator(operator.pk, provider=fake, actor=actor, retry=True)
    assert outcome.action is Action.CREATED
    assert call_names(fake) == ["find_customers", "create_customer"]


def test_retry_wait_seconds_at_the_boundaries() -> None:
    now = timezone.now()
    operator = Operator(display_name="x")
    assert operator_sync.retry_wait_seconds(operator, now) == 0
    for elapsed, expected in [(0, 60), (0.2, 60), (59, 1), (59.9, 1), (60, 0), (61, 0)]:
        operator.sync_attempted_at = now - timedelta(seconds=elapsed)
        assert operator_sync.retry_wait_seconds(operator, now) == expected, elapsed
    # An attempt time in the future (clock skew) never locks longer than the lock.
    operator.sync_attempted_at = now + timedelta(seconds=30)
    assert operator_sync.retry_wait_seconds(operator, now) == operator_sync.RETRY_LOCK_SECONDS


@pytest.mark.django_db
def test_a_plain_sync_is_not_affected_by_the_retry_lock(actor: User, fake: FakeProvider) -> None:
    operator = create(actor)
    Operator.objects.filter(pk=operator.pk).update(sync_attempted_at=timezone.now())
    outcome = operator_sync.sync_operator(operator.pk, provider=fake, actor=actor)
    assert outcome.action is Action.CREATED


class SlowFailingProvider(FakeProvider):
    """Every lookup is slow and fails, so the row stays ``failed`` after each attempt."""

    def find_customers(self, query: CustomerQuery) -> list[CustomerRecord]:
        self._call("find_customers", query)
        time.sleep(0.3)
        raise ProviderUnavailable(503)


@requires_postgresql
@pytest.mark.django_db(transaction=True)
def test_two_concurrent_retries_make_one_call(actor: User) -> None:
    provider = SlowFailingProvider()
    operator = create(actor)
    Operator.objects.filter(pk=operator.pk).update(sync_status=SyncStatus.FAILED)
    barrier = threading.Barrier(2)
    outcomes: list[Action] = []

    def retry() -> None:
        try:
            barrier.wait()
            outcomes.append(
                operator_sync.sync_operator(
                    operator.pk, provider=provider, actor=actor, retry=True
                ).action
            )
        finally:
            connection.close()

    threads = [threading.Thread(target=retry) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == sorted([Action.FAILED, Action.RETRY_TOO_SOON])
    assert call_names(provider) == ["find_customers"]


# --- PLAN-006a §4 row 6: stored field names -----------------------------------------------------


@pytest.mark.django_db
def test_rejected_field_names_are_stored_audited_and_cleared_on_success(
    actor: User, fake: FakeProvider
) -> None:
    operator = create(actor)
    fake.fail_next(
        "create_customer",
        ProviderRejected(422, "CANARY provider text", error_fields=("email", "lastname")),
    )
    outcome = operator_sync.sync_operator(operator.pk, provider=fake, actor=actor)

    operator.refresh_from_db()
    assert outcome.failure == "ProviderRejected"
    assert operator.sync_failure_fields == "email,lastname"
    audit = AuditLog.objects.get(action="operator.sync_failed")
    assert audit.after is not None
    assert audit.after["sync_failure_fields"] == "email,lastname"
    assert "CANARY" not in str(audit.after) and "CANARY" not in outcome.message

    Operator.objects.filter(pk=operator.pk).update(
        sync_attempted_at=timezone.now() - timedelta(seconds=operator_sync.RETRY_LOCK_SECONDS)
    )
    outcome = operator_sync.sync_operator(operator.pk, provider=fake, actor=actor, retry=True)
    operator.refresh_from_db()
    assert outcome.action is Action.CREATED
    assert (operator.sync_failure, operator.sync_failure_fields) == ("", "")
    created = AuditLog.objects.get(action="operator.sync_created")
    assert created.after is not None and created.after["sync_failure_fields"] == ""


@pytest.mark.django_db
def test_a_failure_other_than_rejected_stores_no_field_names(
    actor: User, fake: FakeProvider
) -> None:
    operator = create(actor)
    fake.fail_next("create_customer", ProviderRejected(422, error_fields=("email",)))
    operator_sync.sync_operator(operator.pk, provider=fake, actor=actor)
    Operator.objects.filter(pk=operator.pk).update(sync_attempted_at=None)
    fake.fail_next("find_customers", ProviderUnavailable(503))
    operator_sync.sync_operator(operator.pk, provider=fake, actor=actor, retry=True)
    operator.refresh_from_db()
    assert (operator.sync_failure, operator.sync_failure_fields) == ("ProviderUnavailable", "")


@pytest.mark.django_db
def test_removing_the_email_or_linking_clears_the_field_names(
    actor: User, fake: FakeProvider
) -> None:
    for clear in ("remove_email", "link"):
        operator = create(actor, email=f"{clear}@example.com")
        fake.fail_next("create_customer", ProviderRejected(422, error_fields=("email",)))
        operator_sync.sync_operator(operator.pk, provider=fake, actor=actor)
        if clear == "remove_email":
            operator_sync.update_operator(operator.pk, fields(email=None), actor=actor)
        else:
            add_provider_customer(fake, "cus_clear_0001", "")
            operator_sync.link_operator(
                operator.pk,
                customer_external_id="cus_clear_0001",
                payment_method_external_id="pm_clear",
                provider=fake,
                actor=actor,
            )
        operator.refresh_from_db()
        assert operator.sync_failure_fields == "", clear


@pytest.mark.django_db
def test_the_field_names_column_is_guarded_against_card_numbers(actor: User) -> None:
    operator = create(actor)
    operator.sync_failure_fields = "email,4111111111111111"
    with pytest.raises(ValidationError):
        operator.save()


@requires_postgresql
@pytest.mark.django_db
def test_the_database_trigger_guards_the_field_names_column(actor: User) -> None:
    operator = create(actor)
    with pytest.raises(IntegrityError), transaction.atomic():
        Operator.objects.filter(pk=operator.pk).update(
            sync_failure_fields="email,4111 1111 1111 1111"
        )


@pytest.mark.django_db
def test_an_end_to_end_422_through_the_real_transport_stores_names_only(
    actor: User, fake_stax_api_key: str, caplog: pytest.LogCaptureFixture
) -> None:
    marker = "VALUE-MARKER-0c9a"
    card = "4111111111111111"
    rig = make_rig(
        sequence(
            [
                httpx.Response(200, json={"data": []}),
                httpx.Response(
                    422,
                    json={
                        "email": [f"{marker} {card}"],
                        "lastname": [f"{marker} {fake_stax_api_key}"],
                    },
                ),
            ]
        )
    )
    operator = create(actor)
    with override_settings(STAX_CALLER="worker"), caplog.at_level(logging.DEBUG):
        outcome = operator_sync.sync_operator(
            operator.pk, provider=StaxProvider(rig.transport), actor=actor
        )

    operator.refresh_from_db()
    assert outcome.failure == "ProviderRejected"
    assert operator.sync_failure_fields == "email,lastname"
    assert "error_fields=email,lastname" in caplog.text
    audit = AuditLog.objects.get(action="operator.sync_failed")
    stored = " ".join(
        [str(audit.before), str(audit.after), operator.sync_failure_fields, outcome.message]
    )
    for forbidden in (marker, card, fake_stax_api_key):
        assert forbidden not in stored
        assert forbidden not in caplog.text

"""The lease sync service (docs/qa/PLAN-014 C3; before it, the operator sync of
docs/qa/PLAN-006-operators.md §6 rows 1, 2, 3, 5 and 8 and PLAN-006c §2 rows 1 to 6).

Since PLAN-014 the processor customer is per lease: every row here bills a lease (an
operator made through :mod:`ledger.services.operators`, a store and a lease, with the
billing details written by :func:`payments.lease_sync.update_billing`) and syncs it with
:func:`payments.lease_sync.sync_lease` / :func:`payments.lease_sync.link_lease`. The
operator's own writes (the creation-key replay, the archive) are local and call nothing.

Every provider here is a :class:`~payments.fake.FakeProvider` or a subclass defined in
this file; nothing opens a socket.
"""

from __future__ import annotations

import importlib
import itertools
import json
import logging
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings
from django.utils import timezone

from accounts.roles import Role
from ledger.models import (
    AuditLog,
    Lease,
    LeaseBilling,
    Operator,
    Store,
    SyncFailure,
    SyncStatus,
)
from ledger.services import operators
from payments import lease_sync
from payments.errors import (
    CircuitOpen,
    ProviderError,
    ProviderRejected,
    ProviderUnavailable,
    TransportError,
)
from payments.fake import FakeProvider
from payments.lease_sync import Action
from payments.models import ExternalRef, Job, JobKind, JobStatus
from payments.provider import CustomerQuery, CustomerRecord, mask_identifier
from payments.stax.adapter import StaxProvider
from tests.auth_helpers import make_user
from tests.stax_helpers import make_rig, sequence
from tests.text_helpers import has_token

requires_postgresql = pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="row locks and concurrent transactions need PostgreSQL",
)

EMAIL = "ops@example.com"

#: Store numbers for the stores made here (the reference sent to the provider).
_STORE_NUMBERS = itertools.count(7001)


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
    """The operator's fields, as the operator form posts them (PLAN-014 C1: the Full name
    is the display name; no type, company or legal name)."""
    values: dict[str, Any] = {
        "display_name": "Northwind Stores",
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


def billing_fields(**overrides: Any) -> dict[str, Any]:
    """A lease's billing details, as the Edit billing details form posts them."""
    values: dict[str, Any] = {
        "company_name": "Northwind Stores LLC",
        "email": EMAIL,
        "phone": "+1 555 010 0001",
        "address_line1": "1 Demo Avenue",
        "address_line2": "",
        "city": "Testville",
        "state": "TX",
        "postal_code": "75001",
    }
    return {**values, **overrides}


def customer_refs(lease: Lease) -> list[ExternalRef]:
    """The lease's own processor customer references (PLAN-014 C3: per lease)."""
    return list(
        ExternalRef.objects.filter(
            entity_type="lease", entity_id=lease.pk, external_kind="customer"
        )
    )


def billed_lease(
    actor: User | None = None,
    *,
    operator: Operator | None = None,
    name: str = "Northwind Stores",
    **billing: Any,
) -> Lease:
    """A lease of ``operator`` (a new one named ``name`` when none is given, through
    :func:`ledger.services.operators.create_operator`) on a store of its own, its billing
    details :func:`billing_fields` with ``billing`` written by
    :func:`payments.lease_sync.update_billing`: ``pending`` with a billing e-mail,
    ``awaiting_link`` without one. Nothing calls the provider."""
    if operator is None:
        operator, created = operators.create_operator(
            fields(display_name=name, email=None), creation_key=uuid.uuid4(), actor=actor
        )
        assert created
    number = next(_STORE_NUMBERS)
    store = Store.objects.create(
        external_label=f"Store {number}",
        store_number=number,
        open_date=date(2024, 1, 1),
        needs_open_date=False,
        status="active",
    )
    lease = Lease.objects.create(
        store=store,
        operator=operator,
        debit_day=5,
        collection_status="active",
        payment_channel="stax",
    )
    lease_sync.update_billing(lease.pk, billing_fields(**billing), actor=actor)
    return Lease.objects.select_related("operator", "store", "billing").get(pk=lease.pk)


def billing_row(lease: Lease) -> LeaseBilling:
    return LeaseBilling.objects.get(lease=lease)


@pytest.fixture
def actor() -> User:
    user, _ = make_user("billing-sync", Role.BILLING)
    return user


@pytest.fixture
def fake() -> FakeProvider:
    return FakeProvider()


def create(actor: User, **billing: Any) -> Lease:
    return billed_lease(actor, **billing)


# --- Row 1: one provider customer however often the create is sent ---------------------


@pytest.mark.django_db
def test_the_same_creation_key_twice_gives_one_operator_and_its_lease_one_customer(
    actor: User, fake: FakeProvider
) -> None:
    key = uuid.uuid4()
    first, created_first = operators.create_operator(fields(), creation_key=key, actor=actor)
    second, created_second = operators.create_operator(
        fields(display_name="Replayed with other values"), creation_key=key, actor=actor
    )

    assert (created_first, created_second) == (True, False)
    assert second.pk == first.pk
    assert Operator.objects.count() == 1
    assert Operator.objects.get().display_name == "Northwind Stores"
    # PLAN-014 C3: saving an operator never reaches the provider.
    assert fake.calls == []
    assert Operator.objects.get().sync_status == SyncStatus.NOT_APPLICABLE

    lease = billed_lease(actor, operator=first)
    lease_sync.sync_lease(lease.pk, provider=fake, actor=actor)
    lease_sync.sync_lease(lease.pk, provider=fake, actor=actor)
    assert call_names(fake).count("create_customer") == 1
    assert len(customer_refs(lease)) == 1


@pytest.mark.django_db
def test_a_linked_lease_is_never_synced_again(actor: User, fake: FakeProvider) -> None:
    lease = create(actor)
    lease_sync.sync_lease(lease.pk, provider=fake, actor=actor)
    calls = len(fake.calls)
    for retry in (False, True):
        outcome = lease_sync.sync_lease(lease.pk, provider=fake, actor=actor, retry=retry)
        assert outcome.action is Action.NO_CALL
    assert len(fake.calls) == calls


class SlowFakeProvider(FakeProvider):
    """Holds each lookup long enough for a second request to arrive meanwhile."""

    def find_customers(self, query: CustomerQuery) -> list[CustomerRecord]:
        time.sleep(0.3)
        return super().find_customers(query)


@requires_postgresql
@pytest.mark.django_db(transaction=True)
def test_concurrent_creates_with_one_key_make_one_operator_and_no_call(actor: User) -> None:
    provider = SlowFakeProvider()
    key = uuid.uuid4()
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []
    made: list[uuid.UUID] = []

    def submit() -> None:
        try:
            barrier.wait()
            operator, _ = operators.create_operator(fields(), creation_key=key, actor=actor)
            made.append(operator.pk)
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
    assert made == [Operator.objects.get().pk] * 2
    assert provider.calls == []


@requires_postgresql
@pytest.mark.django_db(transaction=True)
def test_concurrent_syncs_of_one_lease_call_the_provider_once(actor: User) -> None:
    provider = SlowFakeProvider()
    lease = create(actor)
    barrier = threading.Barrier(3)
    outcomes: list[Action] = []

    def sync() -> None:
        try:
            barrier.wait()
            outcomes.append(lease_sync.sync_lease(lease.pk, provider=provider, actor=actor).action)
        finally:
            connection.close()

    threads = [threading.Thread(target=sync) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == sorted([Action.CREATED, Action.NO_CALL, Action.NO_CALL])
    assert call_names(provider) == ["find_customers", "create_customer"]
    assert len(customer_refs(lease)) == 1


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
    lease = create(actor)
    fake.fail_next(method, error)

    outcome = lease_sync.sync_lease(lease.pk, provider=fake, actor=actor)

    lease.refresh_from_db()
    assert outcome.action is Action.FAILED
    assert (lease.sync_status, lease.sync_failure) == (SyncStatus.FAILED, name)
    assert lease.sync_attempted_at is not None
    assert customer_refs(lease) == []
    assert billing_row(lease).email == EMAIL
    assert "CANARY" not in outcome.message
    assert outcome.message == lease_sync.MESSAGES[name]
    audit = AuditLog.objects.get(action="lease.sync_failed")
    assert "CANARY" not in str(audit.after)


@pytest.mark.django_db
def test_a_failed_lease_is_not_called_again_until_retry(actor: User, fake: FakeProvider) -> None:
    lease = create(actor)
    fake.fail_next("find_customers", ProviderUnavailable(503))
    lease_sync.sync_lease(lease.pk, provider=fake, actor=actor)
    assert len(fake.calls) == 1

    # A plain sync (a page load, an edit) does nothing.
    assert lease_sync.sync_lease(lease.pk, provider=fake, actor=actor).action is (Action.NO_CALL)
    lease_sync.update_billing(lease.pk, billing_fields(phone="+1 555 010 0002"), actor=actor)
    assert lease_sync.sync_lease(lease.pk, provider=fake, actor=actor).action is (Action.NO_CALL)
    assert len(fake.calls) == 1

    # The retry, once the lock has passed: one lookup and one create, then linked.
    Lease.objects.filter(pk=lease.pk).update(
        sync_attempted_at=timezone.now() - timedelta(seconds=lease_sync.RETRY_LOCK_SECONDS)
    )
    outcome = lease_sync.sync_lease(lease.pk, provider=fake, actor=actor, retry=True)
    assert outcome.action is Action.CREATED
    assert call_names(fake) == ["find_customers", "find_customers", "create_customer"]
    lease.refresh_from_db()
    assert (lease.sync_status, lease.sync_failure) == (SyncStatus.LINKED, "")


def test_an_unknown_provider_error_subclass_maps_to_its_nearest_known_class() -> None:
    class Unforeseen(ProviderUnavailable):
        pass

    assert lease_sync.failure_name(Unforeseen(502)) == "ProviderUnavailable"
    assert lease_sync.failure_name(ProviderError("x")) == "ProviderError"


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
    lease = create(actor)

    outcome = lease_sync.sync_lease(lease.pk, provider=provider, actor=actor)

    assert outcome.action is Action.LINKED_EXISTING
    assert "create_customer" not in call_names(provider)
    [ref] = customer_refs(lease)
    assert ref.external_id == "cus_existing_0001"
    lease.refresh_from_db()
    assert (lease.sync_status, lease.needs_review) == (SyncStatus.LINKED, False)


@pytest.mark.django_db
def test_two_matches_link_the_oldest_and_flag_for_review(actor: User) -> None:
    provider = FuzzyFakeProvider()
    add_provider_customer(provider, "cus_newer_00001", EMAIL, "2026-05-01 10:00:00")
    add_provider_customer(provider, "cus_oldest_0001", "OPS@example.com", "2024-01-01 09:00:00")
    add_provider_customer(provider, "cus_no_date_001", EMAIL)
    lease = create(actor)

    outcome = lease_sync.sync_lease(lease.pk, provider=provider, actor=actor)

    assert outcome.action is Action.LINKED_EXISTING
    assert outcome.needs_review is True
    [ref] = customer_refs(lease)
    assert ref.external_id == "cus_oldest_0001"
    lease.refresh_from_db()
    assert lease.needs_review is True
    assert "create_customer" not in call_names(provider)
    audit = AuditLog.objects.get(action="lease.sync_linked")
    assert audit.after is not None and audit.after["matches"] == 3


@pytest.mark.django_db
def test_a_near_miss_email_is_not_a_match(actor: User) -> None:
    provider = FuzzyFakeProvider()
    add_provider_customer(provider, "cus_near_000001", "ops@example.com.")
    add_provider_customer(provider, "cus_near_000002", "xops@example.com")
    lease = create(actor)

    outcome = lease_sync.sync_lease(lease.pk, provider=provider, actor=actor)

    assert outcome.action is Action.CREATED
    assert call_names(provider).count("create_customer") == 1


@pytest.mark.django_db
def test_a_match_already_linked_to_another_lease_is_not_reused(actor: User) -> None:
    provider = FuzzyFakeProvider()
    add_provider_customer(provider, "cus_taken_00001", EMAIL)
    other = create(actor, email="other@example.com")
    ExternalRef.objects.create(
        entity_type="lease",
        entity_id=other.pk,
        provider=lease_sync.PROVIDER_KEY,
        external_kind="customer",
        external_id="cus_taken_00001",
    )
    lease = create(actor)

    outcome = lease_sync.sync_lease(lease.pk, provider=provider, actor=actor)

    assert outcome.action is Action.FAILED
    assert outcome.failure == SyncFailure.ALREADY_LINKED_ELSEWHERE
    lease.refresh_from_db()
    assert lease.needs_review is True
    assert "create_customer" not in call_names(provider)


@pytest.mark.django_db
def test_the_create_carries_our_reference_and_the_c3_mapping(
    actor: User, fake: FakeProvider
) -> None:
    lease = billed_lease(actor, name="Ana Maria Lopez")
    lease_sync.sync_lease(lease.pk, provider=fake, actor=actor)
    [(name, (customer,))] = [call for call in fake.calls if call[0] == "create_customer"]
    assert name == "create_customer"
    # The operator's full name split on the last space; the lease's company and e-mail;
    # the store number as our reference.
    assert customer.first_name == "Ana Maria"  # type: ignore[attr-defined]
    assert customer.last_name == "Lopez"  # type: ignore[attr-defined]
    assert customer.company_name == "Northwind Stores LLC"  # type: ignore[attr-defined]
    assert customer.operator_email == EMAIL  # type: ignore[attr-defined]
    assert customer.reference == str(lease.store.store_number)  # type: ignore[attr-defined]


# --- Row 5: no e-mail, and the manual link ----------------------------------------------


@pytest.mark.django_db
def test_no_email_saves_awaiting_link_and_calls_nothing(actor: User, fake: FakeProvider) -> None:
    lease = create(actor, email=None)
    assert lease.sync_status == SyncStatus.AWAITING_LINK
    outcome = lease_sync.sync_lease(lease.pk, provider=fake, actor=actor)
    assert outcome.action is Action.NO_CALL
    assert fake.calls == []


@pytest.mark.django_db
def test_link_stores_both_ids_after_one_read_and_connects(actor: User, fake: FakeProvider) -> None:
    add_provider_customer(fake, "cus_manual_0001", "")
    lease = create(actor, email=None)

    outcome = lease_sync.link_lease(
        lease.pk,
        customer_external_id="cus_manual_0001",
        payment_method_external_id="pm_bank-0001",
        provider=fake,
        actor=actor,
    )

    assert outcome.action is Action.LINK_SET
    assert outcome.message == "Connected to Stax. Autopay can be set up."
    assert fake.calls == [("get_customer", ("cus_manual_0001",))]
    refs = lease_sync.current_refs(lease)
    assert refs == {"customer": "cus_manual_0001", "payment_method": "pm_bank-0001"}
    lease.refresh_from_db()
    assert lease.sync_status == SyncStatus.LINKED
    assert AuditLog.objects.filter(action="lease.link_set", entity_id=str(lease.pk)).count() == 1


@pytest.mark.django_db
def test_a_bad_customer_id_stores_nothing(actor: User, fake: FakeProvider) -> None:
    lease = create(actor, email=None)

    outcome = lease_sync.link_lease(
        lease.pk,
        customer_external_id="cus_missing_001",
        payment_method_external_id="pm_bank-0001",
        provider=fake,
        actor=actor,
    )

    assert outcome.action is Action.LINK_REFUSED
    assert outcome.failure == "ProviderRejected"
    assert outcome.message == lease_sync.LINK_REJECTED_MESSAGE
    assert lease_sync.current_refs(lease) == {}
    lease.refresh_from_db()
    assert lease.sync_status == SyncStatus.AWAITING_LINK
    assert not AuditLog.objects.filter(action="lease.link_set").exists()


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
    lease = create(actor, email=None)
    outcome = lease_sync.link_lease(
        lease.pk,
        customer_external_id=customer_id,
        payment_method_external_id=method_id,
        provider=fake,
        actor=actor,
    )
    assert outcome.action is Action.LINK_REFUSED
    assert fake.calls == []
    assert lease_sync.current_refs(lease) == {}


@pytest.mark.django_db
def test_link_to_a_different_customer_than_the_current_one_is_refused(
    actor: User, fake: FakeProvider
) -> None:
    lease = create(actor)
    lease_sync.sync_lease(lease.pk, provider=fake, actor=actor)
    add_provider_customer(fake, "cus_someone_else", "")
    calls = len(fake.calls)

    outcome = lease_sync.link_lease(
        lease.pk,
        customer_external_id="cus_someone_else",
        payment_method_external_id="pm_1",
        provider=fake,
        actor=actor,
    )

    assert outcome.failure == lease_sync.ALREADY_LINKED
    assert len(fake.calls) == calls


@pytest.mark.django_db
def test_link_adds_a_payment_method_to_a_synced_lease_and_replaces_an_old_one(
    actor: User, fake: FakeProvider
) -> None:
    lease = create(actor)
    lease_sync.sync_lease(lease.pk, provider=fake, actor=actor)
    customer = lease_sync.current_refs(lease)["customer"]
    for method in ("pm_first", "pm_second"):
        outcome = lease_sync.link_lease(
            lease.pk,
            customer_external_id=customer,
            payment_method_external_id=method,
            provider=fake,
            actor=actor,
        )
        assert outcome.action is Action.LINK_SET
    assert lease_sync.current_refs(lease)["payment_method"] == "pm_second"
    old = ExternalRef.objects.get(external_id="pm_first")
    assert old.superseded_at is not None


# --- Row 8: audit ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_every_action_writes_one_audit_row_with_actor_and_before_after(
    actor: User, fake: FakeProvider
) -> None:
    with_email = create(actor)
    assert with_email.operator is not None
    lease_sync.sync_lease(with_email.pk, provider=fake, actor=actor)
    lease_sync.update_billing(with_email.pk, billing_fields(city="Newtown"), actor=actor)
    operators.archive_operator(with_email.operator.pk, actor=actor)

    failing = create(actor, email="fails@example.com")
    fake.fail_next("find_customers", ProviderUnavailable(503))
    lease_sync.sync_lease(failing.pk, provider=fake, actor=actor)

    provider = FuzzyFakeProvider()
    add_provider_customer(provider, "cus_linked_0001", "linked@example.com")
    linked = create(actor, email="linked@example.com")
    lease_sync.sync_lease(linked.pk, provider=provider, actor=actor)

    add_provider_customer(fake, "cus_manual_0001", "")
    manual = create(actor, email=None)
    lease_sync.link_lease(
        manual.pk,
        customer_external_id="cus_manual_0001",
        payment_method_external_id="pm_1",
        provider=fake,
        actor=actor,
    )

    rows = list(AuditLog.objects.order_by("id"))
    assert [row.action for row in rows] == [
        "operator.created",
        "lease_billing.changed",
        "lease.sync_created",
        "lease_billing.changed",
        "operator.archived",
        "operator.created",
        "lease_billing.changed",
        "lease.sync_failed",
        "operator.created",
        "lease_billing.changed",
        "lease.sync_linked",
        "operator.created",
        "lease_billing.changed",
        "lease.link_set",
    ]
    for row in rows:
        assert row.actor_id == actor.pk
        expected = "operator" if row.action.startswith("operator.") else "lease"
        assert row.entity_type == expected, row.action
        assert row.after is not None
    created = rows[0]
    assert created.before is None
    assert created.after is not None and created.after["display_name"] == "Northwind Stores"
    assert rows[2].entity_id == str(with_email.pk)
    updated = rows[3]
    assert updated.before is not None and updated.after is not None
    assert (updated.before["city"], updated.after["city"]) == ("Testville", "Newtown")
    archived = rows[4]
    assert archived.entity_id == str(with_email.operator.pk)
    assert archived.before is not None and archived.after is not None
    assert archived.before["archived_at"] is None and archived.after["archived_at"]
    assert rows[7].after is not None and rows[7].after["sync_failure"] == "ProviderUnavailable"
    # Identifiers in the audit log are masked.
    link = rows[-1]
    assert link.after is not None
    assert link.after["customer"] == "cus_…0001"
    assert "cus_manual_0001" not in str(link.after)


@pytest.mark.django_db
def test_an_edit_that_changes_nothing_writes_no_audit_row(actor: User) -> None:
    lease = create(actor)
    assert lease.operator is not None
    operators.update_operator(lease.operator.pk, fields(email=None), actor=actor)
    lease_sync.update_billing(lease.pk, billing_fields(), actor=actor)
    assert list(AuditLog.objects.order_by("id").values_list("action", flat=True)) == [
        "operator.created",
        "lease_billing.changed",
    ]


@pytest.mark.django_db
def test_archived_operator_cannot_be_updated_and_its_lease_not_synced_or_linked(
    actor: User, fake: FakeProvider
) -> None:
    lease = create(actor)
    assert lease.operator is not None
    operators.archive_operator(lease.operator.pk, actor=actor)
    with pytest.raises(operators.OperatorArchived):
        operators.update_operator(lease.operator.pk, fields(city="X"), actor=actor)
    # Fable's C part 1 finding: the service refuses the billing edit too, whoever calls it.
    status, audits = Lease.objects.get(pk=lease.pk).sync_status, AuditLog.objects.count()
    with pytest.raises(lease_sync.OperatorArchived):
        lease_sync.update_billing(
            lease.pk, billing_fields(city="X", email="moved@example.com"), actor=actor
        )
    assert (billing_row(lease).city, billing_row(lease).email) == ("Testville", EMAIL)
    assert Lease.objects.get(pk=lease.pk).sync_status == status
    assert AuditLog.objects.count() == audits
    assert lease_sync.sync_lease(lease.pk, provider=fake, actor=actor, retry=True).action is (
        Action.NO_CALL
    )
    outcome = lease_sync.link_lease(
        lease.pk,
        customer_external_id="cus_1",
        payment_method_external_id="pm_1",
        provider=fake,
        actor=actor,
    )
    assert outcome.failure == lease_sync.ARCHIVED
    assert fake.calls == []


@pytest.fixture(autouse=True)
def _no_real_provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fail loudly if anything here reaches for the configured provider."""

    def refuse() -> None:
        raise AssertionError("tests must pass a fake provider explicitly")

    monkeypatch.setattr("payments.get_provider", refuse)
    yield


# --- PLAN-006a §4 row 4: the retry lock ------------------------------------------------------


def failed_lease(actor: User, fake: FakeProvider, seconds_ago: float) -> Lease:
    """A lease whose last attempt failed ``seconds_ago`` seconds ago."""
    lease = create(actor)
    fake.fail_next("find_customers", ProviderUnavailable(503))
    lease_sync.sync_lease(lease.pk, provider=fake, actor=actor)
    Lease.objects.filter(pk=lease.pk).update(
        sync_attempted_at=timezone.now() - timedelta(seconds=seconds_ago)
    )
    fake.calls.clear()
    return lease


@pytest.mark.django_db
@pytest.mark.parametrize("seconds_ago", [0, 1, 30, 59])
def test_a_retry_within_60_seconds_is_refused_with_no_call(
    actor: User, fake: FakeProvider, seconds_ago: int
) -> None:
    lease = failed_lease(actor, fake, seconds_ago)
    audit_rows = AuditLog.objects.count()

    outcome = lease_sync.sync_lease(lease.pk, provider=fake, actor=actor, retry=True)

    assert outcome.action is Action.RETRY_TOO_SOON
    assert outcome.status == SyncStatus.FAILED
    assert 1 <= outcome.retry_in <= 60 - seconds_ago
    assert outcome.message == f"Retry available in {outcome.retry_in} seconds."
    assert fake.calls == []
    lease.refresh_from_db()
    assert lease.sync_status == SyncStatus.FAILED
    assert AuditLog.objects.count() == audit_rows


@pytest.mark.django_db
@pytest.mark.parametrize("seconds_ago", [60, 61, 3600])
def test_a_retry_at_or_after_60_seconds_is_allowed(
    actor: User, fake: FakeProvider, seconds_ago: int
) -> None:
    lease = failed_lease(actor, fake, seconds_ago)
    outcome = lease_sync.sync_lease(lease.pk, provider=fake, actor=actor, retry=True)
    assert outcome.action is Action.CREATED
    assert call_names(fake) == ["find_customers", "create_customer"]


def test_retry_wait_seconds_at_the_boundaries() -> None:
    now = timezone.now()
    lease = Lease()
    assert lease_sync.retry_wait_seconds(lease, now) == 0
    for elapsed, expected in [(0, 60), (0.2, 60), (59, 1), (59.9, 1), (60, 0), (61, 0)]:
        lease.sync_attempted_at = now - timedelta(seconds=elapsed)
        assert lease_sync.retry_wait_seconds(lease, now) == expected, elapsed
    # An attempt time in the future (clock skew) never locks longer than the lock.
    lease.sync_attempted_at = now + timedelta(seconds=30)
    assert lease_sync.retry_wait_seconds(lease, now) == lease_sync.RETRY_LOCK_SECONDS


@pytest.mark.django_db
def test_a_plain_sync_is_not_affected_by_the_retry_lock(actor: User, fake: FakeProvider) -> None:
    lease = create(actor)
    Lease.objects.filter(pk=lease.pk).update(sync_attempted_at=timezone.now())
    outcome = lease_sync.sync_lease(lease.pk, provider=fake, actor=actor)
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
    lease = create(actor)
    Lease.objects.filter(pk=lease.pk).update(sync_status=SyncStatus.FAILED)
    barrier = threading.Barrier(2)
    outcomes: list[Action] = []

    def retry() -> None:
        try:
            barrier.wait()
            outcomes.append(
                lease_sync.sync_lease(lease.pk, provider=provider, actor=actor, retry=True).action
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
    lease = create(actor)
    fake.fail_next(
        "create_customer",
        ProviderRejected(422, "CANARY provider text", error_fields=("email", "lastname")),
    )
    outcome = lease_sync.sync_lease(lease.pk, provider=fake, actor=actor)

    lease.refresh_from_db()
    assert outcome.failure == "ProviderRejected"
    assert lease.sync_failure_fields == "email,lastname"
    audit = AuditLog.objects.get(action="lease.sync_failed")
    assert audit.after is not None
    assert audit.after["sync_failure_fields"] == "email,lastname"
    assert "CANARY" not in str(audit.after) and "CANARY" not in outcome.message

    Lease.objects.filter(pk=lease.pk).update(
        sync_attempted_at=timezone.now() - timedelta(seconds=lease_sync.RETRY_LOCK_SECONDS)
    )
    outcome = lease_sync.sync_lease(lease.pk, provider=fake, actor=actor, retry=True)
    lease.refresh_from_db()
    assert outcome.action is Action.CREATED
    assert (lease.sync_failure, lease.sync_failure_fields) == ("", "")
    created = AuditLog.objects.get(action="lease.sync_created")
    assert created.after is not None and created.after["sync_failure_fields"] == ""


@pytest.mark.django_db
def test_a_failure_other_than_rejected_stores_no_field_names(
    actor: User, fake: FakeProvider
) -> None:
    lease = create(actor)
    fake.fail_next("create_customer", ProviderRejected(422, error_fields=("email",)))
    lease_sync.sync_lease(lease.pk, provider=fake, actor=actor)
    Lease.objects.filter(pk=lease.pk).update(sync_attempted_at=None)
    fake.fail_next("find_customers", ProviderUnavailable(503))
    lease_sync.sync_lease(lease.pk, provider=fake, actor=actor, retry=True)
    lease.refresh_from_db()
    assert (lease.sync_failure, lease.sync_failure_fields) == ("ProviderUnavailable", "")


@pytest.mark.django_db
def test_removing_the_email_or_linking_clears_the_field_names(
    actor: User, fake: FakeProvider
) -> None:
    for clear in ("remove_email", "link"):
        lease = create(actor, email=f"{clear}@example.com")
        fake.fail_next("create_customer", ProviderRejected(422, error_fields=("email",)))
        lease_sync.sync_lease(lease.pk, provider=fake, actor=actor)
        if clear == "remove_email":
            lease_sync.update_billing(lease.pk, billing_fields(email=None), actor=actor)
        else:
            add_provider_customer(fake, "cus_clear_0001", "")
            lease_sync.link_lease(
                lease.pk,
                customer_external_id="cus_clear_0001",
                payment_method_external_id="pm_clear",
                provider=fake,
                actor=actor,
            )
        lease.refresh_from_db()
        assert lease.sync_failure_fields == "", clear


@pytest.mark.django_db
def test_the_field_names_column_is_guarded_against_card_numbers(actor: User) -> None:
    lease = create(actor)
    lease.sync_failure_fields = "email,4111111111111111"
    with pytest.raises(ValidationError):
        lease.save()


@requires_postgresql
@pytest.mark.django_db
def test_the_database_trigger_guards_the_field_names_column(actor: User) -> None:
    lease = create(actor)
    with pytest.raises(IntegrityError), transaction.atomic():
        Lease.objects.filter(pk=lease.pk).update(sync_failure_fields="email,4111 1111 1111 1111")


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
    lease = create(actor)
    with override_settings(STAX_CALLER="worker"), caplog.at_level(logging.DEBUG):
        outcome = lease_sync.sync_lease(lease.pk, provider=StaxProvider(rig.transport), actor=actor)

    lease.refresh_from_db()
    assert outcome.failure == "ProviderRejected"
    assert lease.sync_failure_fields == "email,lastname"
    assert "error_fields=email,lastname" in caplog.text
    audit = AuditLog.objects.get(action="lease.sync_failed")
    stored = " ".join(
        [str(audit.before), str(audit.after), lease.sync_failure_fields, outcome.message]
    )
    for forbidden in (marker, card, fake_stax_api_key):
        assert forbidden not in stored
        assert forbidden not in caplog.text


# --- PLAN-006b: the phone rule before any call, and the stored phones ------------------------


def _sent_bodies(rig: Any) -> list[dict[str, Any]]:
    return [json.loads(request.content) for request in rig.recorder.requests if request.content]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("stored", "expected"),
    [("", None), ("+1 555 010 0001", "15550100001"), ("4155550132", "4155550132")],
    ids=["empty", "formatted", "digits"],
)
def test_the_customer_sent_carries_digits_and_no_empty_fields(
    actor: User, stored: str, expected: str | None
) -> None:
    rig = make_rig(
        sequence([httpx.Response(200, json={"data": []}), httpx.Response(200, json={"id": "c1"})])
    )
    # A one-word full name: no last name, so none is sent.
    lease = billed_lease(actor, name="Northwind", phone=stored)
    with override_settings(STAX_CALLER="worker"):
        outcome = lease_sync.sync_lease(lease.pk, provider=StaxProvider(rig.transport), actor=actor)
    assert outcome.action is Action.CREATED
    [body] = _sent_bodies(rig)
    assert "lastname" not in body
    assert "" not in body.values()
    assert body.get("phone") == expected


@pytest.mark.django_db
@pytest.mark.parametrize("retry", [False, True], ids=["sync", "retry"])
def test_a_stored_bad_phone_refuses_the_sync_before_any_call(
    actor: User, fake: FakeProvider, retry: bool
) -> None:
    lease = create(actor)
    status = SyncStatus.FAILED if retry else SyncStatus.PENDING
    LeaseBilling.objects.filter(lease=lease).update(phone="555-0132")
    Lease.objects.filter(pk=lease.pk).update(sync_status=status)
    audits = AuditLog.objects.count()

    outcome = lease_sync.sync_lease(lease.pk, provider=fake, actor=actor, retry=retry)

    lease.refresh_from_db()
    assert fake.calls == []
    assert (outcome.action, outcome.failure) == (Action.FAILED, lease_sync.PHONE_INVALID)
    assert outcome.message == "Fix the phone number first."
    assert lease.sync_status == status
    assert lease.sync_attempted_at is None
    assert AuditLog.objects.count() == audits
    assert lease_sync.phone_is_valid(billing_row(lease)) is False


@pytest.mark.django_db
def test_migration_0006_normalises_good_phones_and_flags_bad_ones() -> None:
    """PLAN-006b §3 row 5, run against the migration's own historical state."""
    migration = importlib.import_module("ledger.migrations.0006_operator_phone_rule")
    rows = {
        "formatted": ("+1 (415) 555-0132", "14155550132", False),
        "digits": ("4155550132", "4155550132", False),
        "dotted": ("415.555.0132", "4155550132", False),
        "too short": ("555-0132", "555-0132", True),
        "extension": ("+44 20 7946 0958 ext 12", "+44 20 7946 0958 ext 12", True),
        "empty": ("", "", False),
    }
    ids = {
        label: Operator.objects.create(display_name=label, phone=stored).pk
        for label, (stored, _, _) in rows.items()
    }
    Operator.objects.filter(pk=ids["digits"]).update(needs_review=True)
    state = MigrationExecutor(connection).loader.project_state(
        ("ledger", "0006_operator_phone_rule")
    )
    editor = SimpleNamespace(connection=connection)

    migration.apply_phone_rule(state.apps, editor)
    migration.apply_phone_rule(state.apps, editor)  # a second run changes nothing

    for label, (_, phone, flagged) in rows.items():
        operator = Operator.objects.get(pk=ids[label])
        assert operator.phone == phone, label
        assert operator.needs_review is (flagged or label == "digits"), label
    assert Operator.objects.count() == len(rows)
    [operation] = migration.Migration.operations
    assert operation.reversible


# --- PLAN-006c: an edit of a linked lease's billing updates the provider customer -----------


def linked(actor: User, fake: FakeProvider, **overrides: Any) -> Lease:
    """A lease created and linked through the service; the fake's call log cleared."""
    lease = billed_lease(actor, **overrides)
    assert lease_sync.sync_lease(lease.pk, provider=fake, actor=actor).action is (Action.CREATED)
    lease.refresh_from_db()
    assert lease.sync_status == SyncStatus.LINKED
    fake.calls.clear()
    return lease


def edit(actor: User, fake: FakeProvider, lease: Lease, **changes: Any) -> lease_sync.SyncOutcome:
    """What the billing view does: save the billing details, then sync the saved row."""
    lease_sync.update_billing(lease.pk, billing_fields(**changes), actor=actor)
    return lease_sync.sync_lease(lease.pk, provider=fake, actor=actor)


def update_calls(fake: FakeProvider) -> list[tuple[str, Any]]:
    """``(customer_external_id, CustomerInput)`` of each ``update_customer`` call."""
    return [(str(args[0]), args[1]) for name, args in fake.calls if name == "update_customer"]


def audit_text(row: AuditLog) -> str:
    return f"{row.before} {row.after}"


@pytest.mark.django_db
def test_006c_row1_a_phone_edit_updates_the_customer_with_the_digits(
    actor: User, fake: FakeProvider
) -> None:
    lease = linked(actor, fake)
    [ref] = customer_refs(lease)

    outcome = edit(actor, fake, lease, phone="+1 (555) 010-0002")

    lease = Lease.objects.select_related("operator", "store", "billing").get(pk=lease.pk)
    assert outcome.action is Action.UPDATED
    assert outcome.message == "Updated the customer in Stax."
    assert call_names(fake) == ["update_customer"]
    [(customer_id, sent)] = update_calls(fake)
    assert customer_id == ref.external_id
    assert sent == lease_sync.customer_input(lease)
    assert sent.phone == "15550100002"
    assert (sent.first_name, sent.last_name, sent.company_name, sent.operator_email) == (
        "Northwind",
        "Stores",
        "Northwind Stores LLC",
        EMAIL,
    )
    assert (lease.sync_status, lease.sync_failure) == (SyncStatus.LINKED, "")
    assert lease.sync_attempted_at is not None
    audit = AuditLog.objects.get(action="lease.sync_updated")
    assert audit.actor_id == actor.pk
    assert audit.after is not None
    assert audit.after["fields"] == ["phone"]
    assert audit.after["email"] == "…@example.com"
    assert audit.after["customer"] == mask_identifier(ref.external_id)
    assert audit.after["sync_status"] == SyncStatus.LINKED
    assert EMAIL not in audit_text(audit)
    assert not has_token(audit_text(audit), "0100002")
    assert len(customer_refs(lease)) == 1


@pytest.mark.django_db
def test_006c_row1_a_company_edit_names_the_field_and_sends_the_c3_mapping(
    actor: User, fake: FakeProvider
) -> None:
    lease = linked(actor, fake)

    assert edit(actor, fake, lease, company_name="Southwind LLC").action is (Action.UPDATED)

    [(_, sent)] = update_calls(fake)
    assert (sent.first_name, sent.last_name, sent.company_name) == (
        "Northwind",
        "Stores",
        "Southwind LLC",
    )
    audit = AuditLog.objects.get(action="lease.sync_updated")
    assert audit.after is not None and audit.after["fields"] == ["company_name"]


@pytest.mark.django_db
def test_006c_an_operator_rename_queues_one_sync_per_lease_with_its_own_customer(
    actor: User, fake: FakeProvider
) -> None:
    """Fable's PLAN-014 C part 1 finding: each lease's customer carries the operator's full
    name, so a rename marks every lease with its own customer reference ``pending`` and
    queues one ``lease_sync`` for it; the job sends the new name. A lease that shares that
    account, a lease without a customer yet and an operator without a linked lease queue
    nothing; the rename itself calls nothing."""
    lease = linked(actor, fake)
    operator = lease.operator
    assert operator is not None
    [ref] = customer_refs(lease)
    unlinked = billed_lease(actor, operator=operator)
    sharer = billed_lease(actor, operator=operator, email=None)
    LeaseBilling.objects.filter(lease=sharer).update(shares_account_of=lease)
    statuses = {row.pk: row.sync_status for row in (unlinked, sharer)}
    lonely = billed_lease(actor, name="Lonely Stores")
    assert lonely.operator is not None

    operators.update_operator(
        operator.pk, fields(display_name="Southwind Person", email=None), actor=actor
    )
    # A second rename while the job waits queues no second one: the job reads the row.
    operators.update_operator(
        operator.pk, fields(display_name="Southwind Persons", email=None), actor=actor
    )
    operators.update_operator(
        lonely.operator.pk, fields(display_name="Lonely Person", email=None), actor=actor
    )

    assert fake.calls == []
    [job] = Job.objects.filter(kind=JobKind.LEASE_SYNC)
    assert (job.entity_type, job.entity_id, job.requested_by_id) == ("lease", lease.pk, actor.pk)
    lease.refresh_from_db()
    assert lease.sync_status == SyncStatus.PENDING
    for row in (unlinked, sharer):
        row.refresh_from_db()
        assert row.sync_status == statuses[row.pk]
    assert AuditLog.objects.filter(action="operator.updated").count() == 3

    outcome = lease_sync.sync_lease(lease.pk, provider=fake, actor=actor)
    assert outcome.action is Action.UPDATED
    [(customer_id, sent)] = update_calls(fake)
    assert customer_id == ref.external_id
    assert (sent.first_name, sent.last_name) == ("Southwind", "Persons")
    lease.refresh_from_db()
    assert lease.sync_status == SyncStatus.LINKED


@pytest.mark.django_db
def test_006c_an_operator_edit_that_keeps_the_name_queues_nothing(
    actor: User, fake: FakeProvider
) -> None:
    lease = linked(actor, fake)
    assert lease.operator is not None
    operators.update_operator(
        lease.operator.pk, fields(phone="+1 555 010 0009", email=None), actor=actor
    )
    assert fake.calls == [] and not Job.objects.exists()
    lease.refresh_from_db()
    assert lease.sync_status == SyncStatus.LINKED


@pytest.mark.django_db
def test_006c_row2_a_rejected_update_keeps_the_edit_and_the_retry_updates_once(
    actor: User, fake: FakeProvider
) -> None:
    lease = linked(actor, fake)
    fake.fail_next(
        "update_customer",
        ProviderRejected(422, "CANARY provider text", error_fields=("phone", "email")),
    )

    outcome = edit(actor, fake, lease, phone="4155550132")

    lease.refresh_from_db()
    assert outcome.action is Action.UPDATE_FAILED
    assert outcome.message == (
        "Saved here. Stax did not accept the update: Rejected. Retry when ready."
    )
    assert billing_row(lease).phone == "4155550132"
    assert (lease.sync_status, lease.sync_failure, lease.sync_failure_fields) == (
        SyncStatus.FAILED,
        "ProviderRejected",
        "phone,email",
    )
    assert lease.sync_attempted_at is not None
    failed = AuditLog.objects.get(action="lease.sync_failed")
    assert failed.after is not None
    assert failed.after["fields"] == ["phone"]
    assert failed.after["sync_failure_fields"] == "phone,email"
    for forbidden in ("CANARY", EMAIL, "4155550132"):
        assert forbidden not in audit_text(failed)
        assert forbidden not in outcome.message
    assert len(customer_refs(lease)) == 1

    # A plain sync and an early retry make no call.
    assert lease_sync.sync_lease(lease.pk, provider=fake, actor=actor).action is (Action.NO_CALL)
    early = lease_sync.sync_lease(lease.pk, provider=fake, actor=actor, retry=True)
    assert early.action is Action.RETRY_TOO_SOON
    assert call_names(fake) == ["update_customer"]

    Lease.objects.filter(pk=lease.pk).update(
        sync_attempted_at=timezone.now() - timedelta(seconds=lease_sync.RETRY_LOCK_SECONDS)
    )
    retried = lease_sync.sync_lease(lease.pk, provider=fake, actor=actor, retry=True)

    lease.refresh_from_db()
    assert retried.action is Action.UPDATED
    assert call_names(fake) == ["update_customer", "update_customer"]
    assert update_calls(fake)[1][1].phone == "4155550132"
    assert (lease.sync_status, lease.sync_failure, lease.sync_failure_fields) == (
        SyncStatus.LINKED,
        "",
        "",
    )
    updated = AuditLog.objects.get(action="lease.sync_updated")
    assert updated.after is not None and updated.after["fields"] == ["phone"]
    assert len(customer_refs(lease)) == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("error", "name", "words"),
    [
        (ProviderUnavailable(503), "ProviderUnavailable", "Unavailable"),
        (TransportError(), "TransportError", "Could not be reached"),
        (CircuitOpen("open"), "CircuitOpen", "Paused after repeated errors"),
    ],
    ids=["unavailable", "transport", "circuit-open"],
)
def test_006c_row2_other_failures_name_the_failure_in_words_and_no_fields(
    actor: User, fake: FakeProvider, error: ProviderError, name: str, words: str
) -> None:
    lease = linked(actor, fake)
    fake.fail_next("update_customer", error)

    outcome = edit(actor, fake, lease, phone="4155550132")

    lease.refresh_from_db()
    assert outcome.message == (
        f"Saved here. Stax did not accept the update: {words}. Retry when ready."
    )
    assert name not in outcome.message
    assert (billing_row(lease).phone, lease.sync_status, lease.sync_failure) == (
        "4155550132",
        SyncStatus.FAILED,
        name,
    )
    assert lease.sync_failure_fields == ""


@pytest.mark.django_db
def test_006c_row2_an_edit_of_a_failed_linked_lease_updates_again(
    actor: User, fake: FakeProvider
) -> None:
    lease = linked(actor, fake)
    fake.fail_next("update_customer", ProviderRejected(422, error_fields=("phone",)))
    edit(actor, fake, lease, phone="4155550132")

    # The fix of the rejected field is sent with the save, as an edit of a linked row is.
    outcome = edit(actor, fake, lease, phone="4155550133")

    lease.refresh_from_db()
    assert outcome.action is Action.UPDATED
    assert call_names(fake) == ["update_customer", "update_customer"]
    assert lease.sync_status == SyncStatus.LINKED
    audit = AuditLog.objects.get(action="lease.sync_updated")
    assert audit.after is not None and audit.after["fields"] == ["phone"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "changes",
    [
        {"notes": "Pays early"},
        {"address_line1": "2 Demo Avenue", "city": "Elsewhere", "postal_code": "75002"},
        {"address_line2": "Suite 4", "state": "OK"},
        {"phone": "15550100001"},  # the same digits as "+1 555 010 0001"
        {"email": " OPS@Example.com "},
    ],
    ids=["not-a-billing-field", "address", "address-line-2", "same-phone-digits", "same-email"],
)
def test_006c_row3_an_edit_outside_the_payload_makes_no_call(
    actor: User, fake: FakeProvider, changes: dict[str, Any]
) -> None:
    lease = linked(actor, fake)

    outcome = edit(actor, fake, lease, **changes)

    lease.refresh_from_db()
    assert outcome.action is Action.NO_CALL
    assert fake.calls == []
    assert lease.sync_status == SyncStatus.LINKED
    assert not AuditLog.objects.filter(action="lease.sync_updated").exists()


@pytest.mark.django_db
def test_006c_row3_a_non_payload_edit_keeps_a_failed_status(
    actor: User, fake: FakeProvider
) -> None:
    lease = linked(actor, fake)
    fake.fail_next("update_customer", ProviderUnavailable(503))
    edit(actor, fake, lease, phone="4155550132")
    fake.calls.clear()

    outcome = edit(actor, fake, lease, phone="4155550132", city="Elsewhere")

    lease.refresh_from_db()
    assert outcome.action is Action.NO_CALL
    assert fake.calls == []
    assert billing_row(lease).city == "Elsewhere"
    assert (lease.sync_status, lease.sync_failure) == (
        SyncStatus.FAILED,
        "ProviderUnavailable",
    )


@pytest.mark.django_db
def test_006c_row4_removing_the_email_updates_without_it_and_stays_linked(
    actor: User, fake: FakeProvider
) -> None:
    lease = linked(actor, fake)
    [ref] = customer_refs(lease)

    outcome = edit(actor, fake, lease, email=None)

    lease.refresh_from_db()
    assert outcome.action is Action.UPDATED
    [(customer_id, sent)] = update_calls(fake)
    assert (customer_id, sent.operator_email) == (ref.external_id, "")
    assert billing_row(lease).email == ""
    assert lease.sync_status == SyncStatus.LINKED
    audit = AuditLog.objects.get(action="lease.sync_updated")
    assert audit.after is not None
    assert (audit.after["fields"], audit.after["email"]) == (["email"], "")
    assert EMAIL not in audit_text(audit)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("changes", "expected_phone", "expected_email"),
    [
        ({"phone": "+1 (555) 010-0002"}, "15550100002", EMAIL),
        ({"email": None}, "15550100001", None),
    ],
    ids=["row1-phone", "row4-email-removed"],
)
def test_006c_an_edit_reaches_the_real_adapter_as_a_put_with_the_mapped_body(
    actor: User,
    fake: FakeProvider,
    changes: dict[str, Any],
    expected_phone: str,
    expected_email: str | None,
) -> None:
    """Rules 1 and 4 through the real StaxProvider and a mock transport, not the fake."""
    lease = linked(actor, fake)
    [ref] = customer_refs(lease)
    rig = make_rig(lambda request: httpx.Response(200, json={"id": ref.external_id}))

    lease_sync.update_billing(lease.pk, billing_fields(**changes), actor=actor)
    with override_settings(STAX_CALLER="worker"):
        outcome = lease_sync.sync_lease(lease.pk, provider=StaxProvider(rig.transport), actor=actor)

    lease.refresh_from_db()
    assert outcome.action is Action.UPDATED
    assert lease.sync_status == SyncStatus.LINKED
    [request] = rig.recorder.requests
    assert (request.method, request.url.path) == ("PUT", f"/customer/{ref.external_id}")
    [body] = _sent_bodies(rig)
    assert body["phone"] == expected_phone
    assert body.get("email") == expected_email
    assert ("email" in body) is (expected_email is not None)
    assert (body["firstname"], body["lastname"]) == ("Northwind", "Stores")
    assert "" not in body.values()


@pytest.mark.django_db
def test_006c_row5_an_unlinked_lease_is_never_updated(actor: User, fake: FakeProvider) -> None:
    awaiting = create(actor, email=None)
    assert edit(actor, fake, awaiting, email=None, phone="4155550132").action is (Action.NO_CALL)

    failed = create(actor, email="failed@example.com")
    fake.fail_next("find_customers", ProviderUnavailable(503))
    lease_sync.sync_lease(failed.pk, provider=fake, actor=actor)
    assert edit(actor, fake, failed, email="failed@example.com", phone="4155550132").action is (
        Action.NO_CALL
    )

    pending = create(actor, email="pending@example.com")
    assert edit(actor, fake, pending, email="pending@example.com", phone="4155550132").action is (
        Action.CREATED
    )

    assert "update_customer" not in call_names(fake)
    assert call_names(fake) == ["find_customers", "find_customers", "create_customer"]
    awaiting.refresh_from_db()
    assert awaiting.sync_status == SyncStatus.AWAITING_LINK


@pytest.mark.django_db
def test_006c_row6_two_edits_in_turn_send_two_updates_and_end_on_the_last(
    actor: User, fake: FakeProvider
) -> None:
    lease = linked(actor, fake)
    edit(actor, fake, lease, phone="4155550132")
    edit(actor, fake, lease, phone="4155550133")

    lease.refresh_from_db()
    assert [sent.phone for _, sent in update_calls(fake)] == ["4155550132", "4155550133"]
    assert lease.sync_status == SyncStatus.LINKED
    assert [
        row.after["fields"] if row.after else None
        for row in AuditLog.objects.filter(action="lease.sync_updated").order_by("id")
    ] == [["phone"], ["phone"]]


class SlowUpdateProvider(FakeProvider):
    """Holds each customer update long enough for a second edit to arrive meanwhile."""

    def update_customer(self, external_id: str, customer: Any) -> CustomerRecord:
        time.sleep(0.3)
        return super().update_customer(external_id, customer)


@requires_postgresql
@pytest.mark.django_db(transaction=True)
def test_006c_row6_two_concurrent_edits_never_crash_and_stax_ends_on_the_saved_row(
    actor: User,
) -> None:
    provider = SlowUpdateProvider()
    lease = linked(actor, provider)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []
    outcomes: list[Action] = []

    def submit(phone: str) -> None:
        try:
            barrier.wait()
            lease_sync.update_billing(lease.pk, billing_fields(phone=phone), actor=actor)
            outcomes.append(lease_sync.sync_lease(lease.pk, provider=provider, actor=actor).action)
        except BaseException as error:  # reported below, not swallowed
            errors.append(error)
        finally:
            connection.close()

    threads = [
        threading.Thread(target=submit, args=(phone,)) for phone in ("4155550132", "4155550133")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    lease.refresh_from_db()
    sent = update_calls(provider)
    assert 1 <= len(sent) <= 2
    assert sorted(outcomes, key=str) in (
        sorted([Action.UPDATED, Action.UPDATED], key=str),
        sorted([Action.UPDATED, Action.NO_CALL], key=str),
    )
    # The last update sent carries what is saved here.
    assert sent[-1][1].phone == billing_row(lease).phone
    assert lease.sync_status == SyncStatus.LINKED
    assert len(customer_refs(lease)) == 1


# --- PLAN-014 C9: a sharing lease's own customer ---------------------------------------------


def primary_and_sharer(actor: User, provider: FakeProvider) -> tuple[Lease, Lease]:
    """The primary lease, holding the customer ``cus_primary_0001`` (billing e-mail
    :data:`EMAIL`, at the provider too), and a second lease of the same operator sharing
    its account with the same billing e-mail, as ledger migration 0059 left them."""
    primary = create(actor)
    add_provider_customer(provider, "cus_primary_0001", EMAIL)
    ExternalRef.objects.create(
        entity_type="lease",
        entity_id=primary.pk,
        provider=lease_sync.PROVIDER_KEY,
        external_kind="customer",
        external_id="cus_primary_0001",
    )
    ExternalRef.objects.create(
        entity_type="lease",
        entity_id=primary.pk,
        provider=lease_sync.PROVIDER_KEY,
        external_kind="payment_method",
        external_id="pm_primary_0001",
    )
    sharer = billed_lease(actor, operator=primary.operator)
    LeaseBilling.objects.filter(lease=sharer).update(shares_account_of=primary)
    Lease.objects.filter(pk=sharer.pk).update(sync_status="not_applicable")
    return primary, Lease.objects.get(pk=sharer.pk)


def pend(lease: Lease) -> None:
    Lease.objects.filter(pk=lease.pk).update(sync_status="pending")


@pytest.mark.django_db
def test_a_sharing_lease_is_never_sent_without_own_customer(actor: User) -> None:
    provider = FuzzyFakeProvider()
    _, sharer = primary_and_sharer(actor, provider)
    pend(sharer)
    outcome = lease_sync.sync_lease(sharer.pk, provider=provider, actor=actor)
    assert outcome.action is Action.NO_CALL and provider.calls == []


@pytest.mark.django_db
def test_own_customer_creates_a_customer_and_never_adopts_the_shared_one(actor: User) -> None:
    from ledger.services import leases

    provider = FuzzyFakeProvider()
    primary, sharer = primary_and_sharer(actor, provider)
    pend(sharer)
    outcome = lease_sync.sync_lease(sharer.pk, provider=provider, actor=actor, own_customer=True)
    assert outcome.action is Action.CREATED, outcome
    assert call_names(provider) == ["find_customers", "create_customer"]
    [own] = customer_refs(sharer)
    assert own.external_id not in ("cus_primary_0001", "") and own.superseded_at is None
    # Still sharing: the account that bills it is the primary's, its method untouched.
    assert billing_row(sharer).shares_account_of_id == primary.pk
    assert leases.customer_ref(sharer) == "cus_primary_0001"
    assert leases.linked_payment_method(sharer) == "pm_primary_0001"
    assert not ExternalRef.objects.filter(entity_id=sharer.pk, external_kind="payment_method")
    sharer.refresh_from_db()
    assert (sharer.sync_status, sharer.needs_review) == ("linked", False)
    audit = AuditLog.objects.filter(entity_id=str(sharer.pk), action="lease.sync_created").get()
    assert audit.after["own_customer"] is True
    # A second run sends nothing.
    provider.calls.clear()
    pend(sharer)
    again = lease_sync.sync_lease(sharer.pk, provider=provider, actor=actor, own_customer=True)
    assert again.action is Action.NO_CALL and provider.calls == []


@pytest.mark.django_db
def test_own_customer_links_a_free_match_and_skips_the_held_one(actor: User) -> None:
    provider = FuzzyFakeProvider()
    _, sharer = primary_and_sharer(actor, provider)
    add_provider_customer(provider, "cus_free_000001", EMAIL)
    pend(sharer)
    outcome = lease_sync.sync_lease(sharer.pk, provider=provider, actor=actor, own_customer=True)
    assert outcome.action is Action.LINKED_EXISTING
    assert [ref.external_id for ref in customer_refs(sharer)] == ["cus_free_000001"]
    assert "create_customer" not in call_names(provider)


def _queue(**options: Any) -> str:
    from io import StringIO

    from django.core.management import call_command

    out = StringIO()
    call_command("create_lease_customers", stdout=out, **options)
    return out.getvalue()


@pytest.mark.django_db
def test_the_command_counts_queues_once_and_honours_the_limit(actor: User) -> None:
    provider = FuzzyFakeProvider()
    primary, sharer = primary_and_sharer(actor, provider)
    second = billed_lease(actor, operator=primary.operator)
    LeaseBilling.objects.filter(lease=second).update(shares_account_of=primary)
    no_email = billed_lease(actor, operator=primary.operator, email="")
    LeaseBilling.objects.filter(lease=no_email).update(shares_account_of=primary)
    Job.objects.all().delete()
    assert _queue(dry_run=True) == "leases: 3, queued: 0\n"
    assert not Job.objects.exists()
    assert _queue(limit=1) == "leases: 3, queued: 1\n"
    [job] = Job.objects.all()
    assert (job.kind, job.entity_id, job.params) == (
        JobKind.LEASE_SYNC,
        sharer.pk,
        {"own_customer": True},
    )
    # The waiting job is not queued again; the lease without an e-mail never is.
    assert _queue() == "leases: 3, queued: 1\n"
    assert sorted(Job.objects.values_list("entity_id", flat=True)) == sorted([sharer.pk, second.pk])
    assert _queue() == "leases: 3, queued: 0\n"
    assert LeaseBilling.objects.filter(shares_account_of=primary).count() == 3


@pytest.mark.django_db
def test_the_queued_jobs_give_each_sharing_lease_its_own_customer(
    actor: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.job_helpers import run_queued_jobs

    provider = FuzzyFakeProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    primary, sharer = primary_and_sharer(actor, provider)
    Job.objects.all().delete()
    assert _queue() == "leases: 1, queued: 1\n"
    [done] = run_queued_jobs()
    assert done.status == JobStatus.DONE, done.result
    assert len(customer_refs(sharer)) == 1 and "list_payment_methods" not in call_names(provider)
    assert billing_row(sharer).shares_account_of_id == primary.pk
    assert _queue() == "leases: 0, queued: 0\n"

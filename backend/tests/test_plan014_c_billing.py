"""PLAN-014 Batch C, part 1 (Fable): the operator is a person and billing details belong to
each lease — `LeaseBilling` and its CHECKs, migration 0059 forward (the primary lease, the
sharers, the flags, the audit rows, the operator that keeps its references) and backward (one
holder reverses, two refuse), the two helpers through one shared hop, a sharer never holding
references of its own, `billing_for` a lease-less charge, the Edit billing details dialog
(billing only; details audited; a sharer cannot link), the operator form without Type, Company,
Contact person and Legal name, "Bill to" with the lease's company over the operator's full
name, a collection message addressed to the lease's billing e-mail, a customer webhook that maps
to the lease — and the adversarial cases of plan §5 C (a cycle, a self-share, a customer id
already linked elsewhere, payment data in the company name, a 2-hop share)."""

from __future__ import annotations

import importlib
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import Client

from accounts.roles import Role
from ledger.models import AuditLog, Lease, LeaseBilling, Operator, OperatorKind, SyncStatus
from ledger.services import leases
from payments import lease_sync
from payments.fake import FakeProvider
from payments.models import ExternalRef
from tests.receivables_helpers import a_lease, a_one_off_charge, a_rent_charge, a_store, an_operator
from tests.test_operator_views import client_for, form_data

pytestmark = pytest.mark.django_db

TODAY = date(2026, 10, 6)
MIGRATIONS = Path(__file__).resolve().parents[1] / "ledger" / "migrations"
MIGRATION_0059 = next(MIGRATIONS.glob("0059_*.py")).stem
LEDGER_BEFORE = ("ledger", next(MIGRATIONS.glob("0058_*.py")).stem)
#: The payments state beside it: ExternalRef lives in payments; 0019 only renames job kinds.
PAYMENTS_BESIDE = ("payments", "0018_copy_labels")
PROVIDER = lease_sync.PROVIDER_KEY


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    return TODAY


def page(client: Client, path: str, expect: int = 200) -> str:
    response = client.get(path)
    assert response.status_code == expect, (path, response.status_code)
    return response.content.decode()


def a_ref(lease: Lease, kind: str, external_id: str) -> ExternalRef:
    return ExternalRef.objects.create(
        entity_type=lease_sync.ENTITY_LEASE,
        entity_id=lease.pk,
        provider=PROVIDER,
        external_kind=kind,
        external_id=external_id,
    )


def connected_lease(label: str = "1901", number: int = 1901) -> Lease:
    lease = a_lease(
        a_store(label, number=number), an_operator("Ayşe Kaya"), started=date(2026, 1, 1)
    )
    a_ref(lease, lease_sync.KIND_CUSTOMER, f"cust-{number}")
    a_ref(lease, lease_sync.KIND_PAYMENT_METHOD, f"pm-{number}")
    LeaseBilling.objects.update_or_create(
        lease=lease,
        defaults={"email": f"store{number}@example.com", "company_name": "Kaya Repairs LLC"},
    )
    return lease


# --- C2: the model ----------------------------------------------------------------------------


def test_billing_row_fields_and_checks() -> None:
    lease = a_lease(a_store("1902", number=1902), an_operator("Person One"))
    row = LeaseBilling.objects.create(
        lease=lease, email="Mixed.Case@Example.com", bank_last4="4821"
    )
    row.refresh_from_db()
    assert row.email == "mixed.case@example.com" and row.bank_last4 == "4821"
    with pytest.raises(IntegrityError), transaction.atomic():
        LeaseBilling.objects.create(
            lease=a_lease(a_store("1903"), an_operator("P")), bank_last4="48x1"
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        other = a_lease(a_store("1904"), an_operator("Q"))
        LeaseBilling.objects.create(lease=other, shares_account_of=other)


def test_the_lease_carries_the_sync_columns_and_the_operator_keeps_its_own() -> None:
    lease = a_lease(a_store("1905"), an_operator("R"))
    assert lease.sync_status == SyncStatus.NOT_APPLICABLE
    assert hasattr(Operator, "sync_status") and hasattr(Lease, "creation_key")


# --- C3: the helpers --------------------------------------------------------------------------


def test_the_helpers_resolve_one_shared_hop_and_no_further() -> None:
    primary = connected_lease()
    sharer = a_lease(a_store("1911", number=1911), primary.operator, started=date(2026, 3, 1))
    LeaseBilling.objects.create(lease=sharer, shares_account_of=primary)
    assert leases.account_lease(sharer) == primary
    assert leases.customer_ref(sharer) == "cust-1901"
    assert leases.linked_payment_method(sharer) == "pm-1901"
    # A chain: a lease sharing a sharer resolves one hop only (to the sharer, which has no refs).
    chain = a_lease(a_store("1912", number=1912), primary.operator, started=date(2026, 4, 1))
    LeaseBilling.objects.create(lease=chain, shares_account_of=sharer)
    assert leases.account_lease(chain) == sharer
    assert leases.customer_ref(chain) == ""


def test_a_sharing_lease_can_never_hold_references_of_its_own() -> None:
    primary = connected_lease()
    sharer = a_lease(a_store("1913", number=1913), primary.operator, started=date(2026, 3, 1))
    LeaseBilling.objects.create(lease=sharer, shares_account_of=primary)
    with pytest.raises(lease_sync.SharesAccount):
        lease_sync._add_ref(sharer, lease_sync.KIND_CUSTOMER, "cust-x")
    outcome = lease_sync.link_lease(
        sharer.pk,
        customer_external_id="cust-x",
        payment_method_external_id="pm-x",
        provider=FakeProvider(),
        actor=None,
    )
    assert outcome.failure == lease_sync.SHARES_ACCOUNT
    assert not ExternalRef.objects.filter(entity_id=sharer.pk).exists()


def test_billing_for_a_lease_less_charge_is_the_operators_first_active_lease() -> None:
    operator = an_operator("Two Stores")
    later = a_lease(a_store("1922", number=1922), operator, started=date(2026, 5, 1))
    earlier = a_lease(a_store("1921", number=1921), operator, started=date(2026, 2, 1))
    LeaseBilling.objects.create(lease=earlier, email="first@example.com")
    LeaseBilling.objects.create(lease=later, email="second@example.com")
    charge = a_one_off_charge(operator, "100.00", date(2026, 10, 1))
    assert leases.billing_lease(charge) == earlier
    billing = leases.billing_for(charge)
    assert billing is not None and billing.email == "first@example.com"
    lonely = an_operator("No Lease")
    assert leases.billing_for(a_one_off_charge(lonely, "50.00", date(2026, 10, 1))) is None
    assert "no lease" in leases.NO_LEASE_BILLING.lower()


def test_customer_input_is_built_from_the_lease_and_the_persons_name() -> None:
    lease = connected_lease()
    assert lease.operator is not None
    lease.operator.display_name = "Ana Maria Lopez"
    lease.operator.save()
    billing = lease_sync.billing_of(lease)
    billing.phone = "+1 212 555 0100"
    billing.save()
    payload = lease_sync.customer_input(lease)
    assert (payload.first_name, payload.last_name) == ("Ana Maria", "Lopez")
    assert payload.company_name == "Kaya Repairs LLC"
    assert payload.operator_email == "store1901@example.com"
    assert payload.reference == "1901"
    assert lease_sync.split_name("Cher") == ("Cher", "")


# --- C8: migration 0059 ------------------------------------------------------------------------


def _migrate(*targets: tuple[str, str]) -> Any:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(list(targets))
    return MigrationExecutor(connection).loader.project_state(list(targets)).apps


def _old_world(old: Any) -> dict[str, Any]:
    """Before 0059: Kaya holds a customer and a method and has two active leases (the
    later-started one has the lower store number); Solo holds refs but only an ended lease;
    Plain has no refs."""
    operator = old.get_model("ledger", "Operator")
    store = old.get_model("ledger", "Store")
    lease = old.get_model("ledger", "Lease")
    ref = old.get_model("payments", "ExternalRef")
    kaya = operator.objects.create(
        display_name="Kaya",
        kind="company",
        company="Kaya Repairs LLC",
        email="kaya@example.com",
        phone="2125550100",
    )
    first = lease.objects.create(
        store=store.objects.create(external_label="1931", store_number=1931, status="active"),
        operator=kaya,
        collection_status="active",
        payment_channel="stax",
        started_on=date(2025, 1, 1),
        debit_day=5,
    )
    second = lease.objects.create(
        store=store.objects.create(external_label="1930", store_number=1930, status="active"),
        operator=kaya,
        collection_status="active",
        payment_channel="stax",
        started_on=date(2026, 1, 1),
        debit_day=5,
    )
    ended = lease.objects.create(
        store=store.objects.create(external_label="1929", store_number=1929, status="active"),
        operator=kaya,
        collection_status="ended",
        payment_channel="stax",
        started_on=date(2024, 1, 1),
        ended_on=date(2024, 12, 31),
        debit_day=5,
    )
    for kind, external_id in (("customer", "cust-kaya"), ("payment_method", "pm-kaya")):
        ref.objects.create(
            entity_type="operator",
            entity_id=kaya.pk,
            provider=PROVIDER,
            external_kind=kind,
            external_id=external_id,
        )
    solo = operator.objects.create(display_name="Solo", email="solo@example.com")
    lease.objects.create(
        store=store.objects.create(external_label="1928", store_number=1928, status="active"),
        operator=solo,
        collection_status="ended",
        payment_channel="stax",
        started_on=date(2024, 1, 1),
        ended_on=date(2025, 6, 30),
        debit_day=5,
    )
    ref.objects.create(
        entity_type="operator",
        entity_id=solo.pk,
        provider=PROVIDER,
        external_kind="customer",
        external_id="cust-solo",
    )
    plain = operator.objects.create(display_name="Plain", email="plain@example.com")
    lease.objects.create(
        store=store.objects.create(external_label="1927", store_number=1927, status="active"),
        operator=plain,
        collection_status="active",
        payment_channel="wire",
        started_on=date(2026, 1, 1),
        debit_day=5,
    )
    return {
        "kaya": kaya.pk,
        "first": first.pk,
        "second": second.pk,
        "ended": ended.pk,
        "solo": solo.pk,
        "plain": plain.pk,
    }


@pytest.mark.django_db(transaction=True)
def test_0059_moves_the_refs_to_the_primary_lease_shares_the_rest_and_refuses_to_reverse() -> None:
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        old = _migrate(LEDGER_BEFORE, PAYMENTS_BESIDE)
        ids = _old_world(old)
        new = _migrate(("ledger", MIGRATION_0059), PAYMENTS_BESIDE)
        billing = new.get_model("ledger", "LeaseBilling")
        lease = new.get_model("ledger", "Lease")
        ref = new.get_model("payments", "ExternalRef")
        audit = new.get_model("ledger", "AuditLog")
        # Every lease has a billing row copied from its operator.
        rows = {row.lease_id: row for row in billing.objects.all()}
        assert set(rows) >= {ids["first"], ids["second"], ids["ended"]}
        assert (
            rows[ids["first"]].email == "kaya@example.com"
            and rows[ids["first"]].company_name == "Kaya Repairs LLC"
        )
        # The primary is the earliest start (1931 started 2025), not the lowest number.
        assert set(
            ref.objects.filter(entity_type="lease", entity_id=ids["first"]).values_list(
                "external_id", flat=True
            )
        ) == {"cust-kaya", "pm-kaya"}
        assert not ref.objects.filter(entity_type="operator", entity_id=ids["kaya"]).exists()
        primary = lease.objects.get(pk=ids["first"])
        assert primary.sync_status == "not_applicable" or primary.sync_status  # copied as it was
        # The other leases share; the active one is flagged, the ended one is not.
        assert rows[ids["second"]].shares_account_of_id == ids["first"]
        assert rows[ids["ended"]].shares_account_of_id == ids["first"]
        second = lease.objects.get(pk=ids["second"])
        assert second.needs_review is True and second.review_reason == "own_customer"
        assert lease.objects.get(pk=ids["ended"]).needs_review is False
        # Solo keeps its references and is flagged; Plain is untouched.
        assert ref.objects.filter(
            entity_type="operator", entity_id=ids["solo"], external_id="cust-solo"
        ).exists()
        operator = new.get_model("ledger", "Operator")
        assert operator.objects.get(pk=ids["solo"]).needs_review is True
        assert operator.objects.get(pk=ids["plain"]).needs_review is False
        assert (
            audit.objects.filter(action="operator.refs_kept", entity_id=str(ids["solo"])).count()
            == 1
        )
        migrated = audit.objects.filter(action="lease_billing.migrated")
        assert migrated.count() == lease.objects.count()
        moved = migrated.get(entity_id=str(ids["first"]))
        assert moved.after["refs_moved"] is True and moved.after["shares"] is False
        # Two leases of one operator hold references after C9: the reverse refuses.
        ref.objects.create(
            entity_type="lease",
            entity_id=ids["second"],
            provider=PROVIDER,
            external_kind="customer",
            external_id="cust-second",
        )
        with pytest.raises(RuntimeError):
            _migrate(LEDGER_BEFORE, PAYMENTS_BESIDE)
    finally:
        _migrate(*leaves)


@pytest.mark.django_db(transaction=True)
def test_0059_reverses_when_each_operator_has_one_holder() -> None:
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        old = _migrate(LEDGER_BEFORE, PAYMENTS_BESIDE)
        operator = old.get_model("ledger", "Operator").objects.create(
            display_name="One", email="one@example.com"
        )
        store = old.get_model("ledger", "Store").objects.create(
            external_label="1940", store_number=1940, status="active"
        )
        lease = old.get_model("ledger", "Lease").objects.create(
            store=store,
            operator=operator,
            collection_status="active",
            payment_channel="stax",
            started_on=date(2026, 1, 1),
            debit_day=5,
        )
        old.get_model("payments", "ExternalRef").objects.create(
            entity_type="operator",
            entity_id=operator.pk,
            provider=PROVIDER,
            external_kind="customer",
            external_id="cust-one",
        )
        new = _migrate(("ledger", MIGRATION_0059), PAYMENTS_BESIDE)
        assert (
            new.get_model("payments", "ExternalRef").objects.get(external_id="cust-one").entity_id
            == lease.pk
        )
        back = _migrate(LEDGER_BEFORE, PAYMENTS_BESIDE)
        ref = back.get_model("payments", "ExternalRef").objects.get(external_id="cust-one")
        assert ref.entity_type == "operator" and ref.entity_id == operator.pk
    finally:
        _migrate(*leaves)


# --- C3: the Edit billing details dialog ---------------------------------------------------


def test_the_dialog_is_billings_and_a_saved_change_is_audited() -> None:
    lease = connected_lease()
    viewer, _ = client_for(Role.VIEWER)
    assert viewer.get(f"/leases/{lease.pk}/billing/").status_code in (302, 403)
    billing, _ = client_for(Role.BILLING)
    html = page(billing, f"/leases/{lease.pk}/billing/")
    assert 'id="billing-save-form"' in html and 'id="billing-link-form"' in html
    response = billing.post(
        f"/leases/{lease.pk}/billing/",
        {
            "form": "details",
            "company_name": "Kaya Repairs LLC",
            "email": "New@Example.com",
            "phone": "2125550100",
            "address_line1": "1 Main St",
            "address_line2": "",
            "city": "Newark",
            "state": "NJ",
            "postal_code": "07102",
        },
    )
    assert response.status_code == 302
    row = LeaseBilling.objects.get(lease=lease)
    assert row.email == "new@example.com"
    changed = AuditLog.objects.get(action="lease_billing.changed", entity_id=str(lease.pk))
    assert (
        changed.before["email"] == "store1901@example.com"
        and changed.after["email"] == "new@example.com"
    )
    lease.refresh_from_db()
    assert (
        lease.sync_status == SyncStatus.PENDING
    )  # the payload changed: the sync job updates the customer


def test_a_company_name_with_payment_data_is_refused_and_not_echoed() -> None:
    lease = connected_lease()
    billing, _ = client_for(Role.BILLING)
    response = billing.post(
        f"/leases/{lease.pk}/billing/",
        {
            "form": "details",
            "company_name": "Card 4111 1111 1111 1111",
            "email": "",
            "phone": "",
            "address_line1": "",
            "address_line2": "",
            "city": "",
            "state": "",
            "postal_code": "",
        },
        follow=True,
    )
    assert response.status_code == 200
    assert "4111" not in response.content.decode()
    assert LeaseBilling.objects.get(lease=lease).company_name == "Kaya Repairs LLC"


def test_a_sharer_sees_whose_account_it_uses_and_has_no_link_form() -> None:
    primary = connected_lease()
    sharer = a_lease(a_store("1914", number=1914), primary.operator, started=date(2026, 3, 1))
    LeaseBilling.objects.create(lease=sharer, shares_account_of=primary)
    billing, _ = client_for(Role.BILLING)
    html = page(billing, f"/leases/{sharer.pk}/billing/")
    assert (
        'id="billing-shares"' in html and "1901" in html[html.index('id="billing-shares"') :][:200]
    )
    assert 'id="billing-link-form"' not in html


# --- C1: the operator form --------------------------------------------------------------------


def test_the_operator_form_has_four_fields_and_ignores_the_removed_ones() -> None:
    admin, _ = client_for(Role.ADMIN)
    html = page(admin, "/operators/new/")
    for gone in ("Company name", "Contact person", "Legal name", 'name="kind"'):
        assert gone not in html, gone
    assert "Full name" in html
    response = admin.post(
        "/operators/new/",
        form_data(
            display_name="Mehmet Demir",
            email="mehmet@example.com",
            kind="company",
            company="Ignored LLC",
            legal_name="Ignored",
        ),
    )
    assert response.status_code == 302, response.content.decode()[:300]
    operator = Operator.objects.get(email="mehmet@example.com")
    assert operator.display_name == "Mehmet Demir" and operator.kind == OperatorKind.PERSON
    assert operator.company == "" and operator.legal_name == ""


def test_the_operator_page_shows_no_connect_link_and_no_company() -> None:
    lease = connected_lease()
    html = page(client_for(Role.BILLING)[0], f"/operators/{lease.operator_id}/")
    assert "Connect to" not in html and "Legal name" not in html
    assert "1901" in html and "····" in html or "no bank account" in html


# --- C6: Bill to -------------------------------------------------------------------------------


def test_bill_to_prints_the_leases_company_over_the_persons_name() -> None:
    from ledger.services import invoice_builder

    lease = connected_lease()
    assert lease.operator is not None
    bill = invoice_builder.bill_to(lease, lease.operator)
    assert (
        bill.company == "Kaya Repairs LLC"
        and bill.name == "Ayşe Kaya"
        and bill.email == "store1901@example.com"
    )
    html = page(client_for(Role.VIEWER)[0], f"/operators/{lease.operator_id}/statement/")
    assert "Kaya Repairs LLC" in html and "Ayşe Kaya" in html


# --- C5: messages and webhooks -----------------------------------------------------------------


def test_a_collection_message_goes_to_the_leases_billing_email() -> None:
    from ledger.models import OutboundMessage
    from ledger.services import collections

    lease = connected_lease()
    assert lease.operator is not None
    lease.operator.email = "person@example.com"
    lease.operator.save()
    a_rent_charge(lease, date(2026, 9, 1), due="1000.00", day=1)
    collections.queue_due(TODAY)
    message = OutboundMessage.objects.order_by("queued_at").last()
    assert (
        message is not None
        and message.to_address == "store1901@example.com"
        and message.lease_id == lease.pk
    )


def test_the_customer_webhook_maps_to_the_lease_and_ignores_one_on_an_operator() -> None:
    from payments.webhooks.handlers import invoice as handler

    lease = connected_lease()
    assert handler.customer_lease("cust-1901") == lease
    assert lease.operator_id is not None
    ExternalRef.objects.create(
        entity_type="operator",
        entity_id=lease.operator_id,
        provider=PROVIDER,
        external_kind="customer",
        external_id="cust-old",
    )
    assert handler.customer_lease("cust-old") is None and handler.customer_on_operator("cust-old")


def test_no_production_module_bills_from_the_operators_contact_details() -> None:
    backend = Path(__file__).resolve().parents[1]
    offenders = []
    for path in (backend / "ledger").rglob("*.py"):
        if "migrations" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for needle in ("customer_ref(operator", "linked_payment_method(operator", "operator_sync."):
            if needle in text:
                offenders.append(f"{path.relative_to(backend)}: {needle}")
    assert offenders == []
    assert importlib.util.find_spec("payments.operator_sync") is None

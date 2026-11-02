"""The bank's institution name beside the last four (ADR-0019, the owner, 2026-11-01).

The adapter keeps ``bank_name`` and still drops ``bank_account``, ``bank_routing``,
``bank_holder_type`` and ``meta``; a name that holds payment data or is too long reads as "";
the redaction keeps ``bank_name`` and drops every other ``bank_*`` key; the method snapshot
caches it on ``LeaseBilling`` with ``bank_last4``; the LEASE and PAYMENT cards and the portal's
store and bank pages print it before the digits, else as before; ledger migration 0067 both
ways, with the PostgreSQL trigger refusing a card number in ``lease_billing.bank_name``.

Every account and routing number below is the public test pattern of the adapter tests, used
only to prove it never survives.
"""

from __future__ import annotations

import importlib
from dataclasses import replace
from datetime import date
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings

from accounts.roles import Role
from ledger.models import LeaseBilling
from ledger.services import detail_cards
from ledger.services import leases as service
from payments.fake import FakeProvider
from payments.models import ExternalRef
from payments.redaction import SENSITIVE_KEY, payment_method_subset, redact
from payments.stax.adapter import StaxProvider
from payments.stax.mapping import summary
from tests.receivables_helpers import a_lease, a_store
from tests.stax_helpers import always, make_rig
from tests.test_lease_services import BANK, CUSTOMER, make_lease, method
from tests.test_operator_views import client_for
from tests.test_plan014_c_billing import _lease_card, _sharer_of, connected_lease, page
from tests.test_portal import World, active_user, portal_client, text

pytestmark = pytest.mark.django_db

ACCOUNT = "000123456789"
ROUTING = "011000015"
CARD = "4111111111111111"
NAME = "First Example Bank"
MIGRATION_0067 = importlib.import_module("ledger.migrations.0067_lease_billing_bank_name")
BEFORE_0067 = ("ledger", "0066_operator_state_codes")
AFTER_0067 = ("ledger", "0067_lease_billing_bank_name")

#: A bank method as the specification names its keys, with hostile values beside the name.
BANK_OBJECT: dict[str, Any] = {
    "id": "pm-bank-1",
    "method": "bank",
    "bank_type": "checking",
    "bank_name": NAME,
    "bank_holder_type": "business",
    "card_last_four": "6789",
    "customer_id": "cus-1",
    "bank_account": ACCOUNT,
    "bank_routing": ROUTING,
    "meta": {"accountDisplay": ACCOUNT, "routingDisplay": ROUTING},
}


def _read(**changes: Any) -> Any:
    rig = make_rig(always(200, {**BANK_OBJECT, **changes}))
    with override_settings(STAX_CALLER="worker"):
        return StaxProvider(rig.transport).get_payment_method("pm-bank-1")


# --- The adapter ------------------------------------------------------------------------------


def test_the_mapping_keeps_the_bank_name_and_drops_account_routing_holder_and_meta() -> None:
    record = _read()
    assert (record.bank_name, record.last_four, record.bank_type) == (NAME, "6789", "checking")
    everything = repr(record)
    for forbidden in (ACCOUNT, ROUTING, "business", "accountDisplay", "routingDisplay"):
        assert forbidden not in everything


@pytest.mark.parametrize(
    "value",
    [
        f"Bank {CARD}",  # a 13-19 digit run that passes the Luhn check
        f"{ROUTING} {ACCOUNT}",  # a routing pattern beside an account number
        "B" * 101,  # longer than 100 characters
        123,  # not a string
        None,
    ],
    ids=["card-number", "routing-and-account", "too-long", "number", "null"],
)
def test_a_bank_name_that_holds_payment_data_or_is_too_long_reads_as_empty(value: object) -> None:
    assert _read(bank_name=value).bank_name == ""


def test_a_bank_name_of_exactly_100_characters_is_kept_and_a_missing_one_is_empty() -> None:
    assert _read(bank_name="B" * 100).bank_name == "B" * 100
    without = {key: value for key, value in BANK_OBJECT.items() if key != "bank_name"}
    rig = make_rig(always(200, without))
    with override_settings(STAX_CALLER="worker"):
        assert StaxProvider(rig.transport).get_payment_method("pm-bank-1").bank_name == ""


# --- The redaction ----------------------------------------------------------------------------


def test_the_redaction_keeps_the_bank_name_and_drops_every_other_bank_key() -> None:
    hostile = {
        "bank_name": NAME,
        "bank_type": "checking",
        "bank_holder_type": "business",
        "bank_account": ACCOUNT,
        "bank_routing": ROUTING,
        "other_bank_name": "Not kept",
        "bank_name_extra": "Not kept",
        "BANK_NAME": "Not kept",
        "x\nbank_name": "Not kept",
        "bank_name\n": "Not kept",
    }
    assert redact(hostile) == {"bank_name": NAME}
    assert redact({"payment_method": {**BANK_OBJECT, "nickname": "Operating"}}) == {
        "payment_method": {
            "id": "pm-bank-1",
            "method": "bank",
            "card_last_four": "6789",
            "bank_name": NAME,
            "nickname": "Operating",
        }
    }
    assert summary(hostile) == {"bank_name": NAME}
    assert [key for key in hostile if not SENSITIVE_KEY.search(key)] == ["bank_name"]


def test_the_redaction_drops_a_bank_name_that_holds_payment_data() -> None:
    assert payment_method_subset({"id": "pm-1", "bank_name": f"Bank {CARD}"}) == {"id": "pm-1"}
    assert redact({"bank_name": f"{ROUTING} {ACCOUNT}"}) == {}


# --- The snapshot caches the name with the last four ------------------------------------------


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


def _link(lease: Any, external_id: str = BANK) -> None:
    ExternalRef.objects.create(
        entity_type="lease",
        entity_id=lease.pk,
        provider="stax_pay",
        external_kind="payment_method",
        external_id=external_id,
    )


def test_a_method_read_sets_the_bank_name_with_the_last_four(fake: FakeProvider) -> None:
    fake.add_payment_method(CUSTOMER, replace(method(), bank_name=NAME))
    lease = make_lease()
    _link(lease)
    assert service.refresh_payment_methods(lease.pk).ok
    billing = LeaseBilling.objects.get(lease=lease)
    assert (billing.bank_last4, billing.bank_name) == ("6789", NAME)
    linked = service.linked_method_record(lease)  # read from the snapshot
    assert linked is not None and linked.bank_name == NAME

    # The processor stops returning a name: the cache follows it, the digits stay.
    fake.add_payment_method(CUSTOMER, method())
    assert service.refresh_payment_methods(lease.pk).ok
    billing.refresh_from_db()
    assert (billing.bank_last4, billing.bank_name) == ("6789", "")

    # The linked method is gone from the list: both are cleared together.
    fake.payment_methods.clear()
    fake.add_payment_method(CUSTOMER, replace(method("pm-other"), bank_name=NAME))
    assert service.refresh_payment_methods(lease.pk).ok
    billing.refresh_from_db()
    assert (billing.bank_last4, billing.bank_name) == ("", "")


def test_attaching_a_bank_method_caches_its_bank_name(fake: FakeProvider) -> None:
    lease = make_lease()
    fake.add_payment_method(CUSTOMER, replace(method(), bank_name=NAME))
    outcome = service.attach_bank_method(lease.pk, BANK, actor=None)
    assert outcome.ok, outcome
    billing = LeaseBilling.objects.get(lease=lease)
    assert (billing.bank_last4, billing.bank_name) == ("6789", NAME)


def test_the_model_refuses_payment_data_and_more_than_100_characters_in_the_bank_name() -> None:
    lease = connected_lease()
    billing = LeaseBilling.objects.get(lease=lease)
    for value in (f"Bank {CARD}", "B" * 101):
        billing.bank_name = value
        with pytest.raises(ValidationError):
            billing.full_clean()


# --- The display ------------------------------------------------------------------------------


def test_the_bank_text_names_the_bank_when_known() -> None:
    assert detail_cards.bank_text("4821", NAME) == f"{NAME} ····4821"
    assert detail_cards.bank_text("4821", "") == "Bank ····4821"
    assert detail_cards.bank_text("4821") == "Bank ····4821"
    assert detail_cards.bank_text("", NAME) == detail_cards.NO_BANK_ON_FILE


def test_the_lease_card_shows_the_bank_name_or_bank() -> None:
    lease = connected_lease()
    client = client_for(Role.BILLING)[0]
    LeaseBilling.objects.filter(lease=lease).update(bank_last4="4821")
    assert '<dd id="header-bank">Bank ····4821</dd>' in _lease_card(client, lease)
    LeaseBilling.objects.filter(lease=lease).update(bank_name=NAME)
    assert f'<dd id="header-bank">{NAME} ····4821</dd>' in _lease_card(client, lease)


def test_the_bank_name_is_printed_as_text() -> None:
    lease = connected_lease()
    LeaseBilling.objects.filter(lease=lease).update(bank_last4="4821", bank_name="<b>A&B</b>")
    card = _lease_card(client_for(Role.BILLING)[0], lease)
    assert '<dd id="header-bank">&lt;b&gt;A&amp;B&lt;/b&gt; ····4821</dd>' in card
    assert "<b>A&B</b>" not in card


def test_a_sharer_shows_the_account_owners_bank_name() -> None:
    primary = connected_lease("fatih", 1999)
    LeaseBilling.objects.filter(lease=primary).update(bank_last4="4821", bank_name=NAME)
    sharer = _sharer_of(primary)
    LeaseBilling.objects.filter(lease=sharer).update(bank_name="Not this one")
    card = _lease_card(client_for(Role.VIEWER)[0], sharer)
    assert f'<dd id="header-bank">{NAME} ····4821 <span class="muted"' in card
    assert "Not this one" not in card


def test_the_payment_card_shows_the_bank_name_or_bank() -> None:
    named = connected_lease("1901", 1901)
    LeaseBilling.objects.filter(lease=named).update(bank_last4="4821", bank_name=NAME)
    plain = a_lease(a_store("1902", number=1902), named.operator, started=date(2026, 1, 1))
    LeaseBilling.objects.create(lease=plain, bank_last4="1234")
    html = page(client_for(Role.BILLING)[0], f"/operators/{named.operator_id}/")
    assert f'id="side-account-{named.pk}">Kaya Repairs LLC<br>{NAME} ····4821</dd>' in html
    assert f'id="side-account-{plain.pk}">Bank ····1234</dd>' in html


@pytest.fixture
def world() -> World:
    return World()


def test_the_portal_store_and_bank_pages_show_the_bank_name_or_the_digits_alone(
    world: World,
) -> None:
    client = portal_client(active_user(world.a))
    store_url = f"/portal/stores/{world.store_a.pk}/"
    assert 'id="store-bank">····4821<' in text(client.get(store_url))
    assert 'id="bank-on-file">····4821' in text(client.get(f"{store_url}bank/"))
    LeaseBilling.objects.filter(lease=world.lease_a).update(bank_name=NAME)
    assert f'id="store-bank">{NAME} ····4821<' in text(client.get(store_url))
    assert f'id="bank-on-file">{NAME} ····4821' in text(client.get(f"{store_url}bank/"))


def test_the_portal_shows_a_sharer_the_account_owners_bank_name(world: World) -> None:
    LeaseBilling.objects.filter(lease=world.lease_a).update(bank_name=NAME)
    sharer = a_lease(a_store("Sharetown", 103), world.a)
    LeaseBilling.objects.create(
        lease=sharer, shares_account_of=world.lease_a, bank_name="Not this one"
    )
    client = portal_client(active_user(world.a))
    store_page = text(client.get(f"/portal/stores/{sharer.store_id}/"))
    bank_page = text(client.get(f"/portal/stores/{sharer.store_id}/bank/"))
    assert f'id="store-bank">{NAME} ····4821<' in store_page
    assert f'id="bank-on-file">{NAME} ····4821' in bank_page
    assert "Not this one" not in store_page + bank_page


# --- Ledger migration 0067 --------------------------------------------------------------------


def _migrate(*targets: tuple[str, str]) -> Any:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(list(targets))
    return MigrationExecutor(connection).loader.project_state(list(targets)).apps


def _columns() -> set[str]:
    with connection.cursor() as cursor:
        described = connection.introspection.get_table_description(cursor, "lease_billing")
    return {column.name for column in described}


def _write_card(apps: Any, pk: Any, column: str) -> None:
    model = apps.get_model("ledger", "LeaseBilling")
    with pytest.raises(IntegrityError), transaction.atomic():
        model.objects.filter(pk=pk).update(**{column: f"Bank {CARD}"})


def test_0067_guards_the_new_column_after_0059s_columns() -> None:
    assert MIGRATION_0067.GUARDED_COLUMNS == {
        "lease_billing": (
            "company_name",
            "email",
            "phone",
            "address_line1",
            "address_line2",
            "city",
            "state",
            "postal_code",
            "bank_name",
        )
    }


@pytest.mark.django_db(transaction=True)
def test_0067_adds_the_column_and_reverses() -> None:
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    postgresql = connection.vendor == "postgresql"
    try:
        old = _migrate(BEFORE_0067)
        assert "bank_name" not in _columns()
        operator = old.get_model("ledger", "Operator").objects.create(display_name="Kaya")
        store = old.get_model("ledger", "Store").objects.create(
            external_label="1901", store_number=1901, status="active"
        )
        lease = old.get_model("ledger", "Lease").objects.create(
            store=store,
            operator=operator,
            collection_status="active",
            payment_channel="wire",
            debit_day=5,
        )
        billing = old.get_model("ledger", "LeaseBilling").objects.create(
            lease=lease, bank_last4="4821"
        )

        new = _migrate(AFTER_0067)
        assert "bank_name" in _columns()
        row = new.get_model("ledger", "LeaseBilling").objects.get(pk=billing.pk)
        assert (row.bank_last4, row.bank_name) == ("4821", "")
        new.get_model("ledger", "LeaseBilling").objects.filter(pk=billing.pk).update(bank_name=NAME)
        if postgresql:
            _write_card(new, billing.pk, "bank_name")
            _write_card(new, billing.pk, "company_name")

        back = _migrate(BEFORE_0067)
        assert "bank_name" not in _columns()
        assert back.get_model("ledger", "LeaseBilling").objects.get(pk=billing.pk).bank_last4 == (
            "4821"
        )
        if postgresql:
            # 0059's trigger is back on 0059's columns.
            _write_card(back, billing.pk, "company_name")

        again = _migrate(AFTER_0067)
        assert again.get_model("ledger", "LeaseBilling").objects.get(pk=billing.pk).bank_name == ""
        if postgresql:
            _write_card(again, billing.pk, "bank_name")
    finally:
        _migrate(*leaves)


def test_a_unicode_bank_name_is_shown_as_stored() -> None:
    lease = connected_lease()
    LeaseBilling.objects.filter(lease=lease).update(bank_last4="4821", bank_name="Banco Ñandú")
    card = _lease_card(client_for(Role.BILLING)[0], lease)
    assert '<dd id="header-bank">Banco Ñandú ····4821</dd>' in card

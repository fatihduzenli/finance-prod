"""The Bank deposits module, slice 1
(docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md I1, I2, I4; §5 I).

The env guard (all three Plaid secrets or none, a Fernet key, value-free messages); the access
token through Fernet and never in ``repr``, logs, the Django admin or a page; the fake source's
sync idempotent on ``external_id`` with debits never kept and payment-data text withheld; Plaid's
client against ``httpx.MockTransport`` (link token, exchange, sync with cursor paging, one retry
on a 5xx, none on a 4xx, logs without bodies or tokens); the module dark until enabled (the
page's sentence, the daily job skips, no HTTP); Confirm through ``record_entry`` (the method
mapping, the reference, the date, partial, the rest kept as a credit, applied once, the alias
offered and saved); Split across two stores and three checks; Ignore with reasons; Undo through
the manual void; auto-confirm; the settings card; the roles on every route; the page's queries;
the migrations both ways. Every key, secret and token here is an obvious fake.
"""

from __future__ import annotations

import base64
import json
import logging
import uuid
from collections.abc import Callable, Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest
from django.contrib import admin
from django.core.management import call_command
from django.db import connection, models
from django.db.migrations.executor import MigrationExecutor
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.roles import Role
from banking import crypto, services, sources
from banking.models import (
    BankConnection,
    BankTransaction,
    Confidence,
    IgnoreReason,
    Kind,
    SenderAlias,
    Source,
    Status,
    normalise,
)
from banking.plaid import client as plaid
from finance.env_guard import EnvGuardError, validate
from ledger.models import (
    AuditLog,
    CompanySettings,
    Lease,
    LeaseCredit,
    Operator,
    Payment,
    PaymentMethod,
    PaymentStatus,
    RentChargeStatus,
)
from tests.receivables_helpers import (
    a_lease,
    a_one_off_charge,
    a_rent_charge,
    a_store,
    an_invoice,
)
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

TODAY = date(2026, 10, 15)
POSTED = date(2026, 10, 10)
OCTOBER = date(2026, 10, 1)
D = Decimal

FAKE_CLIENT_ID = "fake-plaid-client-id-for-tests"
FAKE_SECRET = "fake-plaid-secret-for-tests-only"
#: 32 bytes of plain words, as a Fernet key: obviously not a real key.
FAKE_TOKEN_KEY = base64.urlsafe_b64encode(b"fake-fernet-key-for-tests-only!!").decode()
OTHER_TOKEN_KEY = base64.urlsafe_b64encode(b"another-fake-key-for-tests-only!").decode()
FAKE_ACCESS_TOKEN = "access-sandbox-fake-token-for-tests"
FAKE_LINK_TOKEN = "link-sandbox-fake-link-token"
FAKE_PUBLIC_TOKEN = "public-sandbox-fake-public-token"

BASE_ENV = {
    "APP_ENV": "local",
    "STAX_ENV": "sandbox",
    "STAX_API_BASE_URL": "https://apiprod.fattlabs.com",
    "DATABASE_URL": "sqlite:///:memory:",
    "APP_BASE_URL": "http://localhost:8000",
    "DJANGO_SECRET_KEY": "test-only-not-secret",
}


# --- Fixtures ----------------------------------------------------------------------------------


@pytest.fixture
def plaid_keys(settings: Any) -> None:
    settings.PLAID_ENV = "sandbox"
    settings.PLAID_CLIENT_ID = FAKE_CLIENT_ID
    settings.PLAID_SECRET = FAKE_SECRET
    settings.PLAID_TOKEN_KEY = FAKE_TOKEN_KEY


def enable(*, auto: bool = False) -> None:
    row = CompanySettings.current()
    row.bank_deposits_enabled = True
    row.bank_auto_confirm_exact = auto
    row.save()


def a_connection(
    source: str = Source.FAKE, token: str = sources.FAKE_TOKEN_MARKER
) -> BankConnection:
    from django.utils import timezone

    return BankConnection.objects.create(
        item_id="item-fake",
        account_id="acct-fake",
        institution="Example Bank",
        mask="0000",
        access_token_encrypted=token,
        source=source,
        linked_at=timezone.now(),
    )


def deposit(
    amount: str,
    description: str = "INCOMING",
    counterparty: str = "",
    kind: str = Kind.WIRE,
    posted_on: date = POSTED,
) -> BankTransaction:
    return BankTransaction.objects.create(
        source=Source.FAKE,
        external_id=f"t-{uuid.uuid4().hex[:12]}",
        posted_on=posted_on,
        amount=D(amount),
        description=description,
        counterparty=counterparty,
        kind=kind,
    )


def lease_at(number: int, label: str = "") -> Lease:
    return a_lease(a_store(label or f"Store {number}", number))


def owner(lease: Lease) -> Operator:
    assert lease.operator is not None
    return lease.operator


# --- Factories (also the card-number guard's, tests/test_card_number_guard.py) ---------------


def new_bank_connection(**fields: Any) -> BankConnection:
    from django.utils import timezone

    values: dict[str, Any] = {
        "item_id": "item-fake",
        "account_id": "acct-fake",
        "linked_at": timezone.now(),
        "active": False,
    }
    values.update(fields)
    return BankConnection(**values)


def new_bank_transaction(**fields: Any) -> BankTransaction:
    values: dict[str, Any] = {
        "source": Source.FAKE,
        "external_id": f"t-{uuid.uuid4().hex[:12]}",
        "posted_on": POSTED,
        "amount": D("10.00"),
    }
    values.update(fields)
    return BankTransaction(**values)


def new_sender_alias(**fields: Any) -> SenderAlias:
    values: dict[str, Any] = {"text": "Example sender", "normalised": "EXAMPLE SENDER"}
    values.update(fields)
    if "lease" not in values:
        values["lease"] = a_lease()
    return SenderAlias(**values)


BANKING_FACTORIES: dict[type[models.Model], Callable[..., models.Model]] = {
    BankConnection: new_bank_connection,
    BankTransaction: new_bank_transaction,
    SenderAlias: new_sender_alias,
}


@pytest.fixture
def billing_user() -> Any:
    return client_for(Role.BILLING)[1]


@pytest.fixture
def admin_user() -> Any:
    return client_for(Role.ADMIN)[1]


class Plaid:
    """A fake Plaid behind ``httpx.MockTransport``: answers by path, records each request."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.answers: dict[str, list[httpx.Response]] = {}

    def on(self, path: str, *responses: httpx.Response) -> None:
        self.answers.setdefault(path, []).extend(responses)

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.requests.append((request.url.path, body))
        queue = self.answers.get(request.url.path, [])
        if not queue:
            return httpx.Response(500, json={"error_code": "NO_ANSWER"})
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def source(self) -> plaid.PlaidSource:
        return plaid.PlaidSource(transport=httpx.MockTransport(self.handler))

    def paths(self) -> list[str]:
        return [path for path, _ in self.requests]


def ok(body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=body)


def plaid_txn(
    txn_id: str,
    amount: float,
    *,
    account: str = "acct-fake",
    name: str = "WIRE IN",
    pending: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "transaction_id": txn_id,
        "account_id": account,
        "amount": amount,
        "iso_currency_code": "USD",
        "date": "2026-10-09",
        "name": name,
        "pending": pending,
        **extra,
    }


# --- The env guard -----------------------------------------------------------------------------


def test_the_three_plaid_secrets_are_all_or_none() -> None:
    assert validate(BASE_ENV).plaid_client_id == ""
    config = validate(
        {
            **BASE_ENV,
            "PLAID_CLIENT_ID": FAKE_CLIENT_ID,
            "PLAID_SECRET": FAKE_SECRET,
            "PLAID_TOKEN_KEY": FAKE_TOKEN_KEY,
        }
    )
    assert (config.plaid_env, config.plaid_client_id) == ("sandbox", FAKE_CLIENT_ID)
    for missing in ("PLAID_CLIENT_ID", "PLAID_SECRET", "PLAID_TOKEN_KEY"):
        env = {
            **BASE_ENV,
            "PLAID_CLIENT_ID": FAKE_CLIENT_ID,
            "PLAID_SECRET": FAKE_SECRET,
            "PLAID_TOKEN_KEY": FAKE_TOKEN_KEY,
        }
        del env[missing]
        with pytest.raises(EnvGuardError) as raised:
            validate(env)
        message = str(raised.value)
        assert "must be set together or all left empty" in message
        for value in (FAKE_CLIENT_ID, FAKE_SECRET, FAKE_TOKEN_KEY):
            assert value not in message


def test_the_token_key_must_be_a_fernet_key_and_is_never_echoed() -> None:
    bad = "not-a-fernet-key-but-long-enough"
    with pytest.raises(EnvGuardError) as raised:
        validate({**BASE_ENV, "PLAID_CLIENT_ID": "a", "PLAID_SECRET": "b", "PLAID_TOKEN_KEY": bad})
    assert "PLAID_TOKEN_KEY must be a Fernet key" in str(raised.value)
    assert bad not in str(raised.value)


def test_the_plaid_environment_is_one_of_three_and_never_production_locally() -> None:
    assert validate({**BASE_ENV, "PLAID_ENV": "development"}).plaid_env == "development"
    with pytest.raises(EnvGuardError, match="PLAID_ENV is not one of"):
        validate({**BASE_ENV, "PLAID_ENV": "live"})
    with pytest.raises(EnvGuardError, match="may not be 'production' when APP_ENV is 'local'"):
        validate({**BASE_ENV, "PLAID_ENV": "production"})


def test_the_config_repr_holds_no_plaid_secret() -> None:
    config = validate(
        {
            **BASE_ENV,
            "PLAID_CLIENT_ID": FAKE_CLIENT_ID,
            "PLAID_SECRET": FAKE_SECRET,
            "PLAID_TOKEN_KEY": FAKE_TOKEN_KEY,
        }
    )
    text = repr(config)
    for value in (FAKE_CLIENT_ID, FAKE_SECRET, FAKE_TOKEN_KEY):
        assert value not in text


# --- The access token at rest ------------------------------------------------------------------


def test_the_access_token_round_trips_through_fernet(plaid_keys: None) -> None:
    stored = crypto.encrypt(FAKE_ACCESS_TOKEN)
    assert FAKE_ACCESS_TOKEN not in stored
    assert crypto.decrypt(stored) == FAKE_ACCESS_TOKEN


def test_another_key_cannot_read_the_token(settings: Any, plaid_keys: None) -> None:
    stored = crypto.encrypt(FAKE_ACCESS_TOKEN)
    settings.PLAID_TOKEN_KEY = OTHER_TOKEN_KEY
    with pytest.raises(crypto.TokenUnreadableError):
        crypto.decrypt(stored)
    with pytest.raises(crypto.TokenUnreadableError):
        crypto.decrypt("garbage")


def test_without_the_key_nothing_is_encrypted(settings: Any) -> None:
    settings.PLAID_TOKEN_KEY = ""
    with pytest.raises(crypto.TokenKeyMissingError):
        crypto.encrypt(FAKE_ACCESS_TOKEN)


def test_the_connection_repr_and_str_never_show_the_token(plaid_keys: None) -> None:
    stored = crypto.encrypt(FAKE_ACCESS_TOKEN)
    connection_row = a_connection(Source.PLAID, stored)
    for text in (repr(connection_row), str(connection_row)):
        assert stored not in text
        assert FAKE_ACCESS_TOKEN not in text
        assert "item-fake" not in text
    assert str(connection_row) == "Example Bank ending 0000"


def test_no_banking_model_is_in_the_django_admin() -> None:
    assert not {BankConnection, BankTransaction, SenderAlias} & set(admin.site._registry)


# --- The fake source --------------------------------------------------------------------------


ROWS = [
    {"external_id": "f-1", "posted_on": "2026-10-01", "amount": "1200.00", "description": "WIRE"},
    {"external_id": "f-2", "posted_on": "2026-10-02", "amount": "-45.10", "description": "FEE"},
    {"external_id": "f-3", "posted_on": "2026-10-03", "amount": "80.00", "kind": "zelle"},
]


def test_the_fake_sync_is_idempotent_on_the_external_id() -> None:
    connection_row = a_connection()
    first = sources.FakeSource(ROWS).sync(connection_row)
    assert (first.added, first.already, first.debits_ignored) == (2, 0, 1)
    second = sources.FakeSource(ROWS).sync(connection_row)
    assert (second.added, second.already, second.debits_ignored) == (0, 2, 1)
    assert sorted(BankTransaction.objects.values_list("external_id", flat=True)) == ["f-1", "f-3"]
    assert BankTransaction.objects.get(external_id="f-3").kind == Kind.ZELLE
    connection_row.refresh_from_db()
    assert connection_row.cursor == "fake-3"
    assert connection_row.last_sync_at is not None


def test_debits_are_never_kept() -> None:
    sources.FakeSource([{**ROWS[1], "amount": "-1200.00"}]).sync(a_connection())
    assert not BankTransaction.objects.exists()


def test_a_description_holding_a_card_number_is_withheld() -> None:
    card = "4111 1111 1111 1111"
    result = sources.FakeSource(
        [{**ROWS[0], "description": f"REF {card}", "counterparty": "ACME"}]
    ).sync(a_connection())
    assert result.withheld == 1
    row = BankTransaction.objects.get()
    assert row.description == sources.WITHHELD
    assert card not in row.description
    assert row.counterparty == "ACME"


def test_the_default_fake_reads_its_fixture() -> None:
    result = sources.FakeSource().sync(a_connection())
    assert result.added == 3
    assert result.debits_ignored == 1


def test_an_amount_beyond_the_money_column_is_not_kept() -> None:
    result = sources.FakeSource([{**ROWS[0], "amount": "99999999999.00"}]).sync(a_connection())
    assert (result.added, result.refused) == (0, 1)


# --- Plaid's client ----------------------------------------------------------------------------


def test_the_link_token(plaid_keys: None) -> None:
    fake = Plaid()
    fake.on(plaid.LINK_TOKEN_PATH, ok({"link_token": FAKE_LINK_TOKEN}))
    token = fake.source().create_link_token("42", "Monvera Retail Holdings Incorporated")
    assert token == FAKE_LINK_TOKEN
    [(path, body)] = fake.requests
    assert path == "/link/token/create"
    assert body["client_id"] == FAKE_CLIENT_ID
    assert body["secret"] == FAKE_SECRET
    assert body["user"] == {"client_user_id": "42"}
    assert body["products"] == ["transactions"]
    assert len(body["client_name"]) <= plaid.CLIENT_NAME_MAX_LENGTH


def test_the_exchange_takes_the_mask_from_plaid(plaid_keys: None) -> None:
    fake = Plaid()
    fake.on(plaid.EXCHANGE_PATH, ok({"access_token": FAKE_ACCESS_TOKEN, "item_id": "item-1"}))
    fake.on(
        plaid.ACCOUNTS_PATH,
        ok(
            {
                "accounts": [
                    {"account_id": "acct-1", "mask": "4321", "type": "depository"},
                    {"account_id": "acct-2", "mask": "9999", "type": "credit"},
                ],
                "item": {"institution_name": "First Example Bank"},
            }
        ),
    )
    linked = fake.source().exchange(FAKE_PUBLIC_TOKEN, "acct-1", "typed name")
    assert (linked.item_id, linked.account_id, linked.mask, linked.institution) == (
        "item-1",
        "acct-1",
        "4321",
        "First Example Bank",
    )
    assert FAKE_ACCESS_TOKEN not in repr(linked)
    assert fake.requests[1][1]["access_token"] == FAKE_ACCESS_TOKEN


def test_the_exchange_refuses_an_account_that_is_not_one_depository_account(
    plaid_keys: None,
) -> None:
    fake = Plaid()
    fake.on(plaid.EXCHANGE_PATH, ok({"access_token": FAKE_ACCESS_TOKEN, "item_id": "item-1"}))
    fake.on(
        plaid.ACCOUNTS_PATH,
        ok(
            {
                "accounts": [
                    {"account_id": "acct-1", "mask": "1", "type": "depository"},
                    {"account_id": "acct-2", "mask": "2", "type": "depository"},
                ]
            }
        ),
    )
    with pytest.raises(sources.LinkRefusedError):
        fake.source().exchange(FAKE_PUBLIC_TOKEN, "", "")


def test_the_sync_pages_with_the_cursor(plaid_keys: None) -> None:
    fake = Plaid()
    fake.on(
        plaid.SYNC_PATH,
        ok(
            {
                "added": [
                    plaid_txn("p-1", -1200.00, name="WIRE IN FATIH TRADING"),
                    plaid_txn("p-2", 45.10, name="SERVICE FEE"),
                    plaid_txn("p-3", -10.00, pending=True),
                ],
                "modified": [],
                "removed": [],
                "next_cursor": "cursor-1",
                "has_more": True,
            }
        ),
        ok(
            {
                "added": [
                    plaid_txn("p-4", -300.00, name="ZELLE FROM ACME"),
                    plaid_txn("p-5", -999.00, account="acct-other"),
                    plaid_txn("p-6", -75.00, check_number="1042", name="DEPOSIT"),
                ],
                "next_cursor": "cursor-2",
                "has_more": False,
            }
        ),
    )
    stored = crypto.encrypt(FAKE_ACCESS_TOKEN)
    connection_row = a_connection(Source.PLAID, stored)
    connection_row.cursor = "cursor-0"
    connection_row.save()
    result = fake.source().sync(connection_row)
    assert [body.get("cursor") for _, body in fake.requests] == ["cursor-0", "cursor-1"]
    assert all(body["access_token"] == FAKE_ACCESS_TOKEN for _, body in fake.requests)
    assert (result.added, result.debits_ignored, result.pages) == (3, 1, 2)
    kept = dict(BankTransaction.objects.values_list("external_id", "amount"))
    assert kept == {"p-1": D("1200.00"), "p-4": D("300.00"), "p-6": D("75.00")}
    kinds = dict(BankTransaction.objects.values_list("external_id", "kind"))
    assert kinds == {"p-1": Kind.WIRE, "p-4": Kind.ZELLE, "p-6": Kind.CHECK}
    connection_row.refresh_from_db()
    assert connection_row.cursor == "cursor-2"


def test_the_first_sync_sends_no_cursor(plaid_keys: None) -> None:
    fake = Plaid()
    fake.on(plaid.SYNC_PATH, ok({"added": [], "next_cursor": "c-1", "has_more": False}))
    fake.source().sync(a_connection(Source.PLAID, crypto.encrypt(FAKE_ACCESS_TOKEN)))
    assert "cursor" not in fake.requests[0][1]


def test_a_mutation_during_paging_starts_again_from_the_first_cursor(plaid_keys: None) -> None:
    fake = Plaid()
    fake.on(
        plaid.SYNC_PATH,
        ok({"added": [], "next_cursor": "c-1", "has_more": True}),
        httpx.Response(400, json={"error_code": plaid.MUTATION_DURING_PAGINATION}),
        ok({"added": [plaid_txn("p-1", -5.00)], "next_cursor": "c-9", "has_more": False}),
    )
    result = fake.source().sync(a_connection(Source.PLAID, crypto.encrypt(FAKE_ACCESS_TOKEN)))
    assert [body.get("cursor") for _, body in fake.requests] == [None, "c-1", None]
    assert result.added == 1


def test_a_5xx_is_retried_once(plaid_keys: None) -> None:
    fake = Plaid()
    fake.on(plaid.LINK_TOKEN_PATH, httpx.Response(503), ok({"link_token": FAKE_LINK_TOKEN}))
    assert fake.source().create_link_token("1", "x") == FAKE_LINK_TOKEN
    assert fake.paths() == [plaid.LINK_TOKEN_PATH] * 2


def test_two_5xx_fail_after_one_retry(plaid_keys: None) -> None:
    fake = Plaid()
    fake.on(plaid.LINK_TOKEN_PATH, httpx.Response(500), httpx.Response(502))
    with pytest.raises(plaid.PlaidServerError):
        fake.source().create_link_token("1", "x")
    assert len(fake.requests) == 2


def test_a_4xx_is_not_retried(plaid_keys: None) -> None:
    fake = Plaid()
    fake.on(plaid.LINK_TOKEN_PATH, httpx.Response(400, json={"error_code": "INVALID_FIELD"}))
    with pytest.raises(plaid.PlaidRequestError):
        fake.source().create_link_token("1", "x")
    assert len(fake.requests) == 1


def test_a_network_error_is_one_failure(plaid_keys: None) -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    source = plaid.PlaidSource(transport=httpx.MockTransport(broken))
    with pytest.raises(plaid.PlaidNetworkError):
        source.create_link_token("1", "x")


def test_the_logs_carry_no_body_secret_or_token(
    plaid_keys: None, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="banking")
    fake = Plaid()
    fake.on(plaid.EXCHANGE_PATH, ok({"access_token": FAKE_ACCESS_TOKEN, "item_id": "item-1"}))
    fake.on(
        plaid.ACCOUNTS_PATH,
        httpx.Response(503, json={"secret": FAKE_SECRET}),
        ok({"accounts": [{"account_id": "acct-1", "mask": "4321", "type": "depository"}]}),
    )
    fake.source().exchange(FAKE_PUBLIC_TOKEN, "acct-1", "")
    text = caplog.text
    assert "sandbox.plaid.com answered 200" in text
    assert "answered 503" in text
    for value in (FAKE_ACCESS_TOKEN, FAKE_SECRET, FAKE_CLIENT_ID, FAKE_PUBLIC_TOKEN, "4321"):
        assert value not in text


# --- Dark until enabled ------------------------------------------------------------------------


def counting(fake: Plaid, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services, "get_source", fake.source)


def test_the_module_is_off_by_default_and_makes_no_call(
    plaid_keys: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = Plaid()
    counting(fake, monkeypatch)
    a_connection(Source.PLAID, crypto.encrypt(FAKE_ACCESS_TOKEN))
    report = services.fetch(TODAY)
    assert (report.ok, report.message) == (False, services.OFF)
    assert fake.requests == []
    assert services.readiness() == services.OFF


def test_without_a_connection_nothing_is_fetched(
    plaid_keys: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = Plaid()
    counting(fake, monkeypatch)
    enable()
    assert services.fetch(TODAY).message == services.NO_CONNECTION
    assert fake.requests == []


def test_without_keys_outside_local_nothing_is_fetched(settings: Any) -> None:
    settings.APP_ENV = "staging"
    enable()
    a_connection()
    assert sources.get_source() is None
    assert services.fetch(TODAY).message == services.NO_SOURCE


def test_a_failed_sync_keeps_the_class_name_only(
    plaid_keys: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = Plaid()
    fake.on(plaid.SYNC_PATH, httpx.Response(400, json={"error_code": plaid.ITEM_LOGIN_REQUIRED}))
    counting(fake, monkeypatch)
    enable()
    connection_row = a_connection(Source.PLAID, crypto.encrypt(FAKE_ACCESS_TOKEN))
    report = services.fetch(TODAY)
    assert not report.ok
    assert "PlaidItemLoginRequiredError" in report.message
    connection_row.refresh_from_db()
    assert connection_row.last_error == "PlaidItemLoginRequiredError"


def test_the_daily_job_syncs_only_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from io import StringIO

    monkeypatch.setattr(services, "get_source", lambda: sources.FakeSource(ROWS))
    a_connection()
    out = StringIO()
    call_command("collections_tick", stdout=out)
    assert services.OFF in out.getvalue()
    assert not BankTransaction.objects.exists()
    enable()
    out = StringIO()
    call_command("collections_tick", stdout=out)
    assert "Bank deposits: Fetched from the bank: 2 new deposit(s)." in out.getvalue()
    assert BankTransaction.objects.count() == 2


# --- Confirm --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "method", "note_end"),
    [
        (Kind.WIRE, PaymentMethod.WIRE, "WIRE IN"),
        (Kind.ZELLE, PaymentMethod.ZELLE, "WIRE IN"),
        (Kind.CHECK, PaymentMethod.CHECK, "WIRE IN"),
        (Kind.ACH_CREDIT, PaymentMethod.OTHER, "WIRE IN (ACH credit)"),
        (Kind.OTHER, PaymentMethod.OTHER, "WIRE IN (Other)"),
    ],
)
def test_confirm_records_through_the_payment_entry(
    billing_user: Any, kind: str, method: str, note_end: str
) -> None:
    lease = lease_at(1991, "Istanbul")
    charge = a_rent_charge(lease, OCTOBER, due="1200.00")
    txn = deposit("1200.00", "WIRE IN", kind=kind)
    outcome = services.confirm(
        txn.pk, services.Choice(lease.pk, "rent", charge.pk), actor=billing_user, today=TODAY
    )
    assert outcome.ok, outcome.message
    assert outcome.message == "Deposit of $1,200.00 recorded for #1991 Istanbul."
    [payment] = Payment.objects.filter(rent_charge=charge)
    assert (payment.method, payment.amount, payment.status) == (method, D("1200.00"), "settled")
    assert payment.reference == str(txn.pk)
    assert payment.note == f"Bank deposit matched: {note_end}"
    assert payment.settled_on == POSTED
    txn.refresh_from_db()
    assert txn.status == Status.CONFIRMED
    assert payment.allocation_group == txn.allocation_group
    assert txn.confirmed_by == billing_user
    charge.refresh_from_db()
    assert charge.status == RentChargeStatus.PAID
    audit = AuditLog.objects.get(action=services.CONFIRMED_ACTION, entity_id=str(txn.pk))
    assert audit.after["payments"] == [str(payment.pk)]
    assert AuditLog.objects.filter(action="payment_entry.recorded").count() == 1


def test_a_smaller_deposit_is_a_partial_payment(billing_user: Any) -> None:
    lease = lease_at(5)
    charge = a_rent_charge(lease, OCTOBER, due="1200.00")
    txn = deposit("500.00")
    outcome = services.confirm(
        txn.pk, services.Choice(lease.pk, "rent", charge.pk), actor=billing_user, today=TODAY
    )
    assert outcome.ok
    charge.refresh_from_db()
    assert (charge.status, charge.amount_paid) == (RentChargeStatus.PARTIAL, D("500.00"))


def test_a_larger_deposit_needs_the_rest_kept_as_a_credit(billing_user: Any) -> None:
    lease = lease_at(5)
    charge = a_rent_charge(lease, OCTOBER, due="1200.00")
    txn = deposit("1500.00")
    choice = services.Choice(lease.pk, "rent", charge.pk)
    refused = services.confirm(txn.pk, choice, actor=billing_user, today=TODAY)
    assert not refused.ok
    assert "keep the rest as a credit" in refused.message
    assert not Payment.objects.exists()
    txn.refresh_from_db()
    assert txn.status == Status.NEW
    kept = services.confirm(txn.pk, choice, actor=billing_user, keep_credit=True, today=TODAY)
    assert kept.ok, kept.message
    [credit] = LeaseCredit.objects.filter(lease=lease)
    txn.refresh_from_db()
    assert (credit.amount, credit.allocation_group) == (D("300.00"), txn.allocation_group)


def test_the_whole_balance_pays_every_open_charge(billing_user: Any) -> None:
    lease = lease_at(5)
    rent = a_rent_charge(lease, OCTOBER, due="1200.00")
    fee = a_one_off_charge(owner(lease), "300.00", OCTOBER, lease=lease)
    txn = deposit("1500.00")
    outcome = services.confirm(txn.pk, services.Choice(lease.pk), actor=billing_user, today=TODAY)
    assert outcome.ok, outcome.message
    paid = dict(Payment.objects.values_list("rent_charge", "amount").filter(rent_charge=rent))
    assert paid == {rent.pk: D("1200.00")}
    assert Payment.objects.get(one_off_charge=fee).amount == D("300.00")


def test_a_deposit_is_applied_once(billing_user: Any) -> None:
    lease = lease_at(5)
    charge = a_rent_charge(lease, OCTOBER, due="1200.00")
    other = a_rent_charge(lease, date(2026, 11, 1), due="1200.00")
    txn = deposit("1200.00")
    assert services.confirm(
        txn.pk, services.Choice(lease.pk, "rent", charge.pk), actor=billing_user, today=TODAY
    ).ok
    again = services.confirm(
        txn.pk, services.Choice(lease.pk, "rent", other.pk), actor=billing_user, today=TODAY
    )
    assert (again.ok, again.message) == (False, services.ALREADY)
    assert Payment.objects.count() == 1


def test_a_charge_that_is_not_open_is_refused(billing_user: Any) -> None:
    lease = lease_at(5)
    a_rent_charge(lease, OCTOBER, due="1200.00")
    other_lease = lease_at(6)
    foreign = a_rent_charge(other_lease, OCTOBER, due="1200.00")
    txn = deposit("1200.00")
    outcome = services.confirm(
        txn.pk, services.Choice(lease.pk, "rent", foreign.pk), actor=billing_user, today=TODAY
    )
    assert (outcome.ok, outcome.message) == (False, services.NOT_OPEN)
    assert not Payment.objects.exists()


def test_the_alias_is_offered_and_saved(billing_user: Any) -> None:
    lease = lease_at(1991, "Istanbul")
    charge = a_rent_charge(lease, OCTOBER, due="1200.00")
    txn = deposit("1200.00", "WIRE", counterparty="  Fatih   Trading LLC ")
    assert services.alias_offer(txn, lease.pk, set()) == "Fatih Trading LLC"
    outcome = services.confirm(
        txn.pk,
        services.Choice(lease.pk, "rent", charge.pk),
        actor=billing_user,
        remember=True,
        today=TODAY,
    )
    assert outcome.message.endswith(" Fatih Trading LLC is remembered for #1991 Istanbul.")
    alias = SenderAlias.objects.get(lease=lease)
    assert (alias.text, alias.normalised, alias.created_by) == (
        "Fatih Trading LLC",
        "FATIH TRADING LLC",
        billing_user,
    )
    known = {(lease.pk, "FATIH TRADING LLC")}
    assert (
        services.alias_offer(deposit("1.00", counterparty="FATIH TRADING LLC"), lease.pk, known)
        == ""
    )


def test_an_alias_is_not_saved_unless_asked(billing_user: Any) -> None:
    lease = lease_at(5)
    charge = a_rent_charge(lease, OCTOBER, due="1200.00")
    txn = deposit("1200.00", counterparty="ACME STORES")
    services.confirm(
        txn.pk, services.Choice(lease.pk, "rent", charge.pk), actor=billing_user, today=TODAY
    )
    assert not SenderAlias.objects.exists()


def test_a_viewer_cannot_confirm() -> None:
    viewer = client_for(Role.VIEWER)[1]
    lease = lease_at(5)
    charge = a_rent_charge(lease, OCTOBER, due="1200.00")
    txn = deposit("1200.00")
    outcome = services.confirm(
        txn.pk, services.Choice(lease.pk, "rent", charge.pk), actor=viewer, today=TODAY
    )
    assert (outcome.ok, outcome.message) == (False, services.NOT_ALLOWED)


# --- Split ------------------------------------------------------------------------------------


def test_split_across_two_stores(billing_user: Any) -> None:
    first, second = lease_at(1), lease_at(2)
    one = a_rent_charge(first, OCTOBER, due="1000.00")
    two = a_rent_charge(second, OCTOBER, due="800.00")
    txn = deposit("1800.00", kind=Kind.CHECK)
    outcome = services.split(
        txn.pk,
        [
            services.SplitLine(first.store_id, one.pk, D("1000.00")),
            services.SplitLine(second.store_id, two.pk, D("800.00")),
        ],
        actor=billing_user,
        today=TODAY,
    )
    assert outcome.ok, outcome.message
    txn.refresh_from_db()
    assert txn.status == Status.SPLIT
    payments = Payment.objects.filter(allocation_group=txn.allocation_group)
    assert sorted(p.amount for p in payments) == [D("800.00"), D("1000.00")]
    assert {p.method for p in payments} == {PaymentMethod.CHECK}
    assert AuditLog.objects.filter(action="payment_entry.recorded").count() == 2


def test_split_three_checks_in_one_deposit(billing_user: Any) -> None:
    leases = [lease_at(n) for n in (11, 12, 13)]
    charges = [a_rent_charge(lease, OCTOBER, due="700.00") for lease in leases]
    txn = deposit("2100.00", "MOBILE DEPOSIT 3 CHECKS", kind=Kind.CHECK)
    outcome = services.split(
        txn.pk,
        [
            services.SplitLine(lease.store_id, charge.pk, D("700.00"))
            for lease, charge in zip(leases, charges, strict=True)
        ],
        actor=billing_user,
        today=TODAY,
    )
    assert outcome.ok, outcome.message
    for charge in charges:
        charge.refresh_from_db()
        assert charge.status == RentChargeStatus.PAID


def test_split_amounts_must_add_up(billing_user: Any) -> None:
    first, second = lease_at(1), lease_at(2)
    one = a_rent_charge(first, OCTOBER, due="1000.00")
    two = a_rent_charge(second, OCTOBER, due="800.00")
    txn = deposit("1800.00")
    lines = [services.SplitLine(first.store_id, one.pk, D("1000.00"))]
    short = services.split(txn.pk, lines, actor=billing_user, today=TODAY)
    assert not short.ok and "not the deposit of $1,800.00" in short.message
    over = services.split(
        txn.pk,
        [*lines, services.SplitLine(second.store_id, two.pk, D("900.00"))],
        actor=billing_user,
        today=TODAY,
    )
    assert not over.ok and "more than the deposit" in over.message
    assert not Payment.objects.exists()
    kept = services.split(txn.pk, lines, actor=billing_user, keep_credit=True, today=TODAY)
    assert kept.ok, kept.message
    assert LeaseCredit.objects.get(lease=first).amount == D("800.00")


def test_a_failed_second_store_writes_nothing(billing_user: Any) -> None:
    first, second = lease_at(1), lease_at(2)
    one = a_rent_charge(first, OCTOBER, due="1000.00")
    txn = deposit("1800.00")
    outcome = services.split(
        txn.pk,
        [
            services.SplitLine(first.store_id, one.pk, D("1000.00")),
            services.SplitLine(second.store_id, uuid.uuid4(), D("800.00")),
        ],
        actor=billing_user,
        today=TODAY,
    )
    assert (outcome.ok, outcome.message) == (False, services.SPLIT_NOT_OPEN)
    assert not Payment.objects.exists()
    txn.refresh_from_db()
    assert txn.status == Status.NEW


# --- Ignore ----------------------------------------------------------------------------------


@pytest.mark.parametrize("reason", [IgnoreReason.INTERNAL_TRANSFER, IgnoreReason.NOT_RENT])
def test_ignore_with_a_reason(billing_user: Any, reason: str) -> None:
    txn = deposit("50.00")
    outcome = services.ignore(txn.pk, reason, "", actor=billing_user)
    assert outcome.ok
    txn.refresh_from_db()
    assert (txn.status, txn.ignore_reason, txn.ignored_by) == (Status.IGNORED, reason, billing_user)
    assert AuditLog.objects.filter(action=services.IGNORED_ACTION).count() == 1


def test_ignore_other_needs_words_and_refuses_payment_data(billing_user: Any) -> None:
    txn = deposit("50.00")
    assert services.ignore(txn.pk, "", "", actor=billing_user).message == services.REASON_REQUIRED
    assert services.ignore(txn.pk, "bogus", "x", actor=billing_user).message == (
        services.REASON_REQUIRED
    )
    assert services.ignore(txn.pk, IgnoreReason.OTHER, "  ", actor=billing_user).message == (
        services.NOTE_REQUIRED
    )
    assert (
        services.ignore(
            txn.pk, IgnoreReason.OTHER, "card 4111 1111 1111 1111", actor=billing_user
        ).message
        == services.PAYMENT_DATA
    )
    assert services.ignore(txn.pk, IgnoreReason.OTHER, "x" * 501, actor=billing_user).message == (
        services.NOTE_TOO_LONG
    )
    assert services.ignore(txn.pk, IgnoreReason.OTHER, "Owner's loan", actor=billing_user).ok
    txn.refresh_from_db()
    assert txn.ignore_note == "Owner's loan"


# --- Undo ------------------------------------------------------------------------------------


def test_undo_voids_each_line_and_the_deposit_is_new_again(billing_user: Any) -> None:
    lease = lease_at(5)
    rent = a_rent_charge(lease, OCTOBER, due="1200.00")
    fee = a_one_off_charge(owner(lease), "300.00", OCTOBER, lease=lease)
    txn = deposit("1500.00")
    assert services.confirm(txn.pk, services.Choice(lease.pk), actor=billing_user, today=TODAY).ok
    outcome = services.undo(txn.pk, actor=billing_user, today=TODAY)
    assert outcome.ok, outcome.message
    payments = Payment.objects.filter(reference=str(txn.pk))
    assert {p.status for p in payments} == {PaymentStatus.VOIDED}
    assert {p.void_reason for p in payments} == {services.UNDO_REASON}
    assert AuditLog.objects.filter(action="payment.voided_manual").count() == 2
    rent.refresh_from_db()
    fee.refresh_from_db()
    assert rent.amount_paid == D("0.00")
    assert fee.amount_paid == D("0.00")
    txn.refresh_from_db()
    assert (txn.status, txn.allocation_group, txn.undone_at is not None) == (Status.NEW, None, True)
    assert AuditLog.objects.filter(action=services.UNDONE_ACTION).count() == 1
    # Recorded again with a new creation key: new payments.
    again = services.confirm(txn.pk, services.Choice(lease.pk), actor=billing_user, today=TODAY)
    assert again.ok, again.message
    assert Payment.objects.filter(reference=str(txn.pk), status=PaymentStatus.SETTLED).count() == 2


def test_undo_of_an_ignored_deposit(billing_user: Any) -> None:
    txn = deposit("50.00")
    services.ignore(txn.pk, IgnoreReason.NOT_RENT, "", actor=billing_user)
    assert services.undo(txn.pk, actor=billing_user, today=TODAY).ok
    txn.refresh_from_db()
    assert (txn.status, txn.ignore_reason, txn.ignored_at) == (Status.NEW, "", None)


def test_undo_is_refused_when_a_credit_was_kept(billing_user: Any) -> None:
    lease = lease_at(5)
    charge = a_rent_charge(lease, OCTOBER, due="1200.00")
    txn = deposit("1500.00")
    services.confirm(
        txn.pk,
        services.Choice(lease.pk, "rent", charge.pk),
        actor=billing_user,
        keep_credit=True,
        today=TODAY,
    )
    outcome = services.undo(txn.pk, actor=billing_user, today=TODAY)
    assert (outcome.ok, outcome.message) == (False, services.UNDO_CREDIT)
    assert not Payment.objects.filter(status=PaymentStatus.VOIDED).exists()


def test_undo_of_a_new_deposit_is_refused(billing_user: Any) -> None:
    txn = deposit("50.00")
    assert services.undo(txn.pk, actor=billing_user).message == services.NOT_APPLIED


# --- Auto-confirm -----------------------------------------------------------------------------


def exact_world() -> tuple[Lease, Any]:
    lease = lease_at(1991)
    charge = a_rent_charge(lease, OCTOBER, due="1200.00")
    an_invoice(owner(lease), 501, "1200.00", rent_charge=charge)
    a_rent_charge(lease_at(7), OCTOBER, due="640.00")
    return lease, charge


ROWS_EXACT = [
    {
        "external_id": "x-1",
        "posted_on": "2026-10-09",
        "amount": "1200.00",
        "description": "WIRE INV-2026-0501",
        "kind": "wire",
    },
    {"external_id": "x-2", "posted_on": "2026-10-09", "amount": "640.00", "description": "ZELLE"},
]


def test_auto_confirm_only_exact_and_only_when_on(monkeypatch: pytest.MonkeyPatch) -> None:
    exact_world()
    monkeypatch.setattr(services, "get_source", lambda: sources.FakeSource(ROWS_EXACT))
    a_connection()
    enable(auto=False)
    report = services.fetch(TODAY)
    assert (report.ok, report.auto_confirmed) == (True, 0)
    exact = BankTransaction.objects.get(external_id="x-1")
    weak = BankTransaction.objects.get(external_id="x-2")
    assert (exact.status, exact.confidence) == (Status.NEW, Confidence.EXACT)
    assert (weak.status, weak.confidence) == (Status.NEW, Confidence.WEAK)
    enable(auto=True)
    report = services.fetch(TODAY)
    assert report.auto_confirmed == 1
    assert "1 confirmed automatically" in report.message
    exact.refresh_from_db()
    weak.refresh_from_db()
    assert (exact.status, exact.confirmed_by) == (Status.CONFIRMED, None)
    assert weak.status == Status.NEW
    audit = AuditLog.objects.get(action=services.AUTO_CONFIRMED_ACTION)
    assert (audit.actor, audit.entity_id) == (None, str(exact.pk))


# --- Link and the settings card ----------------------------------------------------------------


def linked_plaid(monkeypatch: pytest.MonkeyPatch) -> Plaid:
    fake = Plaid()
    fake.on(plaid.LINK_TOKEN_PATH, ok({"link_token": FAKE_LINK_TOKEN}))
    fake.on(plaid.EXCHANGE_PATH, ok({"access_token": FAKE_ACCESS_TOKEN, "item_id": "item-1"}))
    fake.on(
        plaid.ACCOUNTS_PATH,
        ok({"accounts": [{"account_id": "acct-1", "mask": "4321", "type": "depository"}]}),
    )
    counting(fake, monkeypatch)
    return fake


def test_link_stores_the_token_encrypted_and_is_audited(
    plaid_keys: None, monkeypatch: pytest.MonkeyPatch, admin_user: Any
) -> None:
    linked_plaid(monkeypatch)
    enable()
    outcome, token = services.create_link_token(admin_user)
    assert (outcome.ok, token) == (True, FAKE_LINK_TOKEN)
    result = services.link(admin_user, FAKE_PUBLIC_TOKEN, "acct-1", "Example Credit Union")
    assert result.ok, result.message
    row = BankConnection.objects.get(active=True)
    assert FAKE_ACCESS_TOKEN not in row.access_token_encrypted
    assert crypto.decrypt(row.access_token_encrypted) == FAKE_ACCESS_TOKEN
    assert (row.mask, row.institution, row.linked_by) == (
        "4321",
        "Example Credit Union",
        admin_user,
    )
    audit = AuditLog.objects.get(action=services.LINKED_ACTION)
    assert FAKE_ACCESS_TOKEN not in json.dumps(audit.after)
    assert row.access_token_encrypted not in json.dumps(audit.after)
    # One connection at a time.
    assert services.link(admin_user, FAKE_PUBLIC_TOKEN, "acct-1", "").message == (
        services.LINK_EXISTS
    )


def test_link_needs_admin_and_the_module_on(
    plaid_keys: None, monkeypatch: pytest.MonkeyPatch, admin_user: Any, billing_user: Any
) -> None:
    fake = linked_plaid(monkeypatch)
    assert services.create_link_token(billing_user)[0].message == services.NOT_ALLOWED
    assert services.create_link_token(admin_user)[0].message == services.LINK_OFF
    assert fake.requests == []


def test_disconnect_clears_the_token(plaid_keys: None, admin_user: Any) -> None:
    row = a_connection(Source.PLAID, crypto.encrypt(FAKE_ACCESS_TOKEN))
    assert services.disconnect(admin_user).ok
    row.refresh_from_db()
    assert (row.active, row.access_token_encrypted) == (False, "")
    assert AuditLog.objects.filter(action=services.DISCONNECTED_ACTION).count() == 1
    assert services.disconnect(admin_user).message == services.NOT_CONNECTED


def settings_page(client: Client) -> str:
    response = client.get(reverse("ledger:settings:index"))
    assert response.status_code == 200
    return response.content.decode()


def test_the_card_says_the_keys_are_missing() -> None:
    html = settings_page(client_for(Role.ADMIN)[0])
    assert services.KEYS_MISSING in html
    assert "cdn.plaid.com/link/v2/stable/link-initialize.js" not in html


def test_the_card_says_the_keys_are_present_and_shows_no_value(plaid_keys: None) -> None:
    enable()
    html = settings_page(client_for(Role.ADMIN)[0])
    assert services.KEYS_PRESENT in html
    assert 'id="bank-connect"' in html
    assert '<script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js">' in html
    for value in (FAKE_CLIENT_ID, FAKE_SECRET, FAKE_TOKEN_KEY):
        assert value not in html


def test_no_page_shows_the_token(plaid_keys: None) -> None:
    stored = crypto.encrypt(FAKE_ACCESS_TOKEN)
    a_connection(Source.PLAID, stored)
    enable()
    client = client_for(Role.ADMIN)[0]
    for html in (settings_page(client), client.get(reverse("banking:page")).content.decode()):
        assert stored not in html
        assert FAKE_ACCESS_TOKEN not in html
        assert "item-fake" not in html
    assert "Connected: Example Bank ending 0000" in settings_page(client)


def test_the_switches_are_saved_and_audited() -> None:
    client = client_for(Role.ADMIN)[0]
    response = client.post(reverse("ledger:settings:bank_save"), {"bank_deposits_enabled": "1"})
    assert response.status_code == 302
    row = CompanySettings.current()
    assert (row.bank_deposits_enabled, row.bank_auto_confirm_exact) == (True, False)
    audit = AuditLog.objects.get(action="company_settings.updated")
    assert audit.after == {"bank_deposits_enabled": True, "bank_auto_confirm_exact": False}


def test_the_fake_connects_locally_with_one_click() -> None:
    enable()
    client = client_for(Role.ADMIN)[0]
    html = settings_page(client)
    assert 'value="fake-public-token"' in html
    response = client.post(reverse("banking:link_exchange"), {"public_token": "fake-public-token"})
    assert response.status_code == 302
    row = BankConnection.objects.get(active=True)
    assert (row.source, row.institution, row.access_token_encrypted) == (
        Source.FAKE,
        "Fake Bank",
        sources.FAKE_TOKEN_MARKER,
    )


# --- The pages and the roles -------------------------------------------------------------------


def test_the_page_shows_the_readiness_sentence_and_the_suggestion() -> None:
    lease = lease_at(1991, "Istanbul")
    charge = a_rent_charge(lease, OCTOBER, due="1200.00")
    an_invoice(owner(lease), 501, "1200.00", rent_charge=charge)
    deposit("1200.00", "WIRE INV-2026-0501", counterparty="FATIH TRADING LLC")
    html = client_for(Role.BILLING)[0].get(reverse("banking:page")).content.decode()
    assert services.OFF in html
    assert "#1991 Istanbul" in html
    assert 'class="badge badge-paid">Exact</span>' in html
    assert "Remember FATIH TRADING LLC for #1991 Istanbul" in html


def test_confirm_through_the_page(billing_user: Any) -> None:
    lease = lease_at(5)
    charge = a_rent_charge(lease, OCTOBER, due="1200.00")
    txn = deposit("1200.00", counterparty="ACME STORES")
    client = client_for(Role.BILLING)[0]
    response = client.post(
        reverse("banking:confirm", kwargs={"pk": txn.pk}),
        {"lease": str(lease.pk), "charge_type": "rent", "charge": str(charge.pk), "remember": "1"},
    )
    assert response.status_code == 302
    txn.refresh_from_db()
    assert txn.status == Status.CONFIRMED
    assert SenderAlias.objects.filter(lease=lease, normalised=normalise("ACME STORES")).exists()


def test_choose_and_split_through_the_pages() -> None:
    first, second = lease_at(21), lease_at(22)
    one = a_rent_charge(first, OCTOBER, due="400.00")
    two = a_rent_charge(second, OCTOBER, due="600.00")
    client = client_for(Role.BILLING)[0]
    txn = deposit("400.00")
    page = client.get(reverse("banking:choose", kwargs={"pk": txn.pk}), {"store": "21"})
    assert page.status_code == 200
    assert f'value="rent:{one.pk}"' in page.content.decode()
    response = client.post(
        reverse("banking:choose", kwargs={"pk": txn.pk}),
        {"lease": str(first.pk), "charge": f"rent:{one.pk}"},
    )
    assert response.status_code == 302
    txn.refresh_from_db()
    assert txn.status == Status.CONFIRMED
    other = deposit("1000.00")
    page = client.get(reverse("banking:split", kwargs={"pk": other.pk}), {"stores": "21, 22"})
    assert page.status_code == 200
    html = page.content.decode()
    assert f'name="amount_{second.store_id}_{two.pk}"' in html
    first_line = a_rent_charge(first, date(2026, 11, 1), due="400.00")
    response = client.post(
        reverse("banking:split", kwargs={"pk": other.pk}),
        {
            f"amount_{first.store_id}_{first_line.pk}": "400.00",
            f"amount_{second.store_id}_{two.pk}": "600.00",
        },
    )
    assert response.status_code == 302
    other.refresh_from_db()
    assert other.status == Status.SPLIT


ROUTE_ROLES: dict[str, tuple[str, Role]] = {
    "banking:page": ("get", Role.BILLING),
    "banking:choose": ("get", Role.BILLING),
    "banking:split": ("get", Role.BILLING),
    "banking:confirm": ("post", Role.BILLING),
    "banking:ignore": ("post", Role.BILLING),
    "banking:undo": ("post", Role.BILLING),
    "banking:fetch_now": ("post", Role.BILLING),
    "banking:link_token": ("post", Role.ADMIN),
    "banking:link_exchange": ("post", Role.ADMIN),
    "banking:disconnect": ("post", Role.ADMIN),
    "ledger:settings:bank_save": ("post", Role.ADMIN),
}
BELOW = {Role.BILLING: Role.VIEWER, Role.ADMIN: Role.BILLING}


def test_every_banking_route_is_listed_here() -> None:
    from tests.auth_helpers import iter_routes

    names = {name for name, _, _ in iter_routes() if name and name.startswith("banking:")}
    assert names == {name for name in ROUTE_ROLES if name.startswith("banking:")}


@pytest.mark.parametrize("name", sorted(ROUTE_ROLES))
def test_the_roles_on_every_route(name: str) -> None:
    method, role = ROUTE_ROLES[name]
    txn = deposit("10.00")
    kwargs = (
        {"pk": txn.pk}
        if name.split(":")[-1] in {"choose", "split", "confirm", "ignore", "undo"}
        else {}
    )
    url = reverse(name, kwargs=kwargs)
    below = client_for(BELOW[role])[0]
    assert getattr(below, method)(url).status_code == 403
    allowed = client_for(role)[0]
    assert getattr(allowed, method)(url).status_code != 403


def test_the_page_queries_do_not_grow_with_the_rows() -> None:
    lease = lease_at(1991)
    charge = a_rent_charge(lease, OCTOBER, due="1200.00")
    an_invoice(owner(lease), 501, "1200.00", rent_charge=charge)
    SenderAlias.objects.create(lease=lease, text="ACME", normalised="ACME")
    client = client_for(Role.BILLING)[0]
    deposit("1200.00", "INV-2026-0501", counterparty="ACME")
    client.get(reverse("banking:page"))

    def measure(status: str) -> int:
        with CaptureQueriesContext(connection) as captured:
            assert client.get(reverse("banking:page"), {"status": status}).status_code == 200
        return len(captured.captured_queries)

    small = measure("new")
    for _ in range(5):
        deposit("1200.00", "INV-2026-0501", counterparty="ACME")
    assert measure("new") == small
    billing = client_for(Role.BILLING)[1]
    for txn in BankTransaction.objects.all()[:1]:
        services.confirm(txn.pk, services.Choice(lease.pk, "rent", charge.pk), actor=billing)
    one_recorded = measure("all")
    other = a_rent_charge(lease_at(8), OCTOBER, due="5.00")
    extra = deposit("5.00")
    services.confirm(extra.pk, services.Choice(other.lease_id, "rent", other.pk), actor=billing)
    assert measure("all") == one_recorded


# --- Migrations -------------------------------------------------------------------------------


def _migrate(targets: list[tuple[str, str | None]]) -> None:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(targets)


@pytest.fixture
def leaves() -> Iterator[list[tuple[str, str]]]:
    found = MigrationExecutor(connection).loader.graph.leaf_nodes()
    yield found
    _migrate(found)


TABLES = {"bank_connections", "bank_transactions", "sender_aliases"}


@pytest.mark.django_db(transaction=True)
def test_the_banking_migrations_go_both_ways(leaves: list[tuple[str, str]]) -> None:
    _migrate([("banking", None)])
    assert not TABLES & set(connection.introspection.table_names())
    _migrate([("ledger", "0064_portal_messages")])
    columns = {
        column.name
        for column in connection.introspection.get_table_description(
            connection.cursor(), "company_settings"
        )
    }
    assert not {"bank_deposits_enabled", "bank_auto_confirm_exact"} & columns
    _migrate(leaves)  # type: ignore[arg-type]
    assert TABLES <= set(connection.introspection.table_names())
    lease = lease_at(5)
    SenderAlias.objects.create(lease=lease, text="ACME", normalised="ACME")
    with pytest.raises(RuntimeError, match="refuses to reverse"):
        _migrate([("banking", None)])
    SenderAlias.objects.all().delete()


GUARDED: list[tuple[Callable[[str], Any], str]] = [
    (lambda text: deposit("1.00", description=text), "description"),
    (lambda text: deposit("1.00", counterparty=text), "counterparty"),
    (
        lambda text: SenderAlias.objects.create(lease=lease_at(9), text=text, normalised="X"),
        "text",
    ),
]


@pytest.mark.parametrize(("make", "column"), GUARDED, ids=[column for _, column in GUARDED])
def test_the_free_text_columns_refuse_a_card_number(
    make: Callable[[str], Any], column: str
) -> None:
    from django.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        make("ref 4111 1111 1111 1111")

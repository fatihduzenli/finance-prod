"""PLAN-014b item 1 (Fable's decisions 1 to 8): the New lease page.

Decision 1, the "E-mail invoice" channel and ledger migration 0070 (forward and back, on
sqlite and on PostgreSQL: run this file on both; the reverse refused while a lease or a
transfer holds ``invoice``).
"""

from __future__ import annotations

import html
import importlib
import re
import uuid
from datetime import date
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import Client
from django.utils import timezone

from accounts.roles import Role
from ledger.models import (
    FeeType,
    InvoiceDraft,
    InvoiceDraftStatus,
    Lease,
    OneOffCharge,
    Operator,
    PaymentChannel,
    RentCharge,
    Store,
    StoreStatus,
)
from ledger.services import move_in, proration, rent_run
from tests.receivables_helpers import a_lease, a_store, an_operator
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db


# --- Decision 1: the channel and migration 0070 -----------------------------------------------

MIGRATION_0070 = importlib.import_module("ledger.migrations.0070_payment_channel_invoice")
BEFORE_0070 = ("ledger", "0069_pay_line_and_settings_holds")
AFTER_0070 = ("ledger", "0070_payment_channel_invoice")


def _migrate(*targets: tuple[str, str]) -> Any:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(list(targets))
    return MigrationExecutor(connection).loader.project_state(list(targets)).apps


def test_the_invoice_channel_is_e_mail_invoice_and_0070_adds_no_guarded_column() -> None:
    assert (PaymentChannel.INVOICE.value, PaymentChannel.INVOICE.label) == (
        "invoice",
        "E-mail invoice",
    )
    assert MIGRATION_0070.GUARDED_COLUMNS == {}
    assert "invoice" not in [value for value, _ in MIGRATION_0070.BEFORE]
    assert [value for value, _ in MIGRATION_0070.AFTER][-1] == "invoice"
    # The earlier migrations that read the channels from the model keep what they applied.
    for name in ("0037_transfer_fields", "0046_copy_labels"):
        module = importlib.import_module(f"ledger.migrations.{name}")
        assert module.CHANNELS == MIGRATION_0070.BEFORE, name


def test_a_lease_saves_the_invoice_channel_and_an_unknown_value_is_refused() -> None:
    lease = a_lease(a_store("7001", number=7001), channel=PaymentChannel.INVOICE)
    lease.refresh_from_db()
    assert lease.payment_channel == "invoice"
    with pytest.raises(IntegrityError), transaction.atomic():
        Lease.objects.filter(pk=lease.pk).update(payment_channel="e-mail")


@pytest.mark.django_db(transaction=True)
def test_0070_forward_and_back_and_the_old_check_refuses_invoice() -> None:
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        old = _migrate(BEFORE_0070)
        store = old.get_model("ledger", "Store").objects.create(
            external_label="7002", status="active"
        )
        operator = old.get_model("ledger", "Operator").objects.create(display_name="Back And Forth")
        model = old.get_model("ledger", "Lease")
        with pytest.raises(IntegrityError), transaction.atomic():
            model.objects.create(
                store=store,
                operator=operator,
                debit_day=1,
                collection_status="prospective",
                payment_channel="invoice",
            )
        new = _migrate(AFTER_0070)
        row = new.get_model("ledger", "Lease").objects.create(
            store_id=store.pk,
            operator_id=operator.pk,
            debit_day=1,
            collection_status="active",
            payment_channel="invoice",
        )
        new.get_model("ledger", "Lease").objects.filter(pk=row.pk).update(payment_channel="other")
        back = _migrate(BEFORE_0070)
        assert back.get_model("ledger", "Lease").objects.get(pk=row.pk).payment_channel == "other"
    finally:
        _migrate(*leaves)
        Lease.objects.all().delete()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("holder", ["lease", "transfer"])
def test_0070_reverse_is_refused_while_a_lease_or_a_transfer_holds_invoice(holder: str) -> None:
    from ledger.models import LeaseTransfer

    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    lease = a_lease(a_store("7003", number=7003), an_operator("Holds Invoice"))
    if holder == "lease":
        Lease.objects.filter(pk=lease.pk).update(payment_channel=PaymentChannel.INVOICE)
    else:
        LeaseTransfer.objects.create(
            store=lease.store, outgoing_lease=lease, incoming_channel=PaymentChannel.INVOICE
        )
    try:
        with pytest.raises(RuntimeError, match="refuses to reverse"):
            _migrate(BEFORE_0070)
    finally:
        _migrate(*leaves)
        LeaseTransfer.objects.all().delete()
        Lease.objects.all().delete()


# --- Decisions 2 to 8: the page ---------------------------------------------------------------

TODAY = date(2026, 11, 3)
NEW = "/leases/new/"


@pytest.fixture
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    return TODAY


@pytest.fixture
def admin() -> Client:
    return client_for(Role.ADMIN, "admin-new-lease")[0]


def vacant_store(number: int = 437, name: str = "Test Store 5") -> Store:
    return Store.objects.create(
        external_label=f"{number} {name}", store_number=number, status=StoreStatus.ACTIVE
    )


def create_path(store: Store, **query: str) -> str:
    return f"{NEW}?{urlencode({'store': str(store.pk), **query})}"


def page(client: Client, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code)
    return response.content.decode()


def deposit_type() -> FeeType:
    return FeeType.objects.get(code="security_deposit")


def create_data(store: Store, operator: Operator | None, **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "creation_key": str(uuid.uuid4()),
        "operator": str(operator.pk) if operator is not None else "",
        "debit_day": "1",
        "second_debit_day": "",
        "payment_channel": PaymentChannel.PROVIDER_ACH,
        "started_on": "2026-11-15",
        "prorate_first_month": "on",
        "rent": "2000.00",
        "account": "later",
    }
    return {**data, **overrides}


def post_create(client: Client, store: Store, data: dict[str, Any]) -> Any:
    return client.post(create_path(store), data)


def tag_with(html_text: str, *parts: str) -> bool:
    """Whether one HTML tag of the page holds every one of ``parts``."""
    return any(all(part in tag for part in parts) for tag in re.findall(r"<[^>]+>", html_text))


def test_the_page_has_five_numbered_cards_the_header_and_the_summary(
    admin: Client, today: date
) -> None:
    store = vacant_store()
    html_text = page(admin, create_path(store))
    titles = re.findall(
        r'<span class="step-number" aria-hidden="true">(\d)</span>'
        r'<div class="step-title"><h2 id="[a-z-]+">([^<]+)</h2>',
        html_text,
    )
    assert titles == [
        ("1", "Operator"),
        ("2", "Terms"),
        ("3", "Collection"),
        ("4", "Billing details"),
        ("5", "Move-in charges"),
    ]
    for note in (
        "Who takes the store.",
        "Rent, start and deposit.",
        "When and how rent is collected.",
        "Who this store is billed to. Each store can use its own company, e-mail and bank account.",
        "One-time charges billed with the first invoice. Leave a price empty to skip it.",
    ):
        assert note in html_text
    assert "New lease for #437 Test Store 5" in html_text
    assert 'id="lease-summary"' in html_text and ">Create lease</button>" in html_text
    assert "After saving you can send the move-in invoice by e-mail." in html_text
    assert tag_with(html_text, 'id="cancel-lease"', f'href="/stores/{store.pk}/"')


def an_operator_with(name: str, **fields: Any) -> Operator:
    operator = Operator.objects.create(display_name=name, **fields)
    return operator


def test_no_status_and_no_rent_date_on_create_both_on_edit(admin: Client, today: date) -> None:
    store = vacant_store()
    html_text = page(admin, create_path(store))
    assert 'name="collection_status"' not in html_text
    assert 'name="rent_effective_from"' not in html_text
    assert "New rent takes effect on" not in html_text
    lease = a_lease(a_store("7101", number=7101), an_operator("Edit Me"))
    edit = page(admin, f"/leases/{lease.pk}/edit/")
    assert 'name="collection_status"' in edit and "New rent takes effect on" in edit
    # The edit select keeps every channel and gains "E-mail invoice".
    assert tag_with(edit, 'value="invoice"') and ">E-mail invoice</option>" in edit
    assert tag_with(edit, 'value="zelle"')


def test_a_saved_lease_is_active_and_the_debit_day_starts_at_the_1st(
    admin: Client, today: date
) -> None:
    store = vacant_store()
    html_text = page(admin, create_path(store))
    assert tag_with(html_text, '<option value="1"', "selected") and ">1st</option>" in html_text
    assert ">28th</option>" in html_text and ">29th</option>" not in html_text
    operator = an_operator("Active Operator")
    data = create_data(store, operator, collection_status="prospective")
    assert post_create(admin, store, data).status_code == 302
    lease = Lease.objects.get(store=store)
    assert lease.collection_status == "active" and lease.debit_day == 1


def test_debit_day_29_is_refused(admin: Client, today: date) -> None:
    store = vacant_store()
    response = post_create(admin, store, create_data(store, an_operator("P"), debit_day="29"))
    assert response.status_code == 302 and not Lease.objects.filter(store=store).exists()
    assert "Choose one of the options." in page(admin, create_path(store))


@pytest.mark.parametrize(
    ("card", "value"),
    [("0", "stax"), ("1", "invoice"), ("2", "other")],
)
def test_the_three_channel_cards_map_to_their_values(
    admin: Client, today: date, card: str, value: str
) -> None:
    store = vacant_store()
    html_text = page(admin, create_path(store))
    assert tag_with(html_text, f'id="id_payment_channel_{card}"', f'value="{value}"')
    post_create(admin, store, create_data(store, an_operator("C"), payment_channel=value))
    assert Lease.objects.get(store=store).payment_channel == value


def test_the_cards_name_the_provider_from_the_settings(admin: Client, today: date) -> None:
    from ledger.wording import provider_name

    html_text = page(admin, create_path(vacant_store()))
    assert f"Paid outside {provider_name()}" in html_text
    assert f"{provider_name()} e-mails each month&#x27;s invoice" in html_text
    assert "Autopay (bank debit)" in html_text and "Debited from the store" in html_text


@pytest.mark.parametrize("channel", ["zelle", "wire", "unknown"])
def test_another_channel_is_refused_on_create_and_accepted_on_edit(
    admin: Client, today: date, channel: str
) -> None:
    store = vacant_store()
    post_create(admin, store, create_data(store, an_operator("Z"), payment_channel=channel))
    assert not Lease.objects.filter(store=store).exists()
    lease = a_lease(a_store("7102", number=7102), an_operator("Edit Z"))
    if channel != "unknown":
        data = {
            "store": str(lease.store_id),
            "operator": str(lease.operator_id),
            "debit_day": "5",
            "second_debit_day": "",
            "payment_channel": channel,
            "started_on": "2025-01-01",
            "collection_status": "active",
            "rent": "3500.00",
            "rent_effective_from": "",
        }
        assert admin.post(f"/leases/{lease.pk}/edit/", data).status_code == 302
        lease.refresh_from_db()
        assert lease.payment_channel == channel


def test_the_deposit_is_the_refundable_types_price_in_terms(admin: Client, today: date) -> None:
    store = vacant_store()
    html_text = page(admin, create_path(store))
    terms = html_text[html_text.index('id="terms-step"') : html_text.index('id="collection-step"')]
    assert '<label for="id_setup_security_deposit_price">Deposit</label>' in terms
    assert terms.count('name="setup_security_deposit_price"') == 1
    assert html_text.count('name="setup_security_deposit_price"') == 1
    assert 'id="deposit-mirror"' in html_text
    operator = an_operator("Deposit Payer")
    post_create(admin, store, create_data(store, operator, setup_security_deposit_price="500"))
    lease = Lease.objects.get(store=store)
    [charge] = OneOffCharge.objects.filter(lease=lease)
    assert (charge.fee_type, charge.price) == (deposit_type(), Decimal("500.00"))
    other = vacant_store(438, "Blank Deposit")
    post_create(admin, other, create_data(other, operator))
    assert not OneOffCharge.objects.filter(lease__store=other).exists()


def test_no_deposit_field_without_an_active_refundable_type(admin: Client, today: date) -> None:
    FeeType.objects.filter(refundable=True).update(active=False)
    terms = page(admin, create_path(vacant_store()))
    assert ">Deposit</label>" not in terms and 'id="deposit-mirror"' not in terms


def test_add_item_rows_are_listed_hidden_and_charge_only_with_a_price(
    admin: Client, today: date
) -> None:
    extra = FeeType.objects.create(
        code="window_film", name="Window film", show_in_setup=False, default_price=Decimal("80")
    )
    store = vacant_store()
    html_text = page(admin, create_path(store))
    assert tag_with(html_text, 'id="setup-window_film"', "data-extra")
    assert tag_with(html_text, 'id="add-item"', "hidden")
    assert "usual 80.00" in html_text
    operator = an_operator("Extras")
    post_create(admin, store, create_data(store, operator))
    assert not OneOffCharge.objects.filter(fee_type=extra).exists()
    other = vacant_store(439, "Priced Extra")
    post_create(admin, other, create_data(other, operator, setup_window_film_price="80.00"))
    assert OneOffCharge.objects.get(fee_type=extra).lease.store == other


def test_operator_query_preselects_an_active_operator_only(admin: Client, today: date) -> None:
    store = vacant_store()
    chosen = an_operator("Chosen One")
    html_text = page(admin, create_path(store, operator=str(chosen.pk)))
    assert tag_with(html_text, f'value="{chosen.pk}"', "selected")
    archived = an_operator("Archived One")
    archived.archived_at = timezone.now()
    archived.save()
    for raw in (str(archived.pk), str(uuid.uuid4()), "not-a-uuid"):
        html_text = page(admin, create_path(store, operator=raw))
        select = html_text.split('id="id_operator"', 1)[1].split("</select>", 1)[0]
        assert not re.search(r'<option value="[0-9a-f-]{36}"[^>]*selected', select), raw


def test_options_carry_initials_phone_and_the_other_current_stores(
    admin: Client, today: date
) -> None:
    store = vacant_store()
    operator = an_operator_with("Fatih Duzenli", phone="2015550142", email="a@b.com")
    a_lease(a_store("1991 Istanbul", number=1991), operator, started=date(2026, 1, 1))
    ended = a_lease(a_store("1999 Ankara", number=1999), operator, started=date(2025, 1, 1))
    Lease.objects.filter(pk=ended.pk).update(ended_on=date(2026, 6, 30), collection_status="ended")
    html_text = page(admin, create_path(store))
    assert tag_with(
        html_text,
        f'value="{operator.pk}"',
        'data-initials="FD"',
        'data-phone-text="(201) 555-0142"',
        'data-stores="#1991 Istanbul"',
        'data-store-count="1"',
    )
    assert "#1999" not in html_text.split('id="id_operator"', 1)[1].split("</select>", 1)[0]


def test_the_option_stores_are_read_in_one_query(admin: Client, today: date) -> None:
    from django.db import connection as db
    from django.test.utils import CaptureQueriesContext

    store = vacant_store()
    for index in range(3):
        operator = an_operator(f"Many {index}")
        a_lease(a_store(f"80{index}", number=800 + index), operator)
    with CaptureQueriesContext(db) as few:
        page(admin, create_path(store))
    for index in range(6):
        operator = an_operator(f"More {index}")
        a_lease(a_store(f"81{index}", number=810 + index), operator)
    with CaptureQueriesContext(db) as more:
        page(admin, create_path(store))
    assert len(more) == len(few)


def test_operator_create_returns_to_the_new_lease_page_with_the_operator(
    admin: Client, today: date
) -> None:
    store = vacant_store()
    target = create_path(store)
    html_text = page(admin, target)
    assert tag_with(html_text, 'id="new-operator"', 'data-dialog-open="operator-form"')
    assert tag_with(html_text, 'name="next"', f'value="{html.escape(target)}"')
    assert 'id="op_display_name"' in html_text
    response = admin.post(
        "/operators/new/",
        {"creation_key": str(uuid.uuid4()), "display_name": "Brand New", "next": target},
    )
    operator = Operator.objects.get(display_name="Brand New")
    assert response.status_code == 302
    assert response["Location"] == f"{target}&operator={operator.pk}"
    assert tag_with(page(admin, response["Location"]), f'value="{operator.pk}"', "selected")


@pytest.mark.parametrize(
    "next_url",
    [
        "https://evil.example.com/leases/new/",
        "//evil.example.com/leases/new/",
        "/\\evil.example.com/",
        "/leases/new/?store=x\\y",
        "/portal/",
        "/portal/home/",
        "/portal",
        "javascript:alert(1)",
    ],
)
def test_operator_create_refuses_an_unsafe_next(admin: Client, today: date, next_url: str) -> None:
    response = admin.post(
        "/operators/new/",
        {"creation_key": str(uuid.uuid4()), "display_name": "Safe Next", "next": next_url},
    )
    operator = Operator.objects.get(display_name="Safe Next")
    assert response["Location"] == f"/operators/{operator.pk}/"


def test_a_refused_operator_keeps_its_next(admin: Client, today: date) -> None:
    target = create_path(vacant_store())
    response = admin.post(
        "/operators/new/", {"creation_key": str(uuid.uuid4()), "display_name": "", "next": target}
    )
    assert response["Location"] == "/operators/new/?" + urlencode({"next": target})
    assert tag_with(page(admin, response["Location"]), 'name="next"', 'value="')


def shared_world() -> tuple[Lease, Operator]:
    """Operator A's #1991 holds its own customer and a usable bank method (shareable)."""
    from django.utils import timezone as tz

    from payments import lease_sync
    from payments.models import ExternalRef, ProviderSnapshot

    operator = an_operator("Holder A")
    holder = a_lease(a_store("1991", number=1991), operator, started=date(2026, 1, 1))
    customer = ExternalRef.objects.create(
        entity_type=lease_sync.ENTITY_LEASE,
        entity_id=holder.pk,
        provider=lease_sync.PROVIDER_KEY,
        external_kind=lease_sync.KIND_CUSTOMER,
        external_id="cust-1991",
    )
    ExternalRef.objects.create(
        entity_type=lease_sync.ENTITY_LEASE,
        entity_id=holder.pk,
        provider=lease_sync.PROVIDER_KEY,
        external_kind=lease_sync.KIND_PAYMENT_METHOD,
        external_id="pm-1991",
    )
    ProviderSnapshot.objects.create(
        external_ref=customer,
        fetched_at=tz.now(),
        payload=[
            {
                "external_id": "pm-1991",
                "raw_status": "",
                "method": "bank",
                "last_four": "4821",
                "bank_type": "checking",
                "nickname": "",
                "is_default": True,
            }
        ],
    )
    return holder, operator


def test_same_radios_carry_their_operator_and_the_short_label(admin: Client, today: date) -> None:
    holder, operator = shared_world()
    html_text = page(admin, create_path(vacant_store()))
    assert tag_with(
        html_text,
        f'value="same:{holder.pk}"',
        f'data-operator="{operator.pk}"',
        f'data-shown-when="id_operator={operator.pk}"',
    )
    assert "Use #1991&#x27;s account" in html_text and " Enter a new account</label>" in html_text


def test_the_account_of_another_operators_lease_is_refused(admin: Client, today: date) -> None:
    from ledger.services.leases import ACCOUNT_NOT_SHAREABLE

    holder, _ = shared_world()
    store = vacant_store()
    other = an_operator("Other B")
    post_create(admin, store, create_data(store, other, account=f"same:{holder.pk}"))
    assert not Lease.objects.filter(store=store).exists()
    assert html.escape(ACCOUNT_NOT_SHAREABLE) in page(admin, create_path(store))


def test_the_account_of_the_chosen_operators_lease_is_accepted(admin: Client, today: date) -> None:
    holder, operator = shared_world()
    store = vacant_store()
    post_create(admin, store, create_data(store, operator, account=f"same:{holder.pk}"))
    assert Lease.objects.get(store=store).billing.shares_account_of == holder


def test_the_header_reads_vacant_with_the_days_and_the_breadcrumb_is_unchanged(
    admin: Client, today: date
) -> None:
    store = vacant_store()
    html_text = page(admin, create_path(store))
    assert "Vacant · never leased" in html_text
    a_lease(store, an_operator("Gone"), started=date(2026, 1, 1))
    Lease.objects.filter(store=store).update(ended_on=date(2026, 10, 10), collection_status="ended")
    html_text = page(admin, create_path(store))
    assert re.search(
        r'id="new-lease-title">New lease for #437 Test Store 5 <span class="badge[^"]*"'
        r"[^>]*>.*Vacant · 24 days</span>",
        html_text,
    )
    assert f'<a href="/stores/{store.pk}/" id="store-crumb">#437</a>' in html_text
    assert '<span aria-current="page">New lease</span>' in html_text


# --- Decision 6: the move-in invoice -----------------------------------------------------------


def connect(lease: Lease) -> None:
    """The lease's own processor customer, as the billing sync links it (Send needs one)."""
    from payments import lease_sync
    from payments.models import ExternalRef

    ExternalRef.objects.create(
        entity_type=lease_sync.ENTITY_LEASE,
        entity_id=lease.pk,
        provider=lease_sync.PROVIDER_KEY,
        external_kind=lease_sync.KIND_CUSTOMER,
        external_id=f"cust-{lease.pk.hex[:8]}",
    )


def saved_draft(admin: Client, store: Store, operator: Operator, **overrides: Any) -> Any:
    response = post_create(
        admin,
        store,
        create_data(
            store,
            operator,
            setup_security_deposit_price="3000",
            setup_sign_fee_price="500",
            **overrides,
        ),
    )
    return response


def test_the_move_in_draft_has_the_prorated_rent_line_and_one_line_per_setup_charge(
    admin: Client, today: date
) -> None:
    store = vacant_store()
    operator = an_operator("Mover In")
    response = saved_draft(admin, store, operator)
    lease = Lease.objects.get(store=store)
    draft = InvoiceDraft.objects.get(lease=lease)
    assert response["Location"] == (
        f"/stores/{store.pk}/?tab=invoices&lease_created={draft.pk}#tab-invoices"
    )
    lines = list(draft.lines.order_by("position"))
    assert [(line.rent_lease_id, line.rent_month) for line in lines[:1]] == [
        (lease.pk, date(2026, 11, 1))
    ]
    expected = proration.prorated(Decimal("2000.00"), date(2026, 11, 1), 15, 30).amount
    assert lines[0].unit_amount == expected == Decimal("1066.67")
    charges = set(OneOffCharge.objects.filter(lease=lease).values_list("pk", flat=True))
    assert {line.existing_charge_id for line in lines[1:]} == charges and len(charges) == 2
    assert draft.due_date == date(2026, 11, 15) and draft.status == InvoiceDraftStatus.DRAFT


def test_without_proration_the_rent_line_is_the_rent(admin: Client, today: date) -> None:
    store = vacant_store()
    data = create_data(store, an_operator("Full Month"))
    data.pop("prorate_first_month")
    post_create(admin, store, data)
    [line] = InvoiceDraft.objects.get(lease__store=store).lines.all()
    assert line.unit_amount == Decimal("2000.00")


def test_the_dialog_names_the_operator_and_send_e_mails_the_draft(
    admin: Client, today: date
) -> None:
    store = vacant_store()
    operator = an_operator("Dialog Person")
    response = saved_draft(admin, store, operator)
    draft = InvoiceDraft.objects.get(lease__store=store)
    html_text = page(admin, response["Location"])
    assert 'id="lease-created"' in html_text and "Lease created" in html_text
    assert "Send the move-in invoice to Dialog Person now?" in html_text
    send = f"/invoices/drafts/{draft.pk}/send/?{urlencode({'store': str(store.pk)})}"
    assert tag_with(html_text, 'id="lease-created-form"', f'action="{html.escape(send)}"')
    assert tag_with(html_text, 'id="lease-created-later"', "tab=invoices")
    connect(draft.lease)
    assert admin.post(send).status_code == 302
    draft.refresh_from_db()
    assert draft.status != InvoiceDraftStatus.DRAFT
    # Sent, the dialog is not offered again.
    assert 'id="lease-created"' not in page(admin, response["Location"])


def test_not_now_leaves_the_draft_under_drafts(admin: Client, today: date) -> None:
    store = vacant_store()
    response = saved_draft(admin, store, an_operator("Later Person"))
    html_text = page(admin, f"/stores/{store.pk}/?tab=invoices")
    draft = InvoiceDraft.objects.get(lease__store=store)
    assert draft.status == InvoiceDraftStatus.DRAFT and draft.number in html_text
    assert 'id="lease-created"' not in html_text
    assert response.status_code == 302


def test_the_dialog_needs_a_draft_of_this_store_and_billing(admin: Client, today: date) -> None:
    store = vacant_store()
    saved_draft(admin, store, an_operator("Mine"))
    draft = InvoiceDraft.objects.get(lease__store=store)
    other = vacant_store(440, "Elsewhere")
    assert 'id="lease-created"' not in page(admin, f"/stores/{other.pk}/?lease_created={draft.pk}")
    assert 'id="lease-created"' not in page(admin, f"/stores/{store.pk}/?lease_created=nope")
    viewer = client_for(Role.VIEWER, "viewer-new-lease")[0]
    assert 'id="lease-created"' not in page(viewer, f"/stores/{store.pk}/?lease_created={draft.pk}")


@pytest.mark.parametrize("sent", [True, False])
def test_the_rent_run_creates_no_second_charge_for_the_start_month(
    admin: Client, today: date, sent: bool
) -> None:
    store = vacant_store()
    saved_draft(admin, store, an_operator("Run Twice"), debit_day="20")
    lease = Lease.objects.get(store=store)
    draft = InvoiceDraft.objects.get(lease=lease)
    connect(lease)
    if sent:
        admin.post(f"/invoices/drafts/{draft.pk}/send/?store={store.pk}")
        draft.refresh_from_db()
        assert draft.status != InvoiceDraftStatus.DRAFT
        [charge] = RentCharge.objects.filter(lease=lease)
        assert charge.billed_by_invoice_id is not None
    report = rent_run.create_charges(date(2026, 11, 20), actor=None)
    mine = [
        item
        for item in (*report.created, *report.already, *report.caught_up)
        if item.lease_id == lease.pk
    ]
    [charge] = RentCharge.objects.filter(lease=lease, period_month=date(2026, 11, 1))
    assert charge.amount_due == Decimal("1066.67")
    if sent:
        assert [item.pk for item in report.already if item.lease_id == lease.pk] == [charge.pk]
        assert not [
            item for item in (*report.created, *report.caught_up) if item.lease_id == lease.pk
        ]
    else:
        # Before the send the run makes the month's charge; the send then takes it.
        assert [item.pk for item in mine] == [charge.pk]
        admin.post(f"/invoices/drafts/{draft.pk}/send/?store={store.pk}")
        draft.refresh_from_db()
        assert draft.status != InvoiceDraftStatus.DRAFT
        assert RentCharge.objects.filter(lease=lease).count() == 1
        again = rent_run.create_charges(date(2026, 11, 20), actor=None)
        assert [item.pk for item in again.already if item.lease_id == lease.pk] == [charge.pk]


def test_a_refused_rent_line_saves_the_lease_without_a_draft_and_says_why(
    admin: Client, today: date
) -> None:
    store = vacant_store()
    response = post_create(
        admin, store, create_data(store, an_operator("Twice Monthly"), second_debit_day="15")
    )
    lease = Lease.objects.get(store=store)
    assert not InvoiceDraft.objects.filter(lease=lease).exists()
    assert response["Location"] == f"/stores/{store.pk}/"
    html_text = page(admin, response["Location"])
    assert "The move-in invoice could not be made: the lease of store" in html_text
    assert "debited twice a month" in html_text


def test_no_operator_makes_no_draft_and_one_sentence() -> None:
    lease = a_lease(a_store("7201", number=7201), status="prospective")
    Lease.objects.filter(pk=lease.pk).update(operator=None)
    lease.refresh_from_db()
    found = move_in.create_draft(lease, [], creation_key=uuid.uuid4(), actor=None)
    assert (found.draft, found.message) == (None, move_in.NO_OPERATOR)
    assert not InvoiceDraft.objects.exists()


def test_a_replayed_form_makes_one_lease_and_one_draft(admin: Client, today: date) -> None:
    store = vacant_store()
    operator = an_operator("Replay")
    data = create_data(store, operator, setup_sign_fee_price="500")
    first = post_create(admin, store, data)
    second = post_create(admin, store, data)
    assert Lease.objects.filter(store=store).count() == 1
    assert InvoiceDraft.objects.filter(lease__store=store).count() == 1
    assert first.status_code == second.status_code == 302


def test_the_summary_without_the_script(admin: Client, today: date) -> None:
    store = vacant_store()
    operator = an_operator("Summary Person")
    post_create(
        admin,
        store,
        create_data(
            store,
            operator,
            rent="",
            setup_security_deposit_price="3000",
            setup_sign_fee_price="500",
        ),
    )
    html_text = page(admin, create_path(store))
    assert 'id="summary-operator">Summary Person<' in html_text
    assert 'id="summary-starts">Nov 15, 2026<' in html_text
    assert 'id="summary-collected">Autopay · 1st<' in html_text
    assert 'id="setup-total">$3,500.00<' in html_text
    assert 'id="summary-total">—<' in html_text

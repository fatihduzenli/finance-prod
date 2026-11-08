"""PLAN-015c task 1 (Opus): the company settings the invoice and receipt PDFs read — the
address and "Invoice e-mails sent by" on Admin › Settings (the Company card), the logo's PNG
copy written when the logo is saved, the two seeded templates and the new message values, and
migration 0071 forward and back."""

from __future__ import annotations

import hashlib
import importlib
import io
import random
import uuid
from typing import Any

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from PIL import Image

import documents_storage
from accounts.roles import Role
from documents_storage.memory import MemoryDocumentStorage
from ledger import merge
from ledger.company_defaults import SEED
from ledger.models import (
    COMPANY_SETTINGS_ID,
    LOGO_PNG_MAX_BYTES,
    AuditLog,
    ChargeEntityType,
    CompanySettings,
    Document,
    InvoiceSender,
    MessageKind,
    MessageRule,
    MessageTemplate,
)
from ledger.services import mail_settings
from ledger.services.pdf import logo
from tests.auth_helpers import make_user, verified_client
from tests.receivables_helpers import a_store
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

SETTINGS = "/admin-settings/"
SAVE = "/admin-settings/mail/"
MIGRATION = importlib.import_module("ledger.migrations.0071_invoice_pdfs")
BEFORE = ("ledger", "0070_payment_channel_invoice")
AFTER = ("ledger", "0071_invoice_pdfs")


def seeded(code: str) -> MessageTemplate:
    """A template of 0071; written again from the migration when a transactional test flushed
    the table."""
    found = MessageTemplate.objects.filter(code=code).first()
    if found is not None:
        return found
    for seeded_code, name, subject, body in MIGRATION.TEMPLATES:
        if seeded_code == code:
            return MessageTemplate.objects.create(code=code, name=name, subject=subject, body=body)
    raise AssertionError(code)


def company_form(**overrides: str) -> dict[str, str]:
    form = {
        "section": "company",
        "address_line1": "100 Main Street",
        "address_line2": "Suite 200",
        "city": "Paterson",
        "state": "NJ",
        "postal_code": "07501",
        "invoice_sender": "company",
    }
    return {**form, **overrides}


def png(width: int = 40, height: int = 20, colour: tuple[int, int, int] = (20, 160, 140)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(out, format="PNG")
    return out.getvalue()


def jpeg(width: int = 40, height: int = 20) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(out, format="JPEG")
    return out.getvalue()


# --- The Company card ---------------------------------------------------------------------------


def test_the_company_card_names_the_processor_and_the_company_with_the_processor_chosen() -> None:
    row = CompanySettings.current()
    row.short_name, row.processor_name = "Monvera", "PayCo"
    row.save()
    admin, _ = client_for(Role.ADMIN)
    page = admin.get(SETTINGS).content.decode()
    card = page[page.index('id="company-card"') : page.index('id="bank-deposits-card"')]
    assert "Invoice e-mails sent by" in card
    assert (
        '<input type="radio" name="invoice_sender" value="processor" id="invoice-sender-processor"'
        " checked> PayCo" in card
    )
    assert (
        '<input type="radio" name="invoice_sender" value="company" id="invoice-sender-company">'
        " Monvera" in card
    )
    assert 'name="section" value="company"' in card
    for name in ("address_line1", "address_line2", "city", "state", "postal_code"):
        assert f'name="{name}"' in card, name


def test_an_admin_saves_the_address_and_the_sender_and_it_is_audited() -> None:
    admin, user = client_for(Role.ADMIN)
    response = admin.post(SAVE, company_form(), follow=True)
    assert response.status_code == 200
    assert mail_settings.COMPANY_SAVED in response.content.decode()
    row = CompanySettings.current()
    assert (row.address_line1, row.address_line2, row.city, row.state, row.postal_code) == (
        "100 Main Street",
        "Suite 200",
        "Paterson",
        "NJ",
        "07501",
    )
    assert row.invoice_sender == InvoiceSender.COMPANY
    entry = AuditLog.objects.get(actor=user, action="company_settings.updated")
    assert entry.before is not None and entry.before["invoice_sender"] == "processor"
    assert entry.after is not None and entry.after["invoice_sender"] == "company"
    assert entry.after["city"] == "Paterson"
    # The page shows what was saved, the company chosen.
    page = admin.get(SETTINGS).content.decode()
    assert 'value="company" id="invoice-sender-company" checked' in page
    assert 'value="100 Main Street"' in page


def test_saving_the_same_values_again_changes_nothing_and_audits_nothing() -> None:
    admin, user = client_for(Role.ADMIN)
    admin.post(SAVE, company_form())
    response = admin.post(SAVE, company_form(), follow=True)
    assert mail_settings.UNCHANGED in response.content.decode()
    assert AuditLog.objects.filter(actor=user, action="company_settings.updated").count() == 1


@pytest.mark.parametrize(
    ("overrides", "refusal"),
    [
        ({"invoice_sender": "both"}, mail_settings.SENDER_REFUSED),
        ({"invoice_sender": ""}, mail_settings.SENDER_REFUSED),
        ({"city": "x" * 101}, "City: at most 100 characters."),
        ({"address_line1": "4111 1111 1111 1111"}, "Address line 1 cannot hold a card"),
    ],
)
def test_a_bad_company_value_is_refused_without_echo_and_nothing_is_written(
    overrides: dict[str, str], refusal: str
) -> None:
    admin, _ = client_for(Role.ADMIN)
    response = admin.post(SAVE, company_form(**overrides), follow=True)
    page = response.content.decode()
    assert refusal in page
    assert "4111 1111 1111 1111" not in page
    row = CompanySettings.current()
    assert (row.invoice_sender, row.city) == (InvoiceSender.PROCESSOR, "")
    assert not AuditLog.objects.filter(action="company_settings.updated").exists()


@pytest.mark.parametrize("role", [Role.VIEWER, Role.BILLING])
def test_below_admin_the_company_card_cannot_be_saved(role: Role) -> None:
    response = client_for(role)[0].post(SAVE, company_form())
    assert response.status_code == 403
    assert CompanySettings.current().invoice_sender == InvoiceSender.PROCESSOR


def test_the_mail_card_and_the_company_card_keep_each_others_fields() -> None:
    admin, _ = client_for(Role.ADMIN)
    admin.post(SAVE, company_form())
    admin.post(
        SAVE,
        {
            "sender_email": "billing@example.com",
            "reply_to_email": "",
            "manager_emails": "",
            "sandbox_allowlist": "",
        },
    )
    row = CompanySettings.current()
    assert row.sender_email == "billing@example.com"
    assert (row.invoice_sender, row.city) == (InvoiceSender.COMPANY, "Paterson")


def test_the_page_path_never_reads_the_logo_copy() -> None:
    row = CompanySettings.current()
    CompanySettings.objects.filter(pk=row.pk).update(logo_png=png())
    found = CompanySettings.read()
    assert "logo_png" in found.get_deferred_fields()


# --- The logo's PNG copy ------------------------------------------------------------------------


def test_a_png_is_kept_as_it_is_and_a_jpeg_becomes_a_png() -> None:
    data = png()
    assert logo.png_copy(data) == data
    copy = logo.png_copy(jpeg())
    with Image.open(io.BytesIO(copy)) as image:
        assert (image.format, image.size) == ("PNG", (40, 20))


@pytest.mark.parametrize(
    "data", [b"not an image", b"%PDF-1.4 fake", b"GIF89a" + b"\x00" * 32, b""], ids=repr
)
def test_anything_but_a_png_or_a_jpeg_is_refused(data: bytes) -> None:
    with pytest.raises(ValueError):
        logo.png_copy(data)


def test_a_gif_is_refused_even_though_pillow_reads_it() -> None:
    out = io.BytesIO()
    Image.new("RGB", (4, 4)).save(out, format="GIF")
    with pytest.raises(ValueError, match="not a PNG or a JPEG"):
        logo.png_copy(out.getvalue())


def test_a_png_over_the_limit_is_scaled_down_until_it_fits() -> None:
    noise = random.Random(15).randbytes(1400 * 900 * 3)  # noqa: S311 (test data, not a secret)
    noisy = Image.frombytes("RGB", (1400, 900), noise)
    out = io.BytesIO()
    noisy.save(out, format="PNG")
    assert len(out.getvalue()) > LOGO_PNG_MAX_BYTES
    copy = logo.png_copy(out.getvalue())
    assert len(copy) <= LOGO_PNG_MAX_BYTES
    with Image.open(io.BytesIO(copy)) as image:
        assert image.format == "PNG" and image.width <= 1200


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch) -> MemoryDocumentStorage:
    memory = MemoryDocumentStorage()
    monkeypatch.setattr(documents_storage, "get_storage", lambda: memory)
    return memory


def stored_logo(storage: MemoryDocumentStorage, data: bytes, content_type: str) -> Document:
    store = a_store("0901 LOGO", 901)
    path = f"documents/store/{store.pk}/{uuid.uuid4()}"
    storage.put(path, data, content_type)
    return Document.objects.create(
        store=store,
        kind="other",
        original_filename="logo.png",
        content_type=content_type,
        size_bytes=len(data),
        blob_path=path,
        sha256=hashlib.sha256(data).hexdigest(),
    )


def admin_form(**overrides: str) -> dict[str, str]:
    form = {
        "name": "Monvera",
        "short_name": "Monvera",
        "support_email": "help@example.com",
        "time_zone": "America/New_York",
        "logo": "",
        "processor_name": "Stax",
    }
    return {**form, **overrides}


def admin_client() -> Any:
    user, device = make_user(f"logo-{uuid.uuid4().hex[:8]}", Role.ADMIN)
    assert device is not None
    return verified_client(user, device), user


def test_saving_the_logo_in_the_admin_writes_its_png_copy_and_removing_it_clears_it(
    storage: MemoryDocumentStorage,
) -> None:
    document = stored_logo(storage, jpeg(), "image/jpeg")
    client, user = admin_client()
    url = "/admin/ledger/companysettings/1/change/"
    assert client.post(url, admin_form(logo=document.blob_path)).status_code == 302
    copy = bytes(CompanySettings.objects.get().logo_png)
    with Image.open(io.BytesIO(copy)) as image:
        assert image.format == "PNG"
    # The read is the admin's audited download of the document.
    assert AuditLog.objects.filter(
        actor=user, action="document.downloaded", entity_id=str(document.pk)
    ).exists()
    assert client.post(url, admin_form(logo="")).status_code == 302
    assert bytes(CompanySettings.objects.get().logo_png) == b""


def test_a_logo_the_storage_cannot_serve_or_not_an_image_leaves_no_copy(
    storage: MemoryDocumentStorage,
) -> None:
    broken = stored_logo(storage, b"\x89PNG not really", "image/png")
    row = CompanySettings.current()
    row.logo = broken.blob_path
    row.save()
    _, user = admin_client()
    assert logo.refresh(row, user) is False
    assert bytes(CompanySettings.objects.get().logo_png) == b""
    gone = stored_logo(storage, png(), "image/png")
    storage.blobs.pop(gone.blob_path)
    row.logo = gone.blob_path
    row.save()
    assert logo.refresh(row, user) is False
    assert bytes(CompanySettings.objects.get().logo_png) == b""


def test_a_deleted_logo_document_leaves_no_copy(storage: MemoryDocumentStorage) -> None:
    from django.utils import timezone

    document = stored_logo(storage, png(), "image/png")
    row = CompanySettings.current()
    row.logo = document.blob_path
    row.save()
    _, user = admin_client()
    assert logo.refresh(row, user) is True
    Document.objects.filter(pk=document.pk).update(
        deleted_at=timezone.now(), delete_note="Replaced"
    )
    assert logo.refresh(row, user) is False
    assert bytes(CompanySettings.objects.get().logo_png) == b""


# --- The enums, the templates and migration 0071 ------------------------------------------------


def test_the_two_message_kinds_rules_and_entities_and_their_templates_exist() -> None:
    assert (MessageKind.INVOICE.value, MessageKind.RECEIPT.value) == ("invoice", "receipt")
    assert (MessageRule.INVOICE.value, MessageRule.RECEIPT.value) == ("invoice", "receipt")
    assert (ChargeEntityType.INVOICE.value, ChargeEntityType.PAYMENT.value) == (
        "invoice",
        "payment",
    )
    invoice = seeded(merge.INVOICE_CODE)
    assert invoice.subject == "Invoice {invoice_number} — {amount} due {due_date}"
    for name in ("{amount}", "{due_date}", "{pay_link}", "{provider_pay_link}", "{support_email}"):
        assert name in invoice.body, name
    receipt = seeded(merge.RECEIPT_CODE)
    assert receipt.subject == "Receipt for {amount}"
    for name in ("{amount}", "{paid_on}", "{applied_to}"):
        assert name in receipt.body, name
    for row in (invoice, receipt):
        assert row.active and not row.internal and row.merge_field_errors() == {}


def test_0071_guards_the_five_address_columns_beside_0061s() -> None:
    before = importlib.import_module("ledger.migrations.0061_messaging_engine")
    assert MIGRATION.GUARDED_COLUMNS == {
        "company_settings": (
            *before.GUARDED_COLUMNS["company_settings"],
            "address_line1",
            "address_line2",
            "city",
            "state",
            "postal_code",
        )
    }


def _migrate(*targets: tuple[str, str]) -> Any:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(list(targets))
    return MigrationExecutor(connection).loader.project_state(list(targets)).apps


@pytest.mark.django_db(transaction=True)
def test_0071_forward_and_back() -> None:
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        old = _migrate(BEFORE)
        assert (
            not old.get_model("ledger", "MessageTemplate")
            .objects.filter(code__in=MIGRATION.SEEDED_CODES)
            .exists()
        )
        # The row the new columns are added over: an earlier transactional test may have
        # flushed the one the migrations seeded, so it is made here when it is missing.
        old.get_model("ledger", "CompanySettings").objects.get_or_create(
            pk=COMPANY_SETTINGS_ID, defaults=SEED
        )
        new = _migrate(AFTER)
        codes = set(
            new.get_model("ledger", "MessageTemplate")
            .objects.filter(code__in=MIGRATION.SEEDED_CODES)
            .values_list("code", flat=True)
        )
        assert codes == {"invoice_sent", "receipt"}
        settings_model = new.get_model("ledger", "CompanySettings")
        row = settings_model.objects.get(pk=COMPANY_SETTINGS_ID)
        assert (row.invoice_sender, row.city, bytes(row.logo_png)) == ("processor", "", b"")
        with pytest.raises(IntegrityError), transaction.atomic():
            settings_model.objects.filter(pk=1).update(invoice_sender="nobody")
        back = _migrate(BEFORE)
        assert (
            not back.get_model("ledger", "MessageTemplate")
            .objects.filter(code__in=MIGRATION.SEEDED_CODES)
            .exists()
        )
        assert "invoice_sender" not in {
            field.name for field in back.get_model("ledger", "CompanySettings")._meta.fields
        }
    finally:
        _migrate(*leaves)


@pytest.mark.django_db(transaction=True)
def test_0071_reverse_is_refused_while_an_invoice_message_exists() -> None:
    from tests.receivables_helpers import a_lease, an_invoice, an_operator

    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    operator = an_operator("Holds A Message")
    lease = a_lease(a_store("0902", number=902), operator)
    invoice = an_invoice(operator, 9902)
    from ledger.models import OutboundMessage

    OutboundMessage.objects.create(
        operator=operator,
        lease=lease,
        template=seeded(merge.INVOICE_CODE),
        kind=MessageKind.INVOICE,
        rule=MessageRule.INVOICE,
        charge_entity_type=ChargeEntityType.INVOICE,
        charge_entity_id=invoice.pk,
        to_address="ops@example.com",
    )
    try:
        with pytest.raises(RuntimeError, match="refuses to reverse"):
            _migrate(BEFORE)
    finally:
        _migrate(*leaves)
        OutboundMessage.objects.all().delete()


def test_an_invoice_message_must_name_an_invoice_under_its_rule() -> None:
    from ledger.models import OutboundMessage
    from tests.receivables_helpers import a_lease, an_invoice, an_operator

    operator = an_operator("Shape")
    lease = a_lease(a_store("0903", number=903), operator)
    invoice = an_invoice(operator, 9903)
    template = seeded(merge.INVOICE_CODE)
    for kind, rule, entity in (
        (MessageKind.INVOICE, MessageRule.MANUAL, ChargeEntityType.INVOICE),
        (MessageKind.LADDER, MessageRule.INVOICE, ChargeEntityType.INVOICE),
        (MessageKind.INVOICE, MessageRule.INVOICE, ChargeEntityType.RENT_CHARGE),
        (MessageKind.RECEIPT, MessageRule.RECEIPT, ChargeEntityType.INVOICE),
        (MessageKind.LADDER, MessageRule.B, ChargeEntityType.PAYMENT),
    ):
        with pytest.raises(IntegrityError), transaction.atomic():
            OutboundMessage.objects.create(
                operator=operator,
                lease=lease,
                template=template,
                kind=kind,
                rule=rule,
                charge_entity_type=entity,
                charge_entity_id=invoice.pk,
                to_address="ops@example.com",
            )


def test_send_a_message_now_offers_neither_the_invoice_nor_the_receipt_template() -> None:
    from ledger.services import messaging

    for code in merge.PDF_CODES:
        seeded(code)
    offered = {row.code for row in messaging.manual_templates()}
    assert not offered & merge.PDF_CODES
    assert offered, "the other operator templates are still offered"


def test_the_invoice_preview_names_the_company_settings_name_not_the_environment_s(
    settings: Any,
) -> None:
    from ledger.models import InvoiceDraft
    from ledger.services import invoice_builder
    from tests.receivables_helpers import an_operator

    settings.COMPANY_NAME = "Environment Name Inc"
    CompanySettings.objects.filter(pk=CompanySettings.current().pk).update(name="Acme Storage Co")
    _, user = admin_client()
    draft = InvoiceDraft.objects.create(operator=an_operator("Decision Four"), created_by=user)
    assert invoice_builder.preview(draft).company == "Acme Storage Co"

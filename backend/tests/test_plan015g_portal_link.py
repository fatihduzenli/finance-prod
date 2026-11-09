"""PLAN-015g task 2: the invoice e-mail links the portal only. ``{provider_pay_link}`` is no
longer a placeholder of ``invoice_sent``, and migration 0078 takes every line naming it out of
the stored body (docs/qa/15/PLAN-015g-net-rent-line-pay-link.md §3B)."""

from __future__ import annotations

import importlib
from datetime import date
from typing import Any, cast

import pytest
from django.apps import apps
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from accounts.roles import Role
from ledger import merge
from ledger.models import AuditLog, MessageTemplate
from ledger.services import message_templates
from tests.pdf_helpers import billed_store
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

MIGRATION = importlib.import_module("ledger.migrations.0078_invoice_email_portal_link_only")
BEFORE = ("ledger", "0077_credit_return")
AFTER = ("ledger", "0078_invoice_email_portal_link_only")
SEEDED_SUBJECT, SEEDED_BODY = MIGRATION.SEEDED
PORTAL_ONLY = (
    "Hello {operator_name},\n\n"
    "Your invoice {invoice_number} for store #{store_number} {store_name} is attached: "
    "{amount}, due {due_date}.\n\n"
    "Pay by bank debit (ACH) here: {pay_link}\n"
    "Paying by wire or Zelle? Put {invoice_number} in the memo.\n\n"
    "Questions? Write to {support_email}.\n\n"
    "{company_name}"
)
NO_SCHEMA_EDITOR = cast(Any, None)  # the data functions never use it


def invoice_template(subject: str, body: str) -> MessageTemplate:
    """The ``invoice_sent`` row holding ``subject`` and ``body`` as stored before 0078 (written
    past the model's validation, which no longer knows the placeholder)."""
    template, _ = MessageTemplate.objects.get_or_create(
        code=merge.INVOICE_CODE,
        defaults={"name": "Invoice", "subject": "Invoice", "body": "Hello {operator_name}"},
    )
    MessageTemplate.objects.filter(pk=template.pk).update(subject=subject, body=body)
    return MessageTemplate.objects.get(pk=template.pk)


def forward() -> MessageTemplate:
    MIGRATION.remove_provider_pay_line(apps, NO_SCHEMA_EDITOR)
    return MessageTemplate.objects.get(code=merge.INVOICE_CODE)


def backward() -> MessageTemplate:
    MIGRATION.restore_provider_pay_line(apps, NO_SCHEMA_EDITOR)
    return MessageTemplate.objects.get(code=merge.INVOICE_CODE)


def audited() -> list[tuple[str, Any, Any]]:
    return [
        (row.action, row.before, row.after)
        for row in AuditLog.objects.filter(action__in=[MIGRATION.REMOVED, MIGRATION.KEPT])
    ]


# --- the merge fields ---------------------------------------------------------------------------


def test_the_hosted_link_is_no_placeholder_of_any_template() -> None:
    assert "provider_pay_link" not in merge.fields_for(merge.INVOICE_CODE)
    assert "provider_pay_link" not in merge.MERGE_FIELDS
    assert "pay_link" in merge.fields_for(merge.INVOICE_CODE)
    assert merge.problems("Pay: {provider_pay_link}", merge.fields_for(merge.INVOICE_CODE)) == [
        merge.UNKNOWN.format(name="provider_pay_link")
    ]


# --- 0078 forwards ------------------------------------------------------------------------------


def test_the_seeded_body_loses_the_hosted_line_and_writes_no_audit_row() -> None:
    assert MIGRATION.without_provider_pay_line(SEEDED_BODY) == PORTAL_ONLY
    invoice_template(SEEDED_SUBJECT, SEEDED_BODY)
    template = forward()
    assert (template.subject, template.body) == (SEEDED_SUBJECT, PORTAL_ONLY)
    assert template.merge_field_errors() == {}
    # The seeded case is not audited (a fresh database holds it; the lesson of 0068).
    assert audited() == []


def test_an_edited_body_naming_the_link_in_other_words_loses_that_line_audited() -> None:
    body = (
        "Hello {operator_name},\n"
        "Card or bank, your choice: {provider_pay_link} (any time)\n"
        "Pay by bank debit (ACH) here: {pay_link}\n"
        "{company_name}"
    )
    invoice_template(SEEDED_SUBJECT, body)
    template = forward()
    after = "Hello {operator_name},\nPay by bank debit (ACH) here: {pay_link}\n{company_name}"
    assert template.body == after
    assert template.merge_field_errors() == {}
    assert audited() == [
        (
            MIGRATION.REMOVED,
            {"code": "invoice_sent", "subject": SEEDED_SUBJECT, "body": body},
            {"code": "invoice_sent", "subject": SEEDED_SUBJECT, "body": after},
        )
    ]


def test_a_body_not_naming_the_link_is_left_alone_with_no_audit_row() -> None:
    body = "Hello {operator_name},\nPay here: {pay_link}\nA literal {{provider_pay_link}} stays."
    invoice_template(SEEDED_SUBJECT, body)
    template = forward()
    assert (template.subject, template.body) == (SEEDED_SUBJECT, body)
    assert audited() == []


def test_a_subject_naming_the_link_loses_the_placeholder_text() -> None:
    invoice_template("Invoice {invoice_number} — pay at {provider_pay_link} today", PORTAL_ONLY)
    template = forward()
    assert template.subject == "Invoice {invoice_number} — pay at today"
    assert template.body == PORTAL_ONLY
    [(action, before, after)] = audited()
    assert action == MIGRATION.REMOVED
    assert before["subject"].endswith("{provider_pay_link} today")
    assert after["subject"] == template.subject


def test_a_body_the_removal_would_leave_empty_is_kept_and_says_so() -> None:
    body = "Pay: {provider_pay_link}\n\nOr here: {provider_pay_link}"
    invoice_template(SEEDED_SUBJECT, body)
    template = forward()
    assert template.body == body  # untouched: an empty body would break the not-blank check
    assert audited() == [
        (MIGRATION.KEPT, None, {"code": "invoice_sent", "reason": "would_be_empty"})
    ]
    # Such a body fails validation until an admin edits it.
    assert template.merge_field_errors()


def test_no_invoice_template_is_nothing_to_do() -> None:
    MessageTemplate.objects.filter(code=merge.INVOICE_CODE).delete()
    MIGRATION.remove_provider_pay_line(apps, NO_SCHEMA_EDITOR)
    MIGRATION.restore_provider_pay_line(apps, NO_SCHEMA_EDITOR)
    assert audited() == []


# --- 0078 backwards -----------------------------------------------------------------------------


def test_the_reverse_puts_the_seeded_line_back_after_the_portal_line() -> None:
    assert MIGRATION.with_provider_pay_line(PORTAL_ONLY) == SEEDED_BODY
    invoice_template(SEEDED_SUBJECT, PORTAL_ONLY)
    assert backward().body == SEEDED_BODY


def test_the_reverse_leaves_a_body_without_the_portal_line_or_still_naming_the_link() -> None:
    for body in ("Hello {operator_name},\nPay: {pay_link}", SEEDED_BODY):
        invoice_template(SEEDED_SUBJECT, body)
        assert backward().body == body


@pytest.mark.django_db(transaction=True)
def test_0078_forward_and_back() -> None:
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        old = migrate_to(BEFORE)
        template = old.get_model("ledger", "MessageTemplate")
        # An earlier transactional test may have flushed the seeded row: it is made here then.
        template.objects.get_or_create(
            code=merge.INVOICE_CODE,
            defaults={"name": "Invoice", "subject": SEEDED_SUBJECT, "body": SEEDED_BODY},
        )
        template.objects.filter(code=merge.INVOICE_CODE).update(
            subject=SEEDED_SUBJECT, body=SEEDED_BODY
        )
        new = migrate_to(AFTER)
        row = new.get_model("ledger", "MessageTemplate").objects.get(code=merge.INVOICE_CODE)
        assert row.body == PORTAL_ONLY
        back = migrate_to(BEFORE)
        row = back.get_model("ledger", "MessageTemplate").objects.get(code=merge.INVOICE_CODE)
        assert row.body == SEEDED_BODY
    finally:
        migrate_to(*leaves)


def migrate_to(*targets: tuple[str, str]) -> Any:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(list(targets))
    return MigrationExecutor(connection).loader.project_state(list(targets)).apps


# --- the editor ---------------------------------------------------------------------------------


def test_the_editor_refuses_a_hand_posted_body_naming_the_link_and_lists_it_nowhere() -> None:
    template = invoice_template(SEEDED_SUBJECT, PORTAL_ONLY)
    client, _ = client_for(Role.ADMIN)
    url = f"/admin-settings/templates/{template.pk}/"
    page = client.get(url).content.decode()
    assert "{pay_link}" in page and "provider_pay_link" not in page
    response = client.post(
        url, {"subject": SEEDED_SUBJECT, "body": PORTAL_ONLY + "\nOr: {provider_pay_link}"}
    )
    assert response.status_code == 400
    assert "{provider_pay_link} is not a placeholder this template can use." in (
        response.content.decode()
    )
    template.refresh_from_db()
    assert template.body == PORTAL_ONLY
    assert not AuditLog.objects.filter(action="message_template.changed").exists()


def test_the_preview_renders_the_portal_link_alone() -> None:
    lease = billed_store()
    template = invoice_template(SEEDED_SUBJECT, PORTAL_ONLY)
    found = message_templates.preview(
        template, lease.store_id, template.subject, template.body, date(2026, 11, 7)
    )
    assert found.ok, found.message
    assert "{" not in found.body and "pay.example.com" not in found.body
    assert "Pay by bank debit (ACH) here: " in found.body
    assert "Or pay on our payment page" not in found.body


def test_the_settings_page_does_not_name_the_hosted_link() -> None:
    page = client_for(Role.ADMIN)[0].get("/admin-settings/").content.decode()
    assert "provider_pay_link" not in page

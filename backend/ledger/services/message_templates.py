"""Admin › Settings › Message templates (docs/qa/PLAN-014-per-lease-billing-messaging-portal-
deposits.md D4): the list, the edit of a template's subject and body, and its preview.

:func:`save` (admin) refuses, with a sentence and nothing written, a subject that is empty,
longer than :data:`~ledger.models.TEMPLATE_SUBJECT_MAX_LENGTH` or more than one line, a body
that is empty or longer than :data:`BODY_MAX_LENGTH`, a text holding a card or account
number, and a placeholder the template's code may not use (:func:`ledger.merge.problems`:
the placeholder is named, no other part of the text is repeated). A change is audited
``message_template.changed`` with the subject and body before and after.

:func:`preview` renders a subject and body (the ones typed, saved or not) with the real
values of a store's current lease and sample amounts (:func:`sample_context`): nothing is
saved and nothing is queued.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F

from finance.dates import day_text, month_long_text
from ledger import merge
from ledger.models import (
    TEMPLATE_SUBJECT_MAX_LENGTH,
    AuditLog,
    CompanySettings,
    Lease,
    MessageTemplate,
    Store,
)
from ledger.services import business_days, messaging, statement_links
from ledger.validators import validate_no_payment_data
from payments import lease_sync

ENTITY = "message_template"
#: Longest body (D4's adversarial case: a 20 000 character body is refused).
BODY_MAX_LENGTH = 10_000
SUBJECT_REQUIRED = "Write a subject."
SUBJECT_TOO_LONG = f"The subject is at most {TEMPLATE_SUBJECT_MAX_LENGTH} characters."
SUBJECT_ONE_LINE = "The subject is one line."
BODY_REQUIRED = "Write the message."
BODY_TOO_LONG = f"The message is at most {BODY_MAX_LENGTH:,} characters."
PAYMENT_DATA = "A template cannot hold a card or account number."
SAVED = "Template saved."
UNCHANGED = "Nothing changed."
NOT_FOUND = "That template is not in the list."
NO_STORE = "Choose a store with a current lease to preview with."

#: Sample values of the preview (D4).
SAMPLE_AMOUNT = "$1,234.56"
SAMPLE_DAYS_LATE = "12"
SAMPLE_RETURN_REASON = "Insufficient funds"
SAMPLE_REFUND = "$0.00"


@dataclass(frozen=True, slots=True)
class Outcome:
    ok: bool
    message: str


@dataclass(frozen=True, slots=True)
class Preview:
    ok: bool
    #: The refusal when not ``ok``.
    message: str = ""
    subject: str = ""
    body: str = ""


def rows() -> list[MessageTemplate]:
    """Every template: the active first, then the operators' before the internal, then by
    name (case-folded) and code. Sorted here, not in SQL, so the order does not follow the
    database's collation (PostgreSQL and sqlite order "F —" and "Final" differently)."""
    return sorted(
        MessageTemplate.objects.all(),
        key=lambda t: (not t.active, t.internal, t.display_name.casefold(), t.code),
    )


def get(pk: uuid.UUID) -> MessageTemplate | None:
    return MessageTemplate.objects.filter(pk=pk).first()


def placeholders(template: MessageTemplate) -> str:
    """ "{operator_name}, {store_number}, …": the placeholders the template may use."""
    return merge.fields_text(merge.fields_for(template.code))


def _text(raw: object) -> str:
    return raw.replace("\r\n", "\n") if isinstance(raw, str) else ""


def check(template: MessageTemplate, subject: str, body: str) -> str:
    """The refusal of ``subject`` and ``body`` for ``template``, or ``""``."""
    if not subject.strip():
        return SUBJECT_REQUIRED
    if "\n" in subject or "\r" in subject:
        return SUBJECT_ONE_LINE
    if len(subject) > TEMPLATE_SUBJECT_MAX_LENGTH:
        return SUBJECT_TOO_LONG
    if not body.strip():
        return BODY_REQUIRED
    if len(body) > BODY_MAX_LENGTH:
        return BODY_TOO_LONG
    for text in (subject, body):
        try:
            validate_no_payment_data(text)
        except ValidationError:
            return PAYMENT_DATA
    fields = merge.fields_for(template.code)
    for text in (subject, body):
        found = merge.problems(text, fields)
        if found:
            return found[0]
    return ""


def save(pk: uuid.UUID, subject_raw: object, body_raw: object, actor: User) -> Outcome:
    """Write a template's subject and body (see the module docstring)."""
    subject, body = _text(subject_raw).strip(), _text(body_raw)
    with transaction.atomic():
        template = MessageTemplate.objects.select_for_update().filter(pk=pk).first()
        if template is None:
            return Outcome(False, NOT_FOUND)
        refusal = check(template, subject, body)
        if refusal:
            return Outcome(False, refusal)
        before = {"subject": template.subject, "body": template.body}
        after = {"subject": subject, "body": body}
        if before == after:
            return Outcome(True, UNCHANGED)
        template.subject, template.body = subject, body
        template.save(update_fields=["subject", "body", "updated_at"])
        AuditLog.objects.create(
            actor=actor,
            action=f"{ENTITY}.changed",
            entity_type=ENTITY,
            entity_id=str(template.pk),
            before={"code": template.code, **before},
            after={"code": template.code, **after},
        )
    return Outcome(True, SAVED)


# --- Preview --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoreChoice:
    store: Store
    label: str


def preview_stores() -> list[StoreChoice]:
    """The stores a preview may use: those with a current lease that has an operator."""
    found = (
        Lease.objects.filter(ended_on__isnull=True, operator__isnull=False)
        .select_related("store")
        .order_by(F("store__store_number").asc(nulls_last=True), "store__external_label", "id")
    )
    seen: dict[uuid.UUID, StoreChoice] = {}
    for lease in found:
        store = lease.store
        if store.pk not in seen:
            number = f"#{store.store_number} " if store.store_number is not None else ""
            seen[store.pk] = StoreChoice(store, f"{number}{store.external_label}")
    return list(seen.values())


def current_lease(store_id: uuid.UUID) -> Lease | None:
    return (
        Lease.objects.filter(store_id=store_id, ended_on__isnull=True, operator__isnull=False)
        .select_related("store", "operator", "billing")
        .order_by(F("started_on").desc(nulls_last=True), "id")
        .first()
    )


def business_days_after(day: date, count: int) -> date:
    """``count`` business days after ``day`` (the preview's sample retry date; Federal
    Reserve holidays skipped, :func:`ledger.services.business_days.add`)."""
    return business_days.add(day, count)


def sample_context(template: MessageTemplate, lease: Lease, today: date) -> dict[str, str]:
    """The placeholder values of the preview: the lease's real names, links and last four,
    and sample amounts and dates (D4)."""
    company = CompanySettings.read()
    operator = lease.operator
    if template.code == merge.FINAL_STATEMENT_CODE:
        return {
            "operator": str(operator.display_name) if operator is not None else "",
            "store": str(lease.store.external_label),
            "company": str(company.name),
            "refund_due": SAMPLE_AMOUNT,
            "amount_owed": SAMPLE_REFUND,
            "end_date": day_text(today),
        }
    store = lease.store
    due = today + timedelta(days=3)
    return {
        "operator_name": str(operator.display_name) if operator is not None else "",
        "store_number": str(store.store_number) if store.store_number is not None else "",
        "store_name": str(store.external_label),
        "amount": SAMPLE_AMOUNT,
        "charge_name": f"Rent {month_long_text(today)}",
        "due_date": day_text(due),
        "debit_date": day_text(due),
        "retry_date": day_text(business_days_after(today, 3)),
        "days_late": SAMPLE_DAYS_LATE,
        "bank_last4": lease_sync.billing_of(lease).bank_last4 or messaging.NO_VALUE,
        "return_reason": SAMPLE_RETURN_REASON,
        "pay_link": statement_links.link(operator) if operator is not None else "",
        "store_link": messaging.store_link(lease),
        "support_email": str(company.support_email or ""),
        "company_name": str(company.name),
    }


def preview(
    template: MessageTemplate,
    store_id: uuid.UUID | None,
    subject_raw: object,
    body_raw: object,
    today: date,
) -> Preview:
    """``subject`` and ``body`` rendered for the store's current lease (see the module
    docstring); a refusal when the text would be refused on save or the store has no current
    lease."""
    subject, body = _text(subject_raw).strip(), _text(body_raw)
    refusal = check(template, subject, body)
    if refusal:
        return Preview(False, refusal)
    lease = current_lease(store_id) if store_id is not None else None
    if lease is None:
        return Preview(False, NO_STORE)
    values = sample_context(template, lease, today)
    try:
        return Preview(
            True,
            subject=messaging.one_line(merge.render(subject, values)),
            body=merge.render(body, values),
        )
    except merge.TemplateRefused:
        return Preview(False, merge.NOT_ALLOWED)

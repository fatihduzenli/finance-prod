"""Operators and their payment-provider customer (docs/qa/PLAN-006-operators.md §4.2).

The only module through which the operator screens reach the provider. Views call the
functions below and pass the provider they obtained from :func:`payments.get_provider`;
nothing else in the operator flow calls a provider method.

Order of work, for every operator with an e-mail (D3, D6, Rule 5):

1. The local row is written first, in its own transaction, with ``sync_status =
   pending`` (``create_operator`` / ``update_operator``). A provider failure can
   therefore never lose staff input, and a local failure never leaves an orphan at the
   provider.
2. :func:`sync_operator` locks the row (``select_for_update``) and calls the provider
   only while the row is ``pending`` and has no current customer reference. It looks
   the e-mail up first and compares each returned record here, lower-cased and
   stripped, for equality: a match is linked, several matches link the oldest and flag
   the operator for review, no match creates one customer. Two submissions of the same
   form therefore produce one provider customer.
3. A :class:`~payments.errors.ProviderError` marks the row ``failed`` with the error's
   class name and the time, and is not retried: :func:`sync_operator` with
   ``retry=True`` is the manual retry. A :class:`~payments.errors.ProviderRejected`
   also stores the key names the provider objected to (``sync_failure_fields``, never
   a value). A retry is refused while the last attempt is less than
   :data:`RETRY_LOCK_SECONDS` old; the check is made under the row lock (PLAN-006a).

Operators without an e-mail make no call (D4): they wait for :func:`link_operator`,
which reads the customer by ID to prove it exists and stores the customer and
payment-method IDs.

Every change writes one ``audit_log`` row. Nothing from a provider response is stored
except the customer ID; the trimmed summary is not persisted. Failure text is a class
name or a fixed sentence from :data:`MESSAGES`, never a provider message.
"""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.utils import timezone

from ledger.models import AuditLog, EnrolmentStatus, Operator, SyncFailure, SyncStatus
from payments.errors import ProviderError, ProviderRejected
from payments.models import ExternalRef
from payments.provider import (
    CustomerInput,
    CustomerQuery,
    CustomerRecord,
    PaymentProvider,
    mask_identifier,
)
from payments.stax.validation import normalise_phone as normalise_phone

#: ``external_refs.provider`` for this app's payment provider (as in ``seed_demo``).
PROVIDER_KEY = "stax_pay"
ENTITY_OPERATOR = "operator"
KIND_CUSTOMER = "customer"
KIND_PAYMENT_METHOD = "payment_method"

#: A manual retry waits this long after the last attempt (PLAN-006a §3.3).
RETRY_LOCK_SECONDS = 60

#: Identifier syntax accepted from staff, as the adapter's own validation (PLAN-005).
IDENTIFIER = re.compile(r"\A[A-Za-z0-9_-]{1,255}\Z")

#: The operator fields staff edit, in form order.
OPERATOR_FIELDS: tuple[str, ...] = (
    "display_name",
    "legal_name",
    "company",
    "email",
    "phone",
    "address_line1",
    "address_line2",
    "city",
    "state",
    "postal_code",
    "notes",
)

#: Local refusals of the link form (no provider call was made).
ALREADY_LINKED = "AlreadyLinked"
ARCHIVED = "Archived"
#: Local refusal of a sync: the stored phone fails the rule (PLAN-006b §2.4).
PHONE_INVALID = "PhoneInvalid"

#: Fixed sentences shown to staff, keyed by failure class name or local refusal.
MESSAGES: dict[str, str] = {
    SyncFailure.PROVIDER_REJECTED: (
        "The payment provider refused the request. Check the operator's details, then retry."
    ),
    SyncFailure.PROVIDER_UNAVAILABLE: (
        "The payment provider is not answering. The operator is saved; retry later."
    ),
    SyncFailure.TRANSPORT_ERROR: (
        "The payment provider could not be reached. The operator is saved; retry later."
    ),
    SyncFailure.CIRCUIT_OPEN: (
        "Calls to the payment provider are paused after repeated failures. Retry in a few minutes."
    ),
    SyncFailure.RATE_LIMIT_WAIT_EXCEEDED: "The payment provider is busy. Retry in a minute.",
    SyncFailure.VALIDATION_FAILED: (
        "The details were refused before anything was sent. Check the e-mail address."
    ),
    SyncFailure.NOT_PERMITTED_HERE: "This server is not allowed to call the payment provider.",
    SyncFailure.ALREADY_LINKED_ELSEWHERE: (
        "That payment provider record is already linked to another operator. Nothing was changed."
    ),
    ALREADY_LINKED: (
        "This operator is already linked to a different payment provider customer. "
        "Nothing was changed."
    ),
    ARCHIVED: "This operator is archived.",
    PHONE_INVALID: "Fix the phone number first.",
}
DEFAULT_MESSAGE = "The payment provider call failed. The operator is saved."
AWAITING_LINK_MESSAGE = "No e-mail address: link the payment provider customer by hand."
LINK_REJECTED_MESSAGE = (
    "The payment provider did not recognise that customer ID. Nothing was saved."
)


class Action(StrEnum):
    """What a sync or link call did."""

    NO_CALL = "no_call"
    AWAITING_LINK = "awaiting_link"
    LINKED_EXISTING = "linked_existing"
    CREATED = "created"
    FAILED = "failed"
    LINK_SET = "link_set"
    LINK_REFUSED = "link_refused"
    RETRY_TOO_SOON = "retry_too_soon"


@dataclass(frozen=True, slots=True)
class SyncOutcome:
    action: Action
    #: The operator's ``sync_status`` afterwards.
    status: str
    #: Error class name or local refusal code; empty on success.
    failure: str = ""
    needs_review: bool = False
    #: Seconds until a retry is allowed (``RETRY_TOO_SOON`` only).
    retry_in: int = 0

    @property
    def message(self) -> str:
        """A fixed sentence for staff; never text from the provider."""
        if self.action is Action.RETRY_TOO_SOON:
            return retry_wait_message(self.retry_in)
        if self.action is Action.LINKED_EXISTING:
            if self.needs_review:
                return (
                    "Linked to the oldest of several payment provider customers with this "
                    "e-mail. The operator is flagged for review."
                )
            return "Linked to the existing payment provider customer with this e-mail."
        if self.action is Action.CREATED:
            return "Created the customer at the payment provider."
        if self.action is Action.AWAITING_LINK:
            return AWAITING_LINK_MESSAGE
        if self.action is Action.LINK_SET:
            return "Linked. The operator is enrolled."
        if self.action is Action.LINK_REFUSED and self.failure == SyncFailure.PROVIDER_REJECTED:
            return LINK_REJECTED_MESSAGE
        if self.action in (Action.FAILED, Action.LINK_REFUSED):
            return MESSAGES.get(self.failure, DEFAULT_MESSAGE)
        return ""


class OperatorArchived(Exception):  # noqa: N818 (reads as a state, like the provider errors)
    """An archived operator may not be edited, synced or linked."""


# --- Local writes -------------------------------------------------------------------


def create_operator(
    fields: Mapping[str, Any], *, creation_key: uuid.UUID, actor: User | None
) -> tuple[Operator, bool]:
    """Save a new operator, or return the one this ``creation_key`` already made.

    Returns ``(operator, created)``. A replayed key never changes the existing row,
    whatever fields came with it (D6). Any other integrity error is raised.
    """
    existing = Operator.objects.filter(creation_key=creation_key).first()
    if existing is not None:
        return existing, False
    operator = Operator(creation_key=creation_key, **_editable(fields))
    operator.sync_status = SyncStatus.PENDING if operator.email else SyncStatus.AWAITING_LINK
    try:
        with transaction.atomic():
            operator.save()
            _audit(actor, "operator.created", operator, None, _snapshot(operator))
    except IntegrityError:
        existing = Operator.objects.filter(creation_key=creation_key).first()
        if existing is None:
            raise
        return existing, False
    return operator, True


def update_operator(
    operator_id: uuid.UUID, fields: Mapping[str, Any], *, actor: User | None
) -> Operator:
    """Apply staff edits under a row lock; mark the row for a sync when it needs one.

    A ``failed`` row stays failed (only the retry button calls again), unless its
    e-mail was removed, which makes it wait for a manual link.
    """
    with transaction.atomic():
        operator = _locked(operator_id)
        before = _snapshot(operator)
        for name, value in _editable(fields).items():
            setattr(operator, name, value)
        if _current_ref(operator, KIND_CUSTOMER) is not None:
            operator.sync_status = SyncStatus.LINKED
        elif not operator.email:
            operator.sync_status = SyncStatus.AWAITING_LINK
            operator.sync_failure = ""
            operator.sync_failure_fields = ""
        elif operator.sync_status != SyncStatus.FAILED:
            operator.sync_status = SyncStatus.PENDING
        operator.save()
        after = _snapshot(operator)
        if after != before:
            _audit(actor, "operator.updated", operator, before, after)
    return operator


def archive_operator(operator_id: uuid.UUID, *, actor: User | None) -> Operator:
    """Soft delete (D9). Archiving an archived operator changes nothing."""
    with transaction.atomic():
        operator = Operator.objects.select_for_update().get(pk=operator_id)
        if operator.archived_at is None:
            before = _snapshot(operator)
            operator.archived_at = timezone.now()
            operator.save(update_fields=["archived_at", "updated_at"])
            _audit(actor, "operator.archived", operator, before, _snapshot(operator))
    return operator


# --- Provider calls -----------------------------------------------------------------


def sync_operator(
    operator_id: uuid.UUID,
    *,
    provider: PaymentProvider,
    actor: User | None,
    retry: bool = False,
) -> SyncOutcome:
    """Link or create the operator's provider customer, at most one call path per row.

    Calls the provider only while the locked row is ``pending`` (or ``failed`` with
    ``retry=True``), not archived, has an e-mail and has no current customer reference.
    With ``retry=True`` it refuses (``RETRY_TOO_SOON``, no call) while the last attempt
    is less than :data:`RETRY_LOCK_SECONDS` old; the check runs under the row lock, so
    two concurrent retries cannot both pass it. A stored phone that fails the provider's
    rule refuses the sync (``PHONE_INVALID``) before any call and writes nothing.
    """
    with transaction.atomic():
        operator = Operator.objects.select_for_update().get(pk=operator_id)
        status = operator.sync_status
        if retry and status == SyncStatus.FAILED:
            status = SyncStatus.PENDING
        if (
            operator.archived_at is not None
            or status != SyncStatus.PENDING
            or _current_ref(operator, KIND_CUSTOMER) is not None
        ):
            return SyncOutcome(Action.NO_CALL, operator.sync_status)
        if not operator.email:
            operator.sync_status = SyncStatus.AWAITING_LINK
            operator.save(update_fields=["sync_status", "updated_at"])
            return SyncOutcome(Action.AWAITING_LINK, operator.sync_status)
        wait = retry_wait_seconds(operator) if retry else 0
        if wait:
            return SyncOutcome(Action.RETRY_TOO_SOON, operator.sync_status, retry_in=wait)
        if not phone_is_valid(operator):
            # Nothing is written and nothing leaves: the row waits for a fixed phone.
            return SyncOutcome(Action.FAILED, operator.sync_status, PHONE_INVALID)

        before = _snapshot(operator)
        operator.sync_attempted_at = timezone.now()
        try:
            matches = [
                record
                for record in provider.find_customers(CustomerQuery(operator_email=operator.email))
                if normalise_email(record.operator_email) == operator.email
            ]
            free = [
                record
                for record in matches
                if not _linked_elsewhere(record.external_id, KIND_CUSTOMER, operator.pk)
            ]
            needs_review = len(matches) > 1 or len(free) != len(matches)
            if matches and not free:
                return _fail(
                    operator,
                    before,
                    actor,
                    SyncFailure.ALREADY_LINKED_ELSEWHERE,
                    needs_review=needs_review,
                )
            if free:
                chosen, action = oldest(free), Action.LINKED_EXISTING
            else:
                chosen, action = provider.create_customer(customer_input(operator)), Action.CREATED
        except ProviderError as error:
            return _fail(
                operator,
                before,
                actor,
                failure_name(error),
                needs_review=False,
                fields=failure_fields(error),
            )

        _add_ref(operator, KIND_CUSTOMER, chosen.external_id)
        operator.sync_status = SyncStatus.LINKED
        operator.sync_failure = ""
        operator.sync_failure_fields = ""
        operator.needs_review = operator.needs_review or needs_review
        operator.save()
        after = {**_snapshot(operator), "customer": mask_identifier(chosen.external_id)}
        if action is Action.LINKED_EXISTING:
            after["matches"] = len(matches)
        audit_action = (
            "operator.sync_linked" if action is Action.LINKED_EXISTING else "operator.sync_created"
        )
        _audit(actor, audit_action, operator, before, after)
        return SyncOutcome(action, operator.sync_status, needs_review=needs_review)


def link_operator(
    operator_id: uuid.UUID,
    *,
    customer_external_id: str,
    payment_method_external_id: str,
    provider: PaymentProvider,
    actor: User | None,
) -> SyncOutcome:
    """Store the customer and payment-method IDs staff copied from the provider (D4).

    The customer is read by ID first (one ``get_customer`` call); if the provider does
    not return it, nothing is stored. The payment method cannot be read through the
    interface and is stored as given. Enrolment is set on success.
    """
    for value in (customer_external_id, payment_method_external_id):
        if not IDENTIFIER.match(value):
            return SyncOutcome(Action.LINK_REFUSED, "", SyncFailure.VALIDATION_FAILED)
    with transaction.atomic():
        operator = Operator.objects.select_for_update().get(pk=operator_id)
        if operator.archived_at is not None:
            return SyncOutcome(Action.LINK_REFUSED, operator.sync_status, ARCHIVED)
        current = _current_ref(operator, KIND_CUSTOMER)
        if current is not None and current.external_id != customer_external_id:
            return SyncOutcome(Action.LINK_REFUSED, operator.sync_status, ALREADY_LINKED)
        if _linked_elsewhere(customer_external_id, KIND_CUSTOMER, operator.pk) or (
            _linked_elsewhere(payment_method_external_id, KIND_PAYMENT_METHOD, operator.pk)
        ):
            return SyncOutcome(
                Action.LINK_REFUSED, operator.sync_status, SyncFailure.ALREADY_LINKED_ELSEWHERE
            )
        try:
            provider.get_customer(customer_external_id)
        except ProviderError as error:
            return SyncOutcome(Action.LINK_REFUSED, operator.sync_status, failure_name(error))

        before = _snapshot(operator)
        if current is None:
            _add_ref(operator, KIND_CUSTOMER, customer_external_id)
        method = _current_ref(operator, KIND_PAYMENT_METHOD)
        if method is not None and method.external_id != payment_method_external_id:
            method.superseded_at = timezone.now()
            method.save(update_fields=["superseded_at"])
            method = None
        if method is None:
            _add_ref(operator, KIND_PAYMENT_METHOD, payment_method_external_id)
        operator.sync_status = SyncStatus.LINKED
        operator.sync_failure = ""
        operator.sync_failure_fields = ""
        operator.enrolment_status = EnrolmentStatus.ENROLLED
        operator.save()
        after = {
            **_snapshot(operator),
            "customer": mask_identifier(customer_external_id),
            "payment_method": mask_identifier(payment_method_external_id),
        }
        _audit(actor, "operator.link_set", operator, before, after)
        return SyncOutcome(Action.LINK_SET, operator.sync_status)


# --- Helpers ------------------------------------------------------------------------


def normalise_email(value: str) -> str:
    """Lower-cased and stripped, as ``Operator.save`` stores it."""
    return value.strip().lower()


def oldest(records: Sequence[CustomerRecord]) -> CustomerRecord:
    """The record with the earliest ``created_at`` in its summary; ties keep list order.

    TODO-VERIFY-032: the customer object's ``created_at`` key and its format
    (``YYYY-MM-DD hh:mm:ss``, which sorts as text) are assumed. A record without it
    sorts after every record that has it.
    """

    def key(indexed: tuple[int, CustomerRecord]) -> tuple[bool, str, int]:
        index, record = indexed
        created = record.raw_summary.get("created_at")
        text = created if isinstance(created, str) and created else ""
        return (not text, text, index)

    return min(enumerate(records), key=key)[1]


def customer_input(operator: Operator) -> CustomerInput:
    """D5: display name as first name, legal name (or display name) as company."""
    return CustomerInput(
        first_name=operator.display_name,
        last_name="",
        company_name=operator.legal_name or operator.display_name,
        operator_email=operator.email or "",
        # The normalised digits; a stored phone that fails the rule is passed as it is,
        # so the adapter refuses it before any request.
        phone=normalise_phone(operator.phone) or operator.phone,
        reference=str(operator.pk),
    )


def phone_is_valid(operator: Operator) -> bool:
    """Whether the stored phone is empty or passes the provider's phone rule."""
    return normalise_phone(operator.phone) is not None


def retry_wait_seconds(operator: Operator, now: datetime | None = None) -> int:
    """Whole seconds until a manual retry is allowed; 0 when it is allowed now."""
    if operator.sync_attempted_at is None:
        return 0
    elapsed = ((now or timezone.now()) - operator.sync_attempted_at).total_seconds()
    if elapsed >= RETRY_LOCK_SECONDS:
        return 0
    # An attempt time in the future (clock skew) never locks longer than the lock itself.
    return min(RETRY_LOCK_SECONDS, max(1, math.ceil(RETRY_LOCK_SECONDS - elapsed)))


def retry_wait_message(seconds: int) -> str:
    """The sentence shown while a retry is locked."""
    return f"Retry available in {seconds} seconds."


def failure_fields(error: ProviderError) -> str:
    """The rejected key names to store, comma-separated; empty for other failures."""
    if isinstance(error, ProviderRejected):
        return ",".join(error.error_fields)
    return ""


def failure_name(error: ProviderError) -> str:
    """The nearest error class name the ``sync_failure`` column accepts."""
    for klass in type(error).__mro__:
        if klass.__name__ in SyncFailure.values:
            return klass.__name__
    return SyncFailure.PROVIDER_ERROR


def current_refs(operator: Operator) -> dict[str, str]:
    """The operator's current external IDs by kind (unmasked; callers mask)."""
    return dict(
        ExternalRef.objects.filter(
            entity_type=ENTITY_OPERATOR,
            entity_id=operator.pk,
            provider=PROVIDER_KEY,
            superseded_at__isnull=True,
        ).values_list("external_kind", "external_id")
    )


def _locked(operator_id: uuid.UUID) -> Operator:
    operator = Operator.objects.select_for_update().get(pk=operator_id)
    if operator.archived_at is not None:
        raise OperatorArchived
    return operator


def _editable(fields: Mapping[str, Any]) -> dict[str, Any]:
    return {name: fields[name] for name in OPERATOR_FIELDS if name in fields}


def _fail(
    operator: Operator,
    before: dict[str, Any],
    actor: User | None,
    failure: str,
    *,
    needs_review: bool,
    fields: str = "",
) -> SyncOutcome:
    operator.sync_status = SyncStatus.FAILED
    operator.sync_failure = failure
    operator.sync_failure_fields = fields
    operator.needs_review = operator.needs_review or needs_review
    operator.save()
    _audit(actor, "operator.sync_failed", operator, before, _snapshot(operator))
    return SyncOutcome(Action.FAILED, operator.sync_status, failure, needs_review)


def _current_ref(operator: Operator, kind: str) -> ExternalRef | None:
    return ExternalRef.objects.filter(
        entity_type=ENTITY_OPERATOR,
        entity_id=operator.pk,
        provider=PROVIDER_KEY,
        external_kind=kind,
        superseded_at__isnull=True,
    ).first()


def _linked_elsewhere(external_id: str, kind: str, operator_id: uuid.UUID) -> bool:
    return (
        ExternalRef.objects.filter(
            provider=PROVIDER_KEY, external_kind=kind, external_id=external_id
        )
        .exclude(entity_type=ENTITY_OPERATOR, entity_id=operator_id)
        .exists()
    )


def _add_ref(operator: Operator, kind: str, external_id: str) -> None:
    ExternalRef.objects.create(
        entity_type=ENTITY_OPERATOR,
        entity_id=operator.pk,
        provider=PROVIDER_KEY,
        external_kind=kind,
        external_id=external_id,
    )


def _snapshot(operator: Operator) -> dict[str, Any]:
    values: dict[str, Any] = {name: getattr(operator, name) for name in OPERATOR_FIELDS}
    values["sync_status"] = operator.sync_status
    values["sync_failure"] = operator.sync_failure
    values["sync_failure_fields"] = operator.sync_failure_fields
    values["enrolment_status"] = operator.enrolment_status
    values["needs_review"] = operator.needs_review
    values["archived_at"] = operator.archived_at.isoformat() if operator.archived_at else None
    return values


def _audit(
    actor: User | None,
    action: str,
    operator: Operator,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> None:
    AuditLog.objects.create(
        actor=actor,
        action=action,
        entity_type=ENTITY_OPERATOR,
        entity_id=str(operator.pk),
        before=before,
        after=after,
    )

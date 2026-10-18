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

An edit of an operator that already has a customer reference (PLAN-006c) and changes
what :func:`customer_input` builds marks the row ``pending`` (the local save commits
first and stands); :func:`sync_operator` then sends that same payload with
``update_customer`` under the row lock. The retry of a ``failed`` row with a customer
reference updates too; it never looks up or creates. An edit that leaves the payload
as it was makes no call and keeps the status.

Every change writes one ``audit_log`` row. Nothing from a provider response is stored
except the customer ID; the trimmed summary is not persisted. Failure text is a class
name or a fixed sentence from :data:`MESSAGES`, never a provider message.
"""

from __future__ import annotations

import dataclasses
import math
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.utils import timezone

from ledger.models import AuditLog, EnrolmentStatus, Operator, SyncFailure, SyncStatus
from ledger.wording import provider_sentence
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
    # PLAN-012a D3: local only, never in the provider payload (PAYLOAD_FIELDS).
    "sms_phone",
    "sms_consent_at",
    "email_consent_at",
)

#: The operator fields :func:`customer_input` reads, named in the update audit row.
PAYLOAD_FIELDS: tuple[str, ...] = ("display_name", "legal_name", "email", "phone")

#: Audit actions after which the provider holds the operator's current details.
PUSHED_ACTIONS: tuple[str, ...] = (
    "operator.sync_created",
    "operator.sync_linked",
    "operator.sync_updated",
    "operator.link_set",
)

#: Local refusals of the link form (no provider call was made).
ALREADY_LINKED = "AlreadyLinked"
ARCHIVED = "Archived"
#: Local refusal of a sync: the stored phone fails the rule (PLAN-006b §2.4).
PHONE_INVALID = "PhoneInvalid"

#: Fixed sentences shown to staff, keyed by failure class name or local refusal.
#: The sentences name the processor by the company settings row's display name (PLAN-012c
#: D9, copy list §4.1).
MESSAGES: dict[str, str] = {
    SyncFailure.PROVIDER_REJECTED: provider_sentence(
        "{provider} refused the request. Check the operator's details, then try again."
    ),
    SyncFailure.PROVIDER_UNAVAILABLE: provider_sentence(
        "{provider} is not answering. The operator is saved; try again later."
    ),
    SyncFailure.TRANSPORT_ERROR: provider_sentence(
        "{provider} could not be reached. The operator is saved; try again later."
    ),
    SyncFailure.CIRCUIT_OPEN: provider_sentence(
        "Calls to {provider} are paused after repeated errors. Try again in a few minutes."
    ),
    SyncFailure.RATE_LIMIT_WAIT_EXCEEDED: provider_sentence(
        "{provider} is busy. Try again in a minute."
    ),
    SyncFailure.VALIDATION_FAILED: (
        "The details were refused before anything was sent. Check the e-mail address."
    ),
    SyncFailure.NOT_PERMITTED_HERE: provider_sentence(
        "This server is not allowed to call {provider}."
    ),
    SyncFailure.ALREADY_LINKED_ELSEWHERE: provider_sentence(
        "That {provider} record is already connected to another operator. Nothing was changed."
    ),
    ALREADY_LINKED: provider_sentence(
        "This operator is already connected to a different {provider} customer. "
        "Nothing was changed."
    ),
    ARCHIVED: "This operator is archived.",
    PHONE_INVALID: "Fix the phone number first.",
}
DEFAULT_MESSAGE = provider_sentence("The call to {provider} failed. The operator is saved.")
AWAITING_LINK_MESSAGE = provider_sentence(
    "No e-mail address: connect the {provider} customer by hand."
)
LINK_REJECTED_MESSAGE = provider_sentence(
    "{provider} did not recognise that customer ID. Nothing was saved."
)


class Action(StrEnum):
    """What a sync or link call did."""

    NO_CALL = "no_call"
    AWAITING_LINK = "awaiting_link"
    LINKED_EXISTING = "linked_existing"
    CREATED = "created"
    UPDATED = "updated"
    FAILED = "failed"
    UPDATE_FAILED = "update_failed"
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
    #: Comma-separated key names the provider rejected (``UPDATE_FAILED`` only).
    fields: str = ""

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
        if self.action is Action.UPDATED:
            return "Updated the customer at the payment provider."
        if self.action is Action.UPDATE_FAILED:
            named = f" (fields {', '.join(self.fields.split(','))})" if self.fields else ""
            return (
                "Saved here. The payment provider did not accept the update: "
                f"{self.failure}{named}. Retry when ready."
            )
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

    Without a customer reference, a ``failed`` row stays failed (only the retry button
    calls again), unless its e-mail was removed, which makes it wait for a manual link.
    With one, a change of the customer payload marks the row ``pending`` so that
    :func:`sync_operator` updates the provider's customer (PLAN-006c); any other edit
    keeps the status.
    """
    with transaction.atomic():
        operator = _locked(operator_id)
        before = _snapshot(operator)
        payload = _payload(operator)
        for name, value in _editable(fields).items():
            setattr(operator, name, value)
        if _current_ref(operator, KIND_CUSTOMER) is not None:
            if _payload(operator) != payload:
                operator.sync_status = SyncStatus.PENDING
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
    """Link, create or update the operator's provider customer, one call path per row.

    Calls the provider only while the locked row is ``pending`` (or ``failed`` with
    ``retry=True``) and not archived. A row with a current customer reference gets one
    ``update_customer`` call with :func:`customer_input` (PLAN-006c), with or without an
    e-mail; a row without one needs an e-mail and is looked up, then linked or created.
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
        if operator.archived_at is not None or status != SyncStatus.PENDING:
            return SyncOutcome(Action.NO_CALL, operator.sync_status)
        customer = _current_ref(operator, KIND_CUSTOMER)
        if customer is not None:
            refused = _refused_before_call(operator, retry=retry)
            if refused is not None:
                return refused
            return _update_customer(operator, customer.external_id, provider=provider, actor=actor)
        if not operator.email:
            operator.sync_status = SyncStatus.AWAITING_LINK
            operator.save(update_fields=["sync_status", "updated_at"])
            return SyncOutcome(Action.AWAITING_LINK, operator.sync_status)
        refused = _refused_before_call(operator, retry=retry)
        if refused is not None:
            return refused

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


def _refused_before_call(operator: Operator, *, retry: bool) -> SyncOutcome | None:
    """The retry lock and the phone rule, checked under the row lock; nothing is written."""
    wait = retry_wait_seconds(operator) if retry else 0
    if wait:
        return SyncOutcome(Action.RETRY_TOO_SOON, operator.sync_status, retry_in=wait)
    if not phone_is_valid(operator):
        # Nothing leaves: the row waits for a fixed phone.
        return SyncOutcome(Action.FAILED, operator.sync_status, PHONE_INVALID)
    return None


def _update_customer(
    operator: Operator, external_id: str, *, provider: PaymentProvider, actor: User | None
) -> SyncOutcome:
    """Send the locked row's payload to its provider customer; audit either outcome.

    The audit rows name the changed fields and mask the e-mail and customer ID; they
    never carry a field value.
    """
    before = _push_summary(operator)
    changed = _changed_since_push(operator)
    operator.sync_attempted_at = timezone.now()
    try:
        # TODO-VERIFY-056: whether omitting `email` on PUT /customer/{id} (an operator
        # whose e-mail was removed) clears the e-mail at Stax or leaves it unchanged.
        # Until known, receipts may still go to the old address; the mismatch flag set
        # from the `update_customer` webhook surfaces it.
        provider.update_customer(external_id, customer_input(operator))
    except ProviderError as error:
        failed = _fail(
            operator,
            before,
            actor,
            failure_name(error),
            needs_review=False,
            fields=failure_fields(error),
            summary=lambda row: _push_summary(row, changed, external_id),
        )
        return dataclasses.replace(
            failed, action=Action.UPDATE_FAILED, fields=operator.sync_failure_fields
        )
    operator.sync_status = SyncStatus.LINKED
    operator.sync_failure = ""
    operator.sync_failure_fields = ""
    operator.save()
    _audit(
        actor,
        "operator.sync_updated",
        operator,
        before,
        _push_summary(operator, changed, external_id),
    )
    return SyncOutcome(Action.UPDATED, operator.sync_status)


def provider_updated_at(operator: Operator) -> datetime | None:
    """When the provider's customer was last updated, if that is the latest push."""
    latest = (
        AuditLog.objects.filter(
            entity_type=ENTITY_OPERATOR, entity_id=str(operator.pk), action__in=PUSHED_ACTIONS
        )
        .order_by("-id")
        .values_list("action", "at")
        .first()
    )
    if latest is None or latest[0] != "operator.sync_updated":
        return None
    return latest[1]


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


def mask_email(value: str | None) -> str:
    """Only the domain of an e-mail address; empty when there is none."""
    if not value or "@" not in value:
        return ""
    return f"…@{value.rsplit('@', 1)[1]}"


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
    summary: Callable[[Operator], dict[str, Any]] | None = None,
) -> SyncOutcome:
    operator.sync_status = SyncStatus.FAILED
    operator.sync_failure = failure
    operator.sync_failure_fields = fields
    operator.needs_review = operator.needs_review or needs_review
    operator.save()
    after = (summary or _snapshot)(operator)
    _audit(actor, "operator.sync_failed", operator, before, after)
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


def _payload(operator: Operator) -> CustomerInput:
    """What :func:`customer_input` builds, with the e-mail as ``Operator.save`` stores it."""
    return dataclasses.replace(
        customer_input(operator), operator_email=normalise_email(operator.email or "")
    )


def _changed_since_push(operator: Operator) -> list[str]:
    """The payload fields edited since the provider last received this operator.

    Read from the ``operator.updated`` audit rows written after the latest push, so a
    retry names the same fields as the attempt it repeats.
    """
    rows = AuditLog.objects.filter(entity_type=ENTITY_OPERATOR, entity_id=str(operator.pk))
    last_push = (
        rows.filter(action__in=PUSHED_ACTIONS).order_by("-id").values_list("id", flat=True).first()
    )
    edits = rows.filter(action="operator.updated")
    if last_push is not None:
        edits = edits.filter(id__gt=last_push)
    changed: set[str] = set()
    for edit_before, edit_after in edits.values_list("before", "after"):
        old, new = edit_before or {}, edit_after or {}
        changed.update(name for name in PAYLOAD_FIELDS if old.get(name) != new.get(name))
    return [name for name in PAYLOAD_FIELDS if name in changed]


def _push_summary(
    operator: Operator, changed: Sequence[str] | None = None, external_id: str = ""
) -> dict[str, Any]:
    """The audit view of an update: status, field names and masked identifiers only."""
    values: dict[str, Any] = {
        "sync_status": operator.sync_status,
        "sync_failure": operator.sync_failure,
        "sync_failure_fields": operator.sync_failure_fields,
    }
    if changed is not None:
        values["fields"] = list(changed)
        values["email"] = mask_email(operator.email)
        values["customer"] = mask_identifier(external_id)
    return values


def _json_value(value: Any) -> Any:
    """A consent time as ISO text in UTC: the audit row is JSON, and the same moment read
    from the form (local time) and from the database (UTC) must compare equal."""
    return value.astimezone(UTC).isoformat() if isinstance(value, datetime) else value


def _snapshot(operator: Operator) -> dict[str, Any]:
    values: dict[str, Any] = {
        name: _json_value(getattr(operator, name)) for name in OPERATOR_FIELDS
    }
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

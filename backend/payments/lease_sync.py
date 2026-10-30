"""Leases and their payment-provider customer (docs/qa/PLAN-014 C3; before it, the
operator's customer of docs/qa/PLAN-006-operators.md §4.2, ``payments.operator_sync``).

Since PLAN-014 every lease is its own customer at the provider (in the provider's dashboard
every customer is a store): the customer and its bank method are ``external_refs`` rows of
the lease (``entity_type = "lease"``), and the details sent are the lease's billing details
(``ledger.LeaseBilling``) with the operator's full name. A lease that shares another
lease's account (``LeaseBilling.shares_account_of``) bills through that lease's customer and
holds no reference of its own: nothing here calls the provider for it, and no reference is
ever written for it (:class:`SharesAccount`).

The only module through which the billing screens reach the provider. The worker calls the
functions below (``lease_sync``, ``lease_retry``, ``lease_link`` jobs) with the provider it
obtained from :func:`payments.get_provider`.

Order of work, for every lease whose billing details hold an e-mail (D3, D6, Rule 5):

1. The local row is written first, in its own transaction, with ``sync_status = pending``
   (:func:`update_billing`). A provider failure can therefore never lose staff input, and a
   local failure never leaves an orphan at the provider.
2. :func:`sync_lease` locks the lease row (``select_for_update``) and calls the provider
   only while the row is ``pending`` and has no current customer reference. It looks the
   e-mail up first and compares each returned record here, lower-cased and stripped, for
   equality: a match is linked, several matches link the oldest and flag the lease for
   review, no match creates one customer. Two submissions therefore produce one customer.
3. A :class:`~payments.errors.ProviderError` marks the row ``failed`` with the error's
   class name and the time, and is not retried: :func:`sync_lease` with ``retry=True`` is
   the manual retry. A :class:`~payments.errors.ProviderRejected` also stores the key names
   the provider objected to (``sync_failure_fields``, never a value). A retry is refused
   while the last attempt is less than :data:`RETRY_LOCK_SECONDS` old; the check is made
   under the row lock.

A lease without a billing e-mail makes no call (D4): it waits for :func:`link_lease`, which
reads the customer by ID to prove it exists and stores the customer and payment-method IDs
staff copied from the dashboard.

An edit of the billing details of a lease that already has a customer reference and changes
what :func:`customer_input` builds marks the row ``pending``; :func:`sync_lease` then sends
that same payload with ``update_customer`` under the row lock.

Every change writes one ``audit_log`` row on the lease. Nothing from a provider response is
stored except the customer ID. Failure text is a class name or a fixed sentence from
:data:`MESSAGES`, never a provider message.
"""

from __future__ import annotations

import dataclasses
import math
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from ledger.models import AuditLog, Lease, LeaseBilling, SyncFailure, SyncStatus
from ledger.wording import provider_sentence, with_provider
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
ENTITY_LEASE = "lease"
#: The entity of the processor references before PLAN-014 (ledger migration 0059 moved
#: them to the leases); a customer reference still on an operator is a mismatch.
ENTITY_OPERATOR = "operator"
KIND_CUSTOMER = "customer"
KIND_PAYMENT_METHOD = "payment_method"

#: A manual retry waits this long after the last attempt (PLAN-006a §3.3).
RETRY_LOCK_SECONDS = 60

#: Identifier syntax accepted from staff, as the adapter's own validation (PLAN-005).
IDENTIFIER = re.compile(r"\A[A-Za-z0-9_-]{1,255}\Z")

#: The billing fields staff edit, in form order (``POST /leases/<pk>/billing/``).
BILLING_FIELDS: tuple[str, ...] = (
    "company_name",
    "email",
    "phone",
    "address_line1",
    "address_line2",
    "city",
    "state",
    "postal_code",
)

#: Audit actions after which the provider holds the lease's current details.
PUSHED_ACTIONS: tuple[str, ...] = (
    "lease.sync_created",
    "lease.sync_linked",
    "lease.sync_updated",
    "lease.link_set",
)

#: Local refusals of the link form (no provider call was made).
ALREADY_LINKED = "AlreadyLinked"
ARCHIVED = "Archived"
#: The lease bills through another lease's account and holds no reference of its own.
SHARES_ACCOUNT = "SharesAccount"
#: Local refusal of a sync: the stored phone fails the rule (PLAN-006b §2.4).
PHONE_INVALID = "PhoneInvalid"

#: Fixed sentences shown to staff, keyed by failure class name or local refusal.
MESSAGES: dict[str, str] = {
    SyncFailure.PROVIDER_REJECTED: provider_sentence(
        "{provider} refused the request. Check the billing details, then try again."
    ),
    SyncFailure.PROVIDER_UNAVAILABLE: provider_sentence(
        "{provider} is not answering. The billing details are saved; try again later."
    ),
    SyncFailure.TRANSPORT_ERROR: provider_sentence(
        "{provider} could not be reached. The billing details are saved; try again later."
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
        "That {provider} record is already connected to another lease. Nothing was changed."
    ),
    ALREADY_LINKED: provider_sentence(
        "This lease is already connected to a different {provider} customer. Nothing was changed."
    ),
    ARCHIVED: "This operator is archived.",
    SHARES_ACCOUNT: (
        "This lease uses another lease's account. Switch it to its own account first. "
        "Nothing was changed."
    ),
    PHONE_INVALID: "Fix the phone number first.",
}
DEFAULT_MESSAGE = provider_sentence("The call to {provider} failed. The billing details are saved.")
AWAITING_LINK_MESSAGE = provider_sentence(
    "No billing e-mail address: connect the {provider} customer by hand."
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
    #: The lease's ``sync_status`` afterwards.
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
                return with_provider(
                    "Connected to the oldest of several {provider} customers with this "
                    "e-mail. The lease is marked Needs review."
                )
            return with_provider("Connected to the existing {provider} customer with this e-mail.")
        if self.action is Action.CREATED:
            return with_provider("Created the customer in {provider}.")
        if self.action is Action.UPDATED:
            return with_provider("Updated the customer in {provider}.")
        if self.action is Action.UPDATE_FAILED:
            words = SyncFailure(self.failure).label if self.failure in SyncFailure.values else ""
            failure = f": {words}" if words else ""
            return with_provider(
                f"Saved here. {{provider}} did not accept the update{failure}. Retry when ready."
            )
        if self.action is Action.AWAITING_LINK:
            return str(AWAITING_LINK_MESSAGE)
        if self.action is Action.LINK_SET:
            return with_provider("Connected to {provider}. Autopay can be set up.")
        if self.action is Action.LINK_REFUSED and self.failure == SyncFailure.PROVIDER_REJECTED:
            return str(LINK_REJECTED_MESSAGE)
        if self.action in (Action.FAILED, Action.LINK_REFUSED):
            return str(MESSAGES.get(self.failure, DEFAULT_MESSAGE))
        return ""


class SharesAccount(Exception):  # noqa: N818 (reads as a state, like the provider errors)
    """A lease that shares another lease's account may not hold references of its own."""


class OperatorArchived(Exception):  # noqa: N818 (reads as a state, like the provider errors)
    """The lease's operator is archived: its billing details may not change (the
    :data:`ARCHIVED` state :func:`link_lease` answers with)."""


# --- Local writes -------------------------------------------------------------------


def billing_of(lease: Lease) -> LeaseBilling:
    """The lease's billing row; an empty one (not saved) when it has none yet."""
    try:
        return lease.billing
    except LeaseBilling.DoesNotExist:
        return LeaseBilling(lease=lease)


def update_billing(
    lease_id: uuid.UUID, fields: Mapping[str, Any], *, actor: User | None
) -> tuple[Lease, LeaseBilling]:
    """Apply staff edits of the billing details under the lease's row lock; audited
    ``lease_billing.changed`` with the fields before and after when something changed.

    A lease with its own customer reference is marked ``pending`` when the payload
    :func:`customer_input` builds changed, so the ``lease_sync`` job updates the provider's
    customer; one without a reference and with an e-mail is marked ``pending`` (unless its
    last attempt failed: only the retry calls again), so the job links or creates its
    customer; one without an e-mail waits for a manual link. A lease that shares another
    lease's account keeps its status: nothing is sent for it. A lease of an archived
    operator raises :class:`OperatorArchived` and writes nothing.
    """
    with transaction.atomic():
        lease = (
            Lease.objects.select_for_update(of=("self",))
            .select_related("operator", "store")
            .get(pk=lease_id)
        )
        if _archived(lease):
            raise OperatorArchived
        billing = billing_of(lease)
        before = billing_snapshot(billing)
        payload = _payload(lease, billing)
        for name in BILLING_FIELDS:
            if name in fields:
                setattr(billing, name, fields[name] or "")
        billing.save()
        lease.billing = billing
        if billing.shares_account_of_id is None:
            if _current_ref(lease, KIND_CUSTOMER) is not None:
                if _payload(lease, billing) != payload:
                    lease.sync_status = SyncStatus.PENDING
            elif not billing.email:
                lease.sync_status = SyncStatus.AWAITING_LINK
                lease.sync_failure = ""
                lease.sync_failure_fields = ""
            elif lease.sync_status != SyncStatus.FAILED:
                lease.sync_status = SyncStatus.PENDING
            lease.save(
                update_fields=["sync_status", "sync_failure", "sync_failure_fields", "updated_at"]
            )
        after = billing_snapshot(billing)
        if after != before:
            AuditLog.objects.create(
                actor=actor,
                action="lease_billing.changed",
                entity_type=ENTITY_LEASE,
                entity_id=str(lease.pk),
                before=before,
                after=after,
            )
    return lease, billing


def mark_renamed(operator_id: uuid.UUID) -> list[Lease]:
    """Mark ``pending`` every lease of the operator that holds its own current customer
    reference, after the operator's display name changed: :func:`customer_input` sends
    that name (first and last), so each lease's ``lease_sync`` job updates its customer.
    A lease that shares another's account holds no reference: nothing is sent for it.
    Returns the leases marked, locked; the caller's transaction holds the lock."""
    own = ExternalRef.objects.filter(
        entity_type=ENTITY_LEASE,
        provider=PROVIDER_KEY,
        external_kind=KIND_CUSTOMER,
        superseded_at__isnull=True,
        entity_id__in=Lease.objects.filter(operator_id=operator_id).values("pk"),
    ).values("entity_id")
    marked = list(Lease.objects.select_for_update().filter(pk__in=own).order_by("pk"))
    for lease in marked:
        lease.sync_status = SyncStatus.PENDING
        lease.save(update_fields=["sync_status", "updated_at"])
    return marked


# --- Provider calls -----------------------------------------------------------------


def sync_lease(
    lease_id: uuid.UUID,
    *,
    provider: PaymentProvider,
    actor: User | None,
    retry: bool = False,
    own_customer: bool = False,
) -> SyncOutcome:
    """Link, create or update the lease's provider customer, one call path per row.

    Calls the provider only while the locked row is ``pending`` (or ``failed`` with
    ``retry=True``), its operator is not archived and it does not share another lease's
    account. A row with a current customer reference gets one ``update_customer`` call
    with :func:`customer_input`; a row without one needs a billing e-mail and is looked
    up, then linked or created. With ``retry=True`` it refuses (``RETRY_TOO_SOON``, no
    call) while the last attempt is less than :data:`RETRY_LOCK_SECONDS` old. A stored
    phone that fails the provider's rule refuses the sync (``PHONE_INVALID``) before any
    call and writes nothing.

    ``own_customer=True`` (PLAN-014 C9, the ``create_lease_customers`` command): a lease
    that shares another lease's account gets a customer of its own, still sharing until
    staff switch it (``shares_account_of`` is not changed). Only a lease without its own
    customer reference is sent. The e-mail look-up never adopts a customer that any other
    entity holds (the account it shares included): a match held elsewhere is skipped, and
    when every match is held elsewhere the customer is created. A match no one holds is
    linked as usual.
    """
    with transaction.atomic():
        lease = (
            Lease.objects.select_for_update(of=("self",))
            .select_related("operator", "store")
            .get(pk=lease_id)
        )
        billing = billing_of(lease)
        status = lease.sync_status
        if retry and status == SyncStatus.FAILED:
            status = SyncStatus.PENDING
        sharer = billing.shares_account_of_id is not None
        own = own_customer and sharer
        if _archived(lease) or (sharer and not own) or status != SyncStatus.PENDING:
            return SyncOutcome(Action.NO_CALL, lease.sync_status)
        customer = _current_ref(lease, KIND_CUSTOMER)
        if own and customer is not None:
            return SyncOutcome(Action.NO_CALL, lease.sync_status)
        if customer is not None:
            refused = _refused_before_call(lease, billing, retry=retry)
            if refused is not None:
                return refused
            return _update_customer(
                lease, billing, customer.external_id, provider=provider, actor=actor
            )
        if not billing.email:
            lease.sync_status = SyncStatus.AWAITING_LINK
            lease.save(update_fields=["sync_status", "updated_at"])
            return SyncOutcome(Action.AWAITING_LINK, lease.sync_status)
        refused = _refused_before_call(lease, billing, retry=retry)
        if refused is not None:
            return refused

        before = _snapshot(lease)
        lease.sync_attempted_at = timezone.now()
        email = normalise_email(billing.email)
        try:
            matches = [
                record
                for record in provider.find_customers(CustomerQuery(operator_email=email))
                if normalise_email(record.operator_email) == email
            ]
            free = [
                record
                for record in matches
                if not _linked_elsewhere(record.external_id, KIND_CUSTOMER, lease.pk)
            ]
            needs_review = len(matches) > 1 or len(free) != len(matches)
            if own:
                # A customer another entity holds is never adopted (PLAN-014 C9).
                needs_review = len(free) > 1
            if matches and not free and not own:
                return _fail(
                    lease,
                    before,
                    actor,
                    SyncFailure.ALREADY_LINKED_ELSEWHERE,
                    needs_review=needs_review,
                )
            if free:
                chosen, action = oldest(free), Action.LINKED_EXISTING
            else:
                chosen = provider.create_customer(customer_input(lease, billing))
                action = Action.CREATED
        except ProviderError as error:
            return _fail(
                lease,
                before,
                actor,
                failure_name(error),
                needs_review=False,
                fields=failure_fields(error),
            )

        _add_ref(lease, KIND_CUSTOMER, chosen.external_id, own_customer=own)
        lease.sync_status = SyncStatus.LINKED
        lease.sync_failure = ""
        lease.sync_failure_fields = ""
        lease.needs_review = lease.needs_review or needs_review
        lease.save()
        after = {**_snapshot(lease), "customer": mask_identifier(chosen.external_id)}
        if action is Action.LINKED_EXISTING:
            after["matches"] = len(matches)
        if own:
            after["own_customer"] = True
        audit_action = (
            "lease.sync_linked" if action is Action.LINKED_EXISTING else "lease.sync_created"
        )
        _audit(actor, audit_action, lease, before, after)
        return SyncOutcome(action, lease.sync_status, needs_review=needs_review)


def _refused_before_call(lease: Lease, billing: LeaseBilling, *, retry: bool) -> SyncOutcome | None:
    """The retry lock and the phone rule, checked under the row lock; nothing is written."""
    wait = retry_wait_seconds(lease) if retry else 0
    if wait:
        return SyncOutcome(Action.RETRY_TOO_SOON, lease.sync_status, retry_in=wait)
    if not phone_is_valid(billing):
        return SyncOutcome(Action.FAILED, lease.sync_status, PHONE_INVALID)
    return None


def _update_customer(
    lease: Lease,
    billing: LeaseBilling,
    external_id: str,
    *,
    provider: PaymentProvider,
    actor: User | None,
) -> SyncOutcome:
    """Send the locked row's payload to its provider customer; audit either outcome.

    The audit rows name the changed fields and mask the e-mail and customer ID; they never
    carry a field value.
    """
    before = _push_summary(lease, billing)
    changed = _changed_since_push(lease)
    lease.sync_attempted_at = timezone.now()
    try:
        # TODO-VERIFY-056: whether omitting `email` on PUT /customer/{id} clears the
        # e-mail at Stax or leaves it unchanged.
        provider.update_customer(external_id, customer_input(lease, billing))
    except ProviderError as error:
        failed = _fail(
            lease,
            before,
            actor,
            failure_name(error),
            needs_review=False,
            fields=failure_fields(error),
            summary=lambda row: _push_summary(row, billing, changed, external_id),
        )
        return dataclasses.replace(
            failed, action=Action.UPDATE_FAILED, fields=lease.sync_failure_fields
        )
    lease.sync_status = SyncStatus.LINKED
    lease.sync_failure = ""
    lease.sync_failure_fields = ""
    lease.save()
    _audit(
        actor,
        "lease.sync_updated",
        lease,
        before,
        _push_summary(lease, billing, changed, external_id),
    )
    return SyncOutcome(Action.UPDATED, lease.sync_status)


def provider_updated_at(lease: Lease) -> datetime | None:
    """When the provider's customer was last updated, if that is the latest push."""
    latest = (
        AuditLog.objects.filter(
            entity_type=ENTITY_LEASE, entity_id=str(lease.pk), action__in=PUSHED_ACTIONS
        )
        .order_by("-id")
        .values_list("action", "at")
        .first()
    )
    if latest is None or latest[0] != "lease.sync_updated":
        return None
    return latest[1]


def link_lease(
    lease_id: uuid.UUID,
    *,
    customer_external_id: str,
    payment_method_external_id: str,
    provider: PaymentProvider,
    actor: User | None,
) -> SyncOutcome:
    """Store the customer and payment-method IDs staff copied from the provider (D4).

    The customer is read by ID first (one ``get_customer`` call); if the provider does not
    return it, nothing is stored. The payment method cannot be read through the interface
    and is stored as given. A lease that shares another lease's account is refused
    (``SHARES_ACCOUNT``): it holds no reference of its own.
    """
    for value in (customer_external_id, payment_method_external_id):
        if not IDENTIFIER.match(value):
            return SyncOutcome(Action.LINK_REFUSED, "", SyncFailure.VALIDATION_FAILED)
    with transaction.atomic():
        lease = (
            Lease.objects.select_for_update(of=("self",))
            .select_related("operator", "store")
            .get(pk=lease_id)
        )
        if _archived(lease):
            return SyncOutcome(Action.LINK_REFUSED, lease.sync_status, ARCHIVED)
        if billing_of(lease).shares_account_of_id is not None:
            return SyncOutcome(Action.LINK_REFUSED, lease.sync_status, SHARES_ACCOUNT)
        current = _current_ref(lease, KIND_CUSTOMER)
        if current is not None and current.external_id != customer_external_id:
            return SyncOutcome(Action.LINK_REFUSED, lease.sync_status, ALREADY_LINKED)
        if _linked_elsewhere(customer_external_id, KIND_CUSTOMER, lease.pk) or (
            _linked_elsewhere(payment_method_external_id, KIND_PAYMENT_METHOD, lease.pk)
        ):
            return SyncOutcome(
                Action.LINK_REFUSED, lease.sync_status, SyncFailure.ALREADY_LINKED_ELSEWHERE
            )
        try:
            provider.get_customer(customer_external_id)
        except ProviderError as error:
            return SyncOutcome(Action.LINK_REFUSED, lease.sync_status, failure_name(error))

        before = _snapshot(lease)
        if current is None:
            _add_ref(lease, KIND_CUSTOMER, customer_external_id)
        method = _current_ref(lease, KIND_PAYMENT_METHOD)
        if method is not None and method.external_id != payment_method_external_id:
            method.superseded_at = timezone.now()
            method.save(update_fields=["superseded_at"])
            method = None
        if method is None:
            _add_ref(lease, KIND_PAYMENT_METHOD, payment_method_external_id)
        lease.sync_status = SyncStatus.LINKED
        lease.sync_failure = ""
        lease.sync_failure_fields = ""
        lease.save()
        after = {
            **_snapshot(lease),
            "customer": mask_identifier(customer_external_id),
            "payment_method": mask_identifier(payment_method_external_id),
        }
        _audit(actor, "lease.link_set", lease, before, after)
        return SyncOutcome(Action.LINK_SET, lease.sync_status)


# --- Helpers ------------------------------------------------------------------------


def normalise_email(value: str) -> str:
    """Lower-cased and stripped, as the models store it."""
    return value.strip().lower()


def oldest(records: Sequence[CustomerRecord]) -> CustomerRecord:
    """The record with the earliest ``created_at`` in its summary; ties keep list order.

    TODO-VERIFY-032: the customer object's ``created_at`` key and its format
    (``YYYY-MM-DD hh:mm:ss``, which sorts as text) are assumed. A record without it sorts
    after every record that has it.
    """

    def key(indexed: tuple[int, CustomerRecord]) -> tuple[bool, str, int]:
        index, record = indexed
        created = record.raw_summary.get("created_at")
        text = created if isinstance(created, str) and created else ""
        return (not text, text, index)

    return min(enumerate(records), key=key)[1]


def split_name(full_name: str) -> tuple[str, str]:
    """A full name as first and last name, split on the last space ("Ana Maria Lopez" ->
    "Ana Maria", "Lopez"); a single word is the first name."""
    words = " ".join(full_name.split())
    first, _, last = words.rpartition(" ")
    return (first, last) if first else (last, "")


def customer_input(lease: Lease, billing: LeaseBilling | None = None) -> CustomerInput:
    """PLAN-014 C3: the lease's company as the company, the operator's full name as first
    and last name, the lease's e-mail and phone, the store number as the reference (the
    lease's id when the store has none)."""
    billing = billing if billing is not None else billing_of(lease)
    first, last = split_name(lease.operator.display_name if lease.operator else "")
    number = lease.store.store_number
    return CustomerInput(
        first_name=first,
        last_name=last,
        company_name=billing.company_name,
        operator_email=billing.email or "",
        # The normalised digits; a stored phone that fails the rule is passed as it is, so
        # the adapter refuses it before any request.
        phone=normalise_phone(billing.phone) or billing.phone,
        reference=str(number) if number is not None else str(lease.pk),
    )


def mask_email(value: str | None) -> str:
    """Only the domain of an e-mail address; empty when there is none."""
    if not value or "@" not in value:
        return ""
    return f"…@{value.rsplit('@', 1)[1]}"


def phone_is_valid(billing: LeaseBilling) -> bool:
    """Whether the billing phone is empty or passes the provider's phone rule."""
    return normalise_phone(billing.phone) is not None


def retry_wait_seconds(row: Any, now: datetime | None = None) -> int:
    """Whole seconds until a manual retry of ``row`` (anything with ``sync_attempted_at``)
    is allowed; 0 when it is allowed now."""
    attempted: datetime | None = row.sync_attempted_at
    if attempted is None:
        return 0
    elapsed = ((now or timezone.now()) - attempted).total_seconds()
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


def current_refs(lease: Lease) -> dict[str, str]:
    """The lease's own current external IDs by kind (unmasked; callers mask). A lease that
    shares another's account has none: ``ledger.services.leases.customer_ref`` follows the
    share."""
    return dict(
        ExternalRef.objects.filter(
            entity_type=ENTITY_LEASE,
            entity_id=lease.pk,
            provider=PROVIDER_KEY,
            superseded_at__isnull=True,
        ).values_list("external_kind", "external_id")
    )


def _archived(lease: Lease) -> bool:
    return lease.operator is not None and lease.operator.archived_at is not None


def _fail(
    lease: Lease,
    before: dict[str, Any],
    actor: User | None,
    failure: str,
    *,
    needs_review: bool,
    fields: str = "",
    summary: Callable[[Lease], dict[str, Any]] | None = None,
) -> SyncOutcome:
    lease.sync_status = SyncStatus.FAILED
    lease.sync_failure = failure
    lease.sync_failure_fields = fields
    lease.needs_review = lease.needs_review or needs_review
    lease.save()
    after = (summary or _snapshot)(lease)
    _audit(actor, "lease.sync_failed", lease, before, after)
    return SyncOutcome(Action.FAILED, lease.sync_status, failure, needs_review)


def _current_ref(lease: Lease, kind: str) -> ExternalRef | None:
    return ExternalRef.objects.filter(
        entity_type=ENTITY_LEASE,
        entity_id=lease.pk,
        provider=PROVIDER_KEY,
        external_kind=kind,
        superseded_at__isnull=True,
    ).first()


def _linked_elsewhere(external_id: str, kind: str, lease_id: uuid.UUID) -> bool:
    return (
        ExternalRef.objects.filter(
            provider=PROVIDER_KEY, external_kind=kind, external_id=external_id
        )
        .exclude(entity_type=ENTITY_LEASE, entity_id=lease_id)
        .exists()
    )


def _add_ref(lease: Lease, kind: str, external_id: str, *, own_customer: bool = False) -> None:
    """Write a reference of the lease; refused (:class:`SharesAccount`) for a lease that
    shares another lease's account (one hop, never a chain or a cycle), except its own
    customer reference when asked for explicitly (``own_customer``, PLAN-014 C9: the
    references that bill it stay the shared account's until staff switch it)."""
    allowed = own_customer and kind == KIND_CUSTOMER
    if (
        not allowed
        and LeaseBilling.objects.filter(lease=lease, shares_account_of__isnull=False).exists()
    ):
        raise SharesAccount
    ExternalRef.objects.create(
        entity_type=ENTITY_LEASE,
        entity_id=lease.pk,
        provider=PROVIDER_KEY,
        external_kind=kind,
        external_id=external_id,
    )


def _payload(lease: Lease, billing: LeaseBilling) -> CustomerInput:
    """What :func:`customer_input` builds, with the e-mail as the model stores it."""
    return dataclasses.replace(
        customer_input(lease, billing), operator_email=normalise_email(billing.email or "")
    )


def _changed_since_push(lease: Lease) -> list[str]:
    """The billing fields edited since the provider last received this lease, read from
    the ``lease_billing.changed`` audit rows written after the latest push."""
    rows = AuditLog.objects.filter(entity_type=ENTITY_LEASE, entity_id=str(lease.pk))
    last_push = (
        rows.filter(action__in=PUSHED_ACTIONS).order_by("-id").values_list("id", flat=True).first()
    )
    edits = rows.filter(action="lease_billing.changed")
    if last_push is not None:
        edits = edits.filter(id__gt=last_push)
    changed: set[str] = set()
    for edit_before, edit_after in edits.values_list("before", "after"):
        old, new = edit_before or {}, edit_after or {}
        changed.update(name for name in BILLING_FIELDS if old.get(name) != new.get(name))
    return [name for name in BILLING_FIELDS if name in changed]


def _push_summary(
    lease: Lease,
    billing: LeaseBilling | None = None,
    changed: Sequence[str] | None = None,
    external_id: str = "",
) -> dict[str, Any]:
    """The audit view of an update: status, field names and masked identifiers only."""
    values: dict[str, Any] = {
        "sync_status": lease.sync_status,
        "sync_failure": lease.sync_failure,
        "sync_failure_fields": lease.sync_failure_fields,
    }
    if changed is not None:
        values["fields"] = list(changed)
        values["email"] = mask_email(billing.email if billing is not None else "")
        values["customer"] = mask_identifier(external_id)
    return values


def billing_snapshot(billing: LeaseBilling) -> dict[str, Any]:
    """The billing row's audit view: its editable fields and the shared account."""
    values: dict[str, Any] = {name: getattr(billing, name) for name in BILLING_FIELDS}
    values["shares_account_of"] = (
        str(billing.shares_account_of_id) if billing.shares_account_of_id else None
    )
    return values


def _snapshot(lease: Lease) -> dict[str, Any]:
    return {
        "sync_status": lease.sync_status,
        "sync_failure": lease.sync_failure,
        "sync_failure_fields": lease.sync_failure_fields,
        "needs_review": lease.needs_review,
    }


def _audit(
    actor: User | None,
    action: str,
    lease: Lease,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> None:
    AuditLog.objects.create(
        actor=actor,
        action=action,
        entity_type=ENTITY_LEASE,
        entity_id=str(lease.pk),
        before=before,
        after=after,
    )

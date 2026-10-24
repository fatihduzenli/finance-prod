"""The contact log of an operator (docs/qa/PLAN-011b D7; PLAN-012 D2 to D5).

Billing records each call, e-mail, visit and promise to pay as a :class:`ContactEntry`,
dated in New York days, optionally about one lease of the operator (the store follows from
it). Every change writes an audit row with its before and after.

**Lease** (D3): when the operator has more than one lease in effect today
(:func:`active_leases`) an entry must name one; a named lease must be the operator's.

**Edit, correct, delete** (D2): the author may edit ``kind``, ``happened_on``, ``body``,
``promised_date`` and ``promised_amount`` while ``now`` is before ``created_at`` plus
:data:`EDIT_WINDOW` (:func:`edit_entry`); a promise whose outcome is recorded keeps its
kind, date and amount. After that, any billing user appends a correction
(:func:`add_correction`). An admin soft deletes an entry with a reason
(:func:`delete_entry`). A deleted entry takes no correction, no edit and no outcome, and a
deleted promise counts for nothing: it is never open, kept or broken.

**Status of a promise** (D4, D5): ``open`` until an outcome is recorded. A later entry
records it (:func:`record_outcome`, called by :func:`add_entry` with ``previous`` and
``outcome``): ``kept`` (``closed_by`` names the new entry), ``rescheduled`` (the new entry
must itself be a promise; a date or amount it leaves out is the old promise's) or
``broken``; ``open`` (or none), "Still open", records the entry and leaves the promise
as it is (PLAN-012c-b D1). A payment keeps it automatically (:func:`close_kept_by_payment`, and
:func:`close_kept` as the daily sweep): ``kept`` with ``kept_by_payment`` set and
``closed_by`` null. Every other kind stays ``open``.

**One open promise per lease** (PLAN-012c-b D3): a new promise closes every other open
promise of the operator on the same lease (no lease is a bucket of its own) as
``rescheduled``, audited with ``"auto": true``; ``closed_by`` is the new entry when it
records no other outcome, else the next newer promise closed here (``closed_by`` is one to
one), else null.

**A payment keeps a promise** when all of these hold: the payment is ``settled`` and not a
refund; its charge or invoice is billed to the promise's operator; when the promise names
a lease, the payment's charge is on that lease (a promise without a lease takes any of the
operator's charges); its ``amount`` is at least the ``promised_amount``; and its
``settled_on`` is from the New York date of the entry's ``created_at`` to the
``promised_date``, both inclusive.

**When a promise is broken** (:func:`broken_promises`), on ``today`` (a New York date):
it is a promise, not deleted, with status ``open``, its ``promised_date`` is before
``today`` (a promise due today or later is never broken), and no payment keeps it (the rule
above, read again so that a payment the sweep has not reached yet still counts). Whatever
the balance. It is an attention item (:mod:`ledger.services.attention`) from the day after
its promised date until an outcome is recorded. The read is one query whatever the number
of promises, and it keeps the most recently recorded broken promise per (operator, lease).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import (
    BooleanField,
    Case,
    Exists,
    F,
    OuterRef,
    Prefetch,
    Q,
    QuerySet,
    When,
)
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone

from finance.dates import day_text
from ledger.models import (
    AuditLog,
    CollectionStatus,
    ContactCorrection,
    ContactEntry,
    ContactKind,
    ContactStatus,
    Lease,
    Operator,
    Payment,
    PaymentStatus,
    Store,
)
from ledger.services.csv_export import CsvWriter
from ledger.validators import contains_bank_routing_pattern, contains_card_number, visible_text
from ledger.wording import format_amount

ENTITY_CONTACT = "contact_entry"
#: The most entries the operator and store pages list; the CSV holds every one.
PAGE_LIMIT = 20
#: Largest promised amount accepted (the money column's range).
AMOUNT_MAX = Decimal("9999999999.99")
#: How long the author may edit an entry after recording it (D2).
EDIT_WINDOW = timedelta(minutes=15)
#: The outcomes a later entry records on an open promise (D5).
OUTCOMES: tuple[str, ...] = (
    ContactStatus.KEPT.value,
    ContactStatus.RESCHEDULED.value,
    ContactStatus.BROKEN.value,
)
#: "Still open (no change)" (PLAN-012c-b D1): the entry leaves the previous promise as it is.
STILL_OPEN = ContactStatus.OPEN.value

BODY_REQUIRED = "A note is required: say what was said or agreed."
BODY_PAYMENT_DATA = "This text may not contain card or account numbers."
PROMISE_FIELDS_REQUIRED = "A promise to pay needs the promised date and amount."
PROMISE_FIELDS_ONLY = "Only a promise to pay has a promised date and amount."
AMOUNT_POSITIVE = "The promised amount must be more than zero."
AMOUNT_TOO_LARGE = "The promised amount is too large."
KIND_REQUIRED = "Choose what kind of contact this was."
PREVIOUS_UNKNOWN = "That promise is not an open promise of this operator."
OUTCOME_UNKNOWN = (
    "Choose what happened to the previous promise: still open, kept, rescheduled or broken."
)
PROMISE_NOT_OPEN = "This promise is no longer open; its outcome is already recorded."
RESCHEDULE_NEEDS_PROMISE = "A rescheduled promise needs a new promise: choose Promise to pay."
LEASE_REQUIRED = "This operator has more than one store: choose the store this is about."
LEASE_UNKNOWN = "That store is not one of this operator's leases."
ARCHIVED = "This operator is archived; no contact can be added."
ADDED = "Contact entry added."
NOT_AUTHOR = "Only the author may edit this entry."
WINDOW_CLOSED = "This entry can no longer be edited; add a correction instead."
DELETED = "This entry was deleted; it cannot be changed."
PROMISE_CLOSED = "This promise has an outcome; its kind, date and amount stay as they are."
EDITED = "Contact entry changed."
UNCHANGED = "Nothing was changed."
CORRECTION_ADDED = "Correction added."
REASON_REQUIRED = "A reason is required: say why this entry is deleted."
ENTRY_DELETED = "Contact entry deleted."

CSV_HEADER: tuple[str, ...] = (
    "Date",
    "Kind",
    "Store",
    "Note",
    "Promised date",
    "Promised amount (USD)",
    "Promise status",
    "Closes promise",
    "Recorded by",
    "Recorded at",
    "Corrections",
    "Deleted at",
    "Delete reason",
)


@dataclass(frozen=True, slots=True)
class ContactInput:
    kind: str
    happened_on: date
    body: str
    promised_date: date | None = None
    promised_amount: Decimal | None = None
    #: The lease the entry is about (D3).
    lease: uuid.UUID | None = None
    #: An open promise of the operator whose outcome this entry records (D5).
    previous: uuid.UUID | None = None
    #: ``kept``, ``rescheduled`` or ``broken``, given with ``previous`` only; ``open`` or
    #: ``""`` leaves ``previous`` open (PLAN-012c-b D1).
    outcome: str = ""


@dataclass(frozen=True, slots=True)
class EditInput:
    kind: str
    happened_on: date
    body: str
    promised_date: date | None = None
    promised_amount: Decimal | None = None


@dataclass(frozen=True, slots=True)
class AddOutcome:
    ok: bool
    message: str
    #: The form field the message belongs to, or ``None``.
    field: str | None = None
    entry: ContactEntry | None = None


# --- Reads ---------------------------------------------------------------------------------


def _listed(rows: QuerySet[ContactEntry], include_deleted: bool) -> QuerySet[ContactEntry]:
    if not include_deleted:
        rows = rows.filter(deleted_at__isnull=True)
    return (
        rows.select_related(
            "author",
            "deleted_by",
            "closed_by",
            "closes__lease",
            "kept_by_payment",
            "lease__store",
            "operator",
        )
        .prefetch_related(
            Prefetch(
                "corrections",
                queryset=ContactCorrection.objects.select_related("author").order_by(
                    "created_at", "id"
                ),
            )
        )
        .order_by("-happened_on", "-created_at", "-id")
    )


def entries(operator: Operator, *, include_deleted: bool = True) -> QuerySet[ContactEntry]:
    """The operator's entries, newest first (by day, then by when they were recorded);
    without ``include_deleted``, the deleted ones are left out (what a viewer sees)."""
    return _listed(ContactEntry.objects.filter(operator=operator), include_deleted)


def store_entries(store: Store, *, include_deleted: bool = True) -> QuerySet[ContactEntry]:
    """The entries about a lease of ``store``, newest first, as :func:`entries`."""
    return _listed(ContactEntry.objects.filter(lease__store=store), include_deleted)


def closed_promise(entry: ContactEntry) -> ContactEntry | None:
    """The promise whose outcome ``entry`` recorded (its ``closed_by`` is ``entry``), or
    ``None``: read from the ``closes`` join of :func:`entries` and :func:`store_entries`."""
    closed: ContactEntry | None = getattr(entry, "closes", None)
    return closed


def closes_sentence(promise: ContactEntry) -> str:
    """PLAN-012c-b D5: "Closed $2,000.00 by Oct 1, 2026 as Broken", the closed promise's
    amount, date and outcome, as the page reads it (DESIGN-SPEC §5); the CSV's cell is
    :func:`closes_csv_text`."""
    return _closes(promise, day_text(promise.promised_date) if promise.promised_date else "")


def closes_csv_text(promise: ContactEntry) -> str:
    """The CSV's form of :func:`closes_sentence`: "Closed $2,000.00 by 2026-10-01 as Broken"
    (an export keeps ISO, DESIGN-SPEC §5)."""
    return _closes(promise, f"{promise.promised_date:%Y-%m-%d}")


def _closes(promise: ContactEntry, day: str) -> str:
    return (
        f"Closed {format_amount(promise.promised_amount or Decimal(0))} by "
        f"{day} as {promise.get_status_display()}"
    )


def with_status(rows: QuerySet[ContactEntry], status: str) -> QuerySet[ContactEntry]:
    """The promises among ``rows`` with ``status`` (a :class:`ContactStatus` value);
    ``rows`` unchanged for any other value."""
    if status not in ContactStatus.values:
        return rows
    return rows.filter(kind=ContactKind.PROMISE, status=status)


def _open_promise() -> Q:
    return Q(kind=ContactKind.PROMISE, status=ContactStatus.OPEN, deleted_at__isnull=True)


def open_promises(operator: Operator) -> QuerySet[ContactEntry]:
    """The operator's open promises not deleted, newest first: what an outcome may close."""
    return entries(operator).filter(_open_promise())


def open_promise(operator: Operator | None, lease: Lease | None) -> ContactEntry | None:
    """The store page's header (PLAN-012d D1): the operator's newest open promise, not
    deleted, about ``lease`` or about no store; ``None`` without an operator."""
    if operator is None:
        return None
    about = Q(lease__isnull=True)
    if lease is not None:
        about |= Q(lease=lease)
    return (
        ContactEntry.objects.filter(_open_promise(), about, operator=operator)
        .order_by("-happened_on", "-created_at", "-id")
        .first()
    )


def _in_effect(today: date) -> Q:
    """Collecting or paused, started on or before ``today`` (or no start date), not ended
    before it."""
    return (
        Q(collection_status__in=[CollectionStatus.ACTIVE, CollectionStatus.PAUSED])
        & (Q(started_on__isnull=True) | Q(started_on__lte=today))
        & (Q(ended_on__isnull=True) | Q(ended_on__gte=today))
    )


def active_leases(operator: Operator | uuid.UUID, today: date) -> QuerySet[Lease]:
    """The operator's leases in effect on ``today``, by store."""
    return (
        Lease.objects.filter(_in_effect(today), operator=operator)
        .select_related("store")
        .order_by("store__external_label", "id")
    )


def store_leases(store: Store, today: date) -> QuerySet[Lease]:
    """The store's leases in effect on ``today`` that have an operator, the later start
    first (two on a hand-over day)."""
    return (
        Lease.objects.filter(_in_effect(today), store=store, operator__isnull=False)
        .select_related("operator", "store")
        .order_by("-started_on", "-created_at")
    )


def store_lease(store: Store, today: date) -> Lease | None:
    """The store's lease in effect on ``today`` that has an operator; on a hand-over day
    the later start wins."""
    return store_leases(store, today).first()


def store_contact_lease(store: Store, today: date) -> Lease | None:
    """The lease the store page's Add entry is about (PLAN-012c D1): the store's lease in
    effect on ``today`` of an operator not archived (the later start on a hand-over day),
    else the store's most recent lease of an operator not archived (latest ``started_on``,
    a lease without one last, then latest ``created_at``); ``None`` when there is none."""
    current = store_leases(store, today).filter(operator__archived_at__isnull=True).first()
    if current is not None:
        return current
    return (
        Lease.objects.filter(
            store=store, operator__isnull=False, operator__archived_at__isnull=True
        )
        .select_related("operator", "store")
        .order_by(F("started_on").desc(nulls_last=True), "-created_at")
        .first()
    )


def edit_closes_at(entry: ContactEntry) -> datetime:
    return entry.created_at + EDIT_WINDOW


def can_edit(entry: ContactEntry, actor: User | None, now: datetime) -> bool:
    """The author, while the edit window is open, of an entry not deleted."""
    return (
        actor is not None
        and entry.author_id is not None
        and entry.author_id == actor.pk
        and entry.deleted_at is None
        and now < edit_closes_at(entry)
    )


# --- Writes --------------------------------------------------------------------------------


def _refusal(
    kind: str, body: str, promised_date: date | None, promised_amount: Decimal | None
) -> tuple[str, str] | None:
    if kind not in ContactKind.values:
        return KIND_REQUIRED, "kind"
    if not body:
        return BODY_REQUIRED, "body"
    if contains_card_number(body) or contains_bank_routing_pattern(body):
        return BODY_PAYMENT_DATA, "body"
    promise = kind == ContactKind.PROMISE
    if promise and (promised_date is None or promised_amount is None):
        field = "promised_date" if promised_date is None else "promised_amount"
        return PROMISE_FIELDS_REQUIRED, field
    if not promise and (promised_date is not None or promised_amount is not None):
        return PROMISE_FIELDS_ONLY, "promised_date"
    if promised_amount is not None:
        if promised_amount <= 0:
            return AMOUNT_POSITIVE, "promised_amount"
        if promised_amount > AMOUNT_MAX:
            return AMOUNT_TOO_LARGE, "promised_amount"
    return None


def _state(entry: ContactEntry) -> dict[str, object]:
    return {
        "operator": str(entry.operator_id),
        "lease": str(entry.lease_id) if entry.lease_id else None,
        "kind": entry.kind,
        "happened_on": entry.happened_on.isoformat(),
        "promised_date": entry.promised_date.isoformat() if entry.promised_date else None,
        "promised_amount": (
            str(entry.promised_amount) if entry.promised_amount is not None else None
        ),
        "body": entry.body,
        "status": entry.status,
    }


def _outcome_state(entry: ContactEntry) -> dict[str, object]:
    return {
        "status": entry.status,
        "closed_by": str(entry.closed_by_id) if entry.closed_by_id else None,
        "kept_by_payment": str(entry.kept_by_payment_id) if entry.kept_by_payment_id else None,
    }


def add_entry(operator_id: uuid.UUID, data: ContactInput, *, actor: User | None) -> AddOutcome:
    """Record one entry, and the outcome of ``data.previous`` when there is one (``open``
    or ``""`` leaves it open). A new promise closes the other open promises of its lease
    (the module docstring's rule). Refusals change nothing and name the field. The body is
    stored as :func:`visible_text`."""
    body = visible_text(data.body or "")
    if data.kind not in ContactKind.values:
        return AddOutcome(False, KIND_REQUIRED, "kind")
    if data.outcome not in ("", STILL_OPEN, *OUTCOMES) or (
        data.previous is None and data.outcome in OUTCOMES
    ):
        return AddOutcome(False, OUTCOME_UNKNOWN, "outcome")
    closing = data.outcome if data.outcome in OUTCOMES else ""
    with transaction.atomic():
        operator = Operator.objects.select_for_update().get(pk=operator_id)
        if operator.archived_at is not None:
            return AddOutcome(False, ARCHIVED)
        previous: ContactEntry | None = None
        if data.previous is not None:
            previous = (
                ContactEntry.objects.select_for_update()
                .filter(_open_promise(), pk=data.previous, operator=operator)
                .first()
            )
            if previous is None:
                return AddOutcome(False, PREVIOUS_UNKNOWN, "previous")
        promised_date, promised_amount = data.promised_date, data.promised_amount
        if previous is not None and closing == ContactStatus.RESCHEDULED:
            if data.kind != ContactKind.PROMISE:
                return AddOutcome(False, RESCHEDULE_NEEDS_PROMISE, "kind")
            if promised_date is None:
                promised_date = previous.promised_date
            if promised_amount is None:
                promised_amount = previous.promised_amount
        refusal = _refusal(data.kind, body, promised_date, promised_amount)
        if refusal is not None:
            return AddOutcome(False, refusal[0], refusal[1])
        lease: Lease | None = None
        if data.lease is not None:
            lease = Lease.objects.filter(pk=data.lease, operator=operator).first()
            if lease is None:
                return AddOutcome(False, LEASE_UNKNOWN, "lease")
        elif active_leases(operator, timezone.localdate()).count() > 1:
            return AddOutcome(False, LEASE_REQUIRED, "lease")
        entry = ContactEntry(
            operator=operator,
            lease=lease,
            kind=data.kind,
            happened_on=data.happened_on,
            promised_date=promised_date,
            promised_amount=promised_amount,
            body=body,
            author=actor,
        )
        try:
            entry.save()
        except ValidationError:
            return AddOutcome(False, BODY_PAYMENT_DATA, "body")
        created = _state(entry)
        if previous is not None and not closing:
            created |= {"previous": str(previous.pk), "outcome": STILL_OPEN}
        _audit(actor, "contact_entry.created", entry.pk, None, created)
        if previous is not None and closing:
            record_outcome(previous, closing, entry, actor=actor)
        superseded = _close_superseded(entry, actor=actor)
    if superseded:
        return AddOutcome(True, _superseded_message(superseded), entry=entry)
    return AddOutcome(True, ADDED, entry=entry)


def _promise_words(promise: ContactEntry) -> str:
    day = day_text(promise.promised_date) if promise.promised_date is not None else ""
    return f"{format_amount(promise.promised_amount or Decimal(0))} by {day}"


def _superseded_message(closed: list[ContactEntry]) -> str:
    if len(closed) == 1:
        earlier = f"the earlier promise of {_promise_words(closed[0])} is"
    else:
        earlier = "the earlier promises of " + ", ".join(_promise_words(row) for row in closed)
        earlier += " are"
    return f"Promise recorded; {earlier} closed as Rescheduled."


def _close_superseded(entry: ContactEntry, *, actor: User | None) -> list[ContactEntry]:
    """PLAN-012c-b D3: close every other open promise on the bucket of the new promise
    ``entry`` as ``rescheduled``, newest first; each audited with ``"auto": true``. The
    caller holds the operator's row lock. Returns them, newest first."""
    if entry.kind != ContactKind.PROMISE:
        return []
    candidates = list(
        ContactEntry.objects.select_for_update()
        .filter(
            _open_promise(),
            Q(lease__isnull=True) if entry.lease_id is None else Q(lease_id=entry.lease_id),
            operator_id=entry.operator_id,
        )
        .exclude(pk=entry.pk)
        .order_by("-created_at", "-id")
    )
    closer: ContactEntry | None = entry
    for promise in candidates:
        if closer is not None and ContactEntry.objects.filter(closed_by=closer).exists():
            closer = None
        record_outcome(
            promise, ContactStatus.RESCHEDULED, closer, actor=actor, extra={"auto": True}
        )
        closer = promise
    return candidates


def record_outcome(
    promise: ContactEntry,
    outcome: str,
    entry: ContactEntry | None,
    *,
    actor: User | None,
    extra: Mapping[str, object] | None = None,
) -> None:
    """Close the open ``promise`` with ``outcome`` (``kept``, ``rescheduled`` or
    ``broken``), recorded by the later ``entry`` (``None`` only for an automatic close
    whose entry already closes another promise); audited, with ``extra`` in the audit's
    after. The caller holds the promise's row lock and has checked that a rescheduling
    entry is a promise."""
    if outcome not in OUTCOMES or promise.status != ContactStatus.OPEN or promise.deleted_at:
        raise ValueError("record_outcome needs an open promise and a known outcome")
    before = _outcome_state(promise)
    promise.status = outcome
    promise.closed_by = entry
    promise.save(update_fields=["status", "closed_by"])
    _audit(
        actor,
        "contact_entry.outcome_recorded",
        promise.pk,
        before,
        {**_outcome_state(promise), **(extra or {})},
    )


def edit_entry(
    entry: ContactEntry, data: EditInput, *, actor: User | None, now: datetime | None = None
) -> AddOutcome:
    """Change an entry: only its author, only before :func:`edit_closes_at`, never a deleted
    one; the same rules as a new entry. A promise with an outcome keeps its kind, date and
    amount; an entry that rescheduled a promise stays a promise."""
    now = now or timezone.now()
    body = visible_text(data.body or "")
    with transaction.atomic():
        row = ContactEntry.objects.select_for_update().get(pk=entry.pk)
        if row.deleted_at is not None:
            return AddOutcome(False, DELETED)
        if actor is None or row.author_id != actor.pk:
            return AddOutcome(False, NOT_AUTHOR)
        if now >= edit_closes_at(row):
            return AddOutcome(False, WINDOW_CLOSED)
        refusal = _refusal(data.kind, body, data.promised_date, data.promised_amount)
        if refusal is not None:
            return AddOutcome(False, refusal[0], refusal[1])
        moved = (data.kind, data.promised_date, data.promised_amount) != (
            row.kind,
            row.promised_date,
            row.promised_amount,
        )
        if moved and row.status != ContactStatus.OPEN:
            return AddOutcome(False, PROMISE_CLOSED, "kind")
        rescheduled = ContactEntry.objects.filter(
            closed_by=row, status=ContactStatus.RESCHEDULED
        ).exists()
        if rescheduled and data.kind != ContactKind.PROMISE:
            return AddOutcome(False, RESCHEDULE_NEEDS_PROMISE, "kind")
        before = _state(row)
        row.kind = data.kind
        row.happened_on = data.happened_on
        row.promised_date = data.promised_date
        row.promised_amount = data.promised_amount
        row.body = body
        if _state(row) == before:
            return AddOutcome(True, UNCHANGED, entry=row)
        try:
            row.save(
                update_fields=["kind", "happened_on", "promised_date", "promised_amount", "body"]
            )
        except ValidationError:
            return AddOutcome(False, BODY_PAYMENT_DATA, "body")
        _audit(actor, "contact_entry.edited", row.pk, before, _state(row))
    return AddOutcome(True, EDITED, entry=row)


def _required_text(text: str, missing: str, field: str) -> tuple[str, AddOutcome | None]:
    text = visible_text(text or "")
    if not text:
        return text, AddOutcome(False, missing, field)
    if contains_card_number(text) or contains_bank_routing_pattern(text):
        return text, AddOutcome(False, BODY_PAYMENT_DATA, field)
    return text, None


def add_correction(entry: ContactEntry, body: str, *, actor: User | None) -> AddOutcome:
    """Append a correction to an entry not deleted; audited on the entry."""
    text, refused = _required_text(body, BODY_REQUIRED, "body")
    if refused is not None:
        return refused
    with transaction.atomic():
        row = ContactEntry.objects.select_for_update().get(pk=entry.pk)
        if row.deleted_at is not None:
            return AddOutcome(False, DELETED)
        before = {"corrections": row.corrections.count()}
        correction = ContactCorrection(entry=row, body=text, author=actor)
        try:
            correction.save()
        except ValidationError:
            return AddOutcome(False, BODY_PAYMENT_DATA, "body")
        _audit(
            actor,
            "contact_entry.corrected",
            row.pk,
            before,
            {"corrections": before["corrections"] + 1, "correction": str(correction.pk)}
            | {"body": text},
        )
    return AddOutcome(True, CORRECTION_ADDED, entry=row)


def delete_entry(entry: ContactEntry, reason: str, *, actor: User) -> AddOutcome:
    """Soft delete an entry with a reason (the caller checks that the actor is an admin).
    The row stays; a deleted promise counts for nothing from now on. An outcome the entry
    recorded on an earlier promise stands."""
    text, refused = _required_text(reason, REASON_REQUIRED, "reason")
    if refused is not None:
        return refused
    with transaction.atomic():
        row = ContactEntry.objects.select_for_update().get(pk=entry.pk)
        if row.deleted_at is not None:
            return AddOutcome(False, DELETED)
        before = {"deleted_at": None, "deleted_by": None, "delete_reason": ""}
        row.deleted_at = timezone.now()
        row.deleted_by = actor
        row.delete_reason = text
        try:
            row.save(update_fields=["deleted_at", "deleted_by", "delete_reason"])
        except ValidationError:
            return AddOutcome(False, BODY_PAYMENT_DATA, "reason")
        _audit(
            actor,
            "contact_entry.deleted",
            row.pk,
            before,
            {
                "deleted_at": row.deleted_at.isoformat(),
                "deleted_by": str(actor.pk),
                "delete_reason": text,
            },
        )
    return AddOutcome(True, ENTRY_DELETED, entry=row)


def _audit(
    actor: User | None,
    action: str,
    entry_id: uuid.UUID,
    before: Mapping[str, object] | None,
    after: Mapping[str, object],
) -> None:
    AuditLog.objects.create(
        actor=actor,
        action=action,
        entity_type=ENTITY_CONTACT,
        entity_id=str(entry_id),
        before=before,
        after=after,
    )


# --- Kept and broken promises (the module docstring's rules) -------------------------------


def _billed() -> Coalesce:
    """The operator a payment is billed to: its invoice's, else its charge's."""
    return Coalesce(
        "invoice__operator",
        "rent_charge__lease__operator",
        "insurance_charge__lease__operator",
        "one_off_charge__operator",
        "one_off_charge__lease__operator",
    )


def _paid_lease() -> Coalesce:
    """The lease of a payment's charge, directly or through its invoice."""
    return Coalesce(
        "rent_charge__lease",
        "insurance_charge__lease",
        "one_off_charge__lease",
        "invoice__rent_charge__lease",
        "invoice__insurance_charge__lease",
        "invoice__one_off_charge__lease",
    )


def _keeping_payments(payments: QuerySet[Payment], *, by_lease: bool) -> QuerySet[Payment]:
    """The payments of ``payments`` that keep the outer promise (the module docstring's
    rule): settled, not a refund, billed to its operator (and on its lease with
    ``by_lease``), at least its amount, settled from ``recorded_on`` to its date."""
    rows = (
        payments.filter(
            status=PaymentStatus.SETTLED,
            is_refund=False,
            amount__gte=OuterRef("promised_amount"),
            settled_on__gte=OuterRef("recorded_on"),
            settled_on__lte=OuterRef("promised_date"),
        )
        .annotate(billed=_billed())
        .filter(billed=OuterRef("operator_id"))
    )
    if by_lease:
        rows = rows.annotate(paid_lease=_paid_lease()).filter(paid_lease=OuterRef("lease_id"))
    return rows


def _open_promises(payments: QuerySet[Payment], *, kept: bool) -> QuerySet[ContactEntry]:
    """Open promises not deleted that a payment of ``payments`` keeps (``kept``), or that
    none keeps. ``created_at`` is truncated in the current time zone, New York
    (``settings.TIME_ZONE``)."""
    return (
        ContactEntry.objects.filter(_open_promise())
        .annotate(recorded_on=TruncDate("created_at"))
        .annotate(
            kept=Case(
                When(lease__isnull=True, then=Exists(_keeping_payments(payments, by_lease=False))),
                default=Exists(_keeping_payments(payments, by_lease=True)),
                output_field=BooleanField(),
            )
        )
        .filter(kept=kept)
    )


def broken_promises(today: date, operator: uuid.UUID | None = None) -> list[ContactEntry]:
    """The promises broken on ``today`` (the module docstring's rule), oldest promised
    date first; with ``operator``, only that operator's. At most one per (operator, lease)
    (PLAN-012c-b D3): the most recently recorded, even if the one-open-promise rule were
    ever bypassed. One query."""
    rows = (
        _open_promises(Payment.objects.all(), kept=False)
        .filter(promised_date__lt=today)
        .filter(**({"operator": operator} if operator is not None else {}))
        .select_related("operator", "lease__store")
        .order_by("-created_at", "-id")
    )
    newest: dict[tuple[uuid.UUID, uuid.UUID | None], ContactEntry] = {}
    for row in rows:
        newest.setdefault((row.operator_id, row.lease_id), row)
    return sorted(newest.values(), key=lambda row: (row.promised_date, row.created_at, row.pk))


def _first_keeper(promise: ContactEntry, payments: QuerySet[Payment]) -> Payment | None:
    """The earliest payment of ``payments`` that keeps ``promise``."""
    rows = (
        payments.filter(
            status=PaymentStatus.SETTLED,
            is_refund=False,
            amount__gte=promise.promised_amount,
            settled_on__gte=timezone.localdate(promise.created_at),
            settled_on__lte=promise.promised_date,
        )
        .annotate(billed=_billed())
        .filter(billed=promise.operator_id)
    )
    if promise.lease_id is not None:
        rows = rows.annotate(paid_lease=_paid_lease()).filter(paid_lease=promise.lease_id)
    return rows.order_by("settled_on", "created_at", "id").first()


def _close_kept(payments: QuerySet[Payment]) -> int:
    """Mark ``kept`` every open promise that a payment of ``payments`` keeps, naming the
    earliest such payment; each audited. Returns how many."""
    closed = 0
    with transaction.atomic():
        kept = list(_open_promises(payments, kept=True).values_list("pk", flat=True))
        promises = (
            ContactEntry.objects.select_for_update()
            .filter(_open_promise(), pk__in=kept)
            .order_by("created_at", "id")
        )
        for promise in promises:
            payment = _first_keeper(promise, payments)
            if payment is None:
                continue
            before = _outcome_state(promise)
            promise.status = ContactStatus.KEPT
            promise.kept_by_payment = payment
            promise.closed_by = None
            promise.save(update_fields=["status", "kept_by_payment", "closed_by"])
            _audit(
                None,
                "contact_entry.kept_by_payment",
                promise.pk,
                before,
                {**_outcome_state(promise), "settled_on": str(payment.settled_on)},
            )
            closed += 1
    return closed


def close_kept_by_payment(payment: Payment) -> int:
    """D4: the open promises that ``payment`` keeps become ``kept`` (the module
    docstring's rule). Called when a payment settles (the transaction handler and the
    manual payments); a payment that is not settled, or is a refund, keeps nothing.
    Idempotent: a kept promise is no longer open. Returns how many it closed."""
    if payment.status != PaymentStatus.SETTLED or payment.is_refund or payment.settled_on is None:
        return 0
    return _close_kept(Payment.objects.filter(pk=payment.pk))


def close_kept() -> int:
    """D4, the daily sweep (``collections_tick``): every open promise that a settled
    payment keeps becomes ``kept``, in case a settlement path did not close it.
    Idempotent. Returns how many it closed."""
    return _close_kept(Payment.objects.all())


# --- CSV ---------------------------------------------------------------------------------


def _corrections_text(entry: ContactEntry) -> str:
    return "\n".join(
        f"{timezone.localtime(row.created_at):%Y-%m-%d %H:%M} "
        f"{row.author.get_username() if row.author is not None else '-'}: {row.body}"
        for row in entry.corrections.all()
    )


def _csv_record(entry: ContactEntry) -> list[object]:
    return [
        entry.happened_on,
        entry.get_kind_display(),
        entry.lease.store.external_label if entry.lease is not None else "",
        entry.body,
        entry.promised_date,
        entry.promised_amount,
        entry.get_status_display() if entry.kind == ContactKind.PROMISE else "",
        closes_csv_text(closed) if (closed := closed_promise(entry)) is not None else "",
        entry.author.get_username() if entry.author is not None else "",
        entry.created_at,
        _corrections_text(entry),
        entry.deleted_at,
        entry.delete_reason,
    ]


def csv_lines(operator: Operator, *, include_deleted: bool) -> Iterator[str]:
    """Every entry of the operator, newest first, with readable headers; the deleted ones
    only with ``include_deleted`` (billing and admin; a viewer's export leaves them out)."""
    rows = entries(operator, include_deleted=include_deleted)
    return CsvWriter(CSV_HEADER).lines(
        _csv_record(entry) for entry in rows.iterator(chunk_size=500)
    )

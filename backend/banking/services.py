"""The Bank deposits module's actions
(docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md I2, I4).

**Off by default** (I2). :func:`readiness` says in one sentence why nothing is fetched: the
module is off (``CompanySettings.bank_deposits_enabled``), there is no bank source here (the
Plaid keys are missing outside local), or no bank account is connected. :func:`fetch` (the
daily job, after the re-debits, and "Fetch now") syncs only when it is ready, then refreshes
the suggestion of every new deposit and, when ``bank_auto_confirm_exact`` is on as well,
confirms the exact ones (audited ``bank_transaction.auto_confirmed``). A failed sync keeps the
error's class name on the connection and says so; nothing else of it is kept.

**Link** (admin): :func:`create_link_token` gives the browser a link token,
:func:`link` exchanges the browser's public token and stores the item with its access token
encrypted (:mod:`banking.crypto`; audited ``bank_connection.linked``), :func:`disconnect` clears
the token and ends the connection (audited ``bank_connection.disconnected``). The settings card's
two switches are saved by :func:`save_settings` (audited ``company_settings.updated``).

**Recording** (billing), each under the transaction's row lock, applied once (its state and the
unique ``allocation_group``):

- :func:`confirm`: through :func:`ledger.services.payments_entry.record_entry` for the store of
  the chosen lease: the method from the kind (wire, Zelle, check; an ACH credit or other is
  ``other`` with the kind in the note), ``reference`` the transaction's id, the note "Bank
  deposit matched: {description}", ``paid_on`` the posted date, the creation key from the
  transaction's id; one charge (a partial payment when the deposit is less) or the lease's
  whole open balance; what is left over kept as a credit on the lease only when asked. When
  asked, the sender is remembered as an alias of the lease. Audited ``bank_transaction.confirmed``.
- :func:`split`: several charges of one or more stores (several checks in one deposit), the
  amounts adding up to the deposit's (or the rest kept as a credit on the first store's lease):
  one entry per store, every entry in the deposit's one allocation group. Audited
  ``bank_transaction.confirmed`` with ``split``.
- :func:`ignore`: with a reason (an internal transfer, not rent, or other with words). Audited
  ``bank_transaction.ignored``.
- :func:`undo`: a recorded deposit's payments are voided through
  :func:`ledger.services.refunds.void_manual_payment`, one per line, with the reason "Bank
  deposit match undone", and the deposit is new again; an ignored one is new again. Refused,
  with nothing changed, when the deposit kept a credit on a lease or paid a charge whose
  payment cannot be voided by hand (an insurance charge). Audited ``bank_transaction.undone``.

**Sender names** (billing; the lease's billing details dialog): :func:`add_alias` remembers a
name the bank shows for the lease's deposits (one line, compared upper-cased with its
whitespace collapsed; at least three characters; no card or account number; once per lease),
:func:`remove_alias` forgets one. Audited ``sender_alias.added`` and ``sender_alias.removed``.

Every refusal is one sentence and writes nothing. Money is ``Decimal`` throughout; nothing here
calls the payment processor.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.roles import Role, meets, role_of
from banking import crypto, matching
from banking.models import (
    ALIAS_MAX_LENGTH,
    APPLIED_STATUSES,
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
from banking.sources import (
    FAKE_TOKEN_MARKER,
    MONEY_MAX,
    BankTransactionSource,
    LinkRefusedError,
    SyncResult,
    clean_text,
    get_source,
    keys_present,
)
from ledger.models import (
    ZERO,
    AuditLog,
    CompanySettings,
    Lease,
    LeaseCredit,
    Payment,
    PaymentMethod,
    PaymentStatus,
)
from ledger.services import payments_entry, refunds
from ledger.validators import validate_no_payment_data, visible_text
from ledger.wording import format_amount

logger = logging.getLogger(__name__)

ENTITY_TRANSACTION = "bank_transaction"
ENTITY_CONNECTION = "bank_connection"
ENTITY_SETTINGS = "company_settings"
CONFIRMED_ACTION = "bank_transaction.confirmed"
AUTO_CONFIRMED_ACTION = "bank_transaction.auto_confirmed"
IGNORED_ACTION = "bank_transaction.ignored"
UNDONE_ACTION = "bank_transaction.undone"
ENTITY_ALIAS = "sender_alias"
ALIAS_ADDED_ACTION = "sender_alias.added"
ALIAS_REMOVED_ACTION = "sender_alias.removed"
LINKED_ACTION = "bank_connection.linked"
DISCONNECTED_ACTION = "bank_connection.disconnected"
SETTINGS_FIELDS: tuple[str, ...] = ("bank_deposits_enabled", "bank_auto_confirm_exact")
UNDO_REASON = "Bank deposit match undone"
IGNORE_NOTE_MAX_LENGTH = 500
#: An alias shorter than this would match nearly every deposit.
ALIAS_MIN_LENGTH = 3
PUBLIC_TOKEN_MAX_LENGTH = 200

# --- Sentences ------------------------------------------------------------------------------

KEYS_PRESENT = "Plaid keys: present in this environment."
KEYS_MISSING = "Plaid keys: missing in this environment."
OFF = (
    "Bank deposits are turned off: an admin turns them on in Admin › Settings › Bank deposits. "
    "Nothing is fetched from the bank."
)
NO_SOURCE = "The Plaid keys are missing in this environment: nothing is fetched from the bank."
NO_CONNECTION = (
    "No bank account is connected: an admin connects one in Admin › Settings › Bank deposits."
)
NOT_ALLOWED = "Your role cannot do this. Nothing was changed."
NOT_FOUND = "This deposit no longer exists. Nothing was changed."
ALREADY = "This deposit was already recorded or ignored. Nothing was changed."
NOT_APPLIED = "Only a recorded or ignored deposit can be undone. Nothing was changed."
NO_LEASE = "Choose the store's lease the deposit pays. Nothing was recorded."
NOT_OPEN = (
    "That charge is not an open charge of the store's current lease any more. Nothing was recorded."
)
NOTHING_OPEN = "The lease owes nothing now. Nothing was recorded."
LEFT_OVER = (
    "The deposit of {amount} is more than the {paid} it pays: keep the rest as a credit on the "
    "lease, or choose another charge. Nothing was recorded."
)
SPLIT_EMPTY = "Enter what the deposit pays on at least one charge. Nothing was recorded."
SPLIT_AMOUNT = "Each amount must be more than zero with at most two decimals. Nothing was recorded."
SPLIT_OVER = (
    "The amounts add up to {allocated}, more than the deposit of {amount}. Nothing was recorded."
)
SPLIT_SHORT = (
    "The amounts add up to {allocated}, not the deposit of {amount}: allocate the rest or keep "
    "it as a credit on the first store's lease. Nothing was recorded."
)
SPLIT_NOT_OPEN = "A charge of the split is not an open charge of its store. Nothing was recorded."
CONFIRMED = "Deposit of {amount} recorded for {store}."
SPLIT_DONE = "Deposit of {amount} recorded over {count} charge(s)."
ALIAS_SAVED = " {text} is remembered for {store}."
ALIAS_REFUSED = " The sender was not remembered (it holds a number shaped like an account number)."
ALIAS_TOO_SHORT = (
    f"Enter a sender name of at least {ALIAS_MIN_LENGTH} characters. Nothing was saved."
)
ALIAS_TOO_LONG = f"Keep the sender name under {ALIAS_MAX_LENGTH} characters. Nothing was saved."
ALIAS_PAYMENT_DATA = "A sender name may not contain card or account numbers. Nothing was saved."
ALIAS_DUPLICATE = "{text} is already a sender name of this lease. Nothing was saved."
ALIAS_ADDED = "{text} is remembered for {store}."
ALIAS_REMOVED = "{text} is no longer a sender name of {store}."
ALIAS_GONE = "That sender name was already removed. Nothing was changed."
REASON_REQUIRED = "Choose why the deposit is ignored. Nothing was changed."
NOTE_REQUIRED = "Say why the deposit is ignored. Nothing was changed."
NOTE_TOO_LONG = f"Keep the reason under {IGNORE_NOTE_MAX_LENGTH} characters. Nothing was changed."
PAYMENT_DATA = "This text may not contain card or account numbers. Nothing was changed."
IGNORED = "Deposit of {amount} ignored."
UNDONE = "Deposit of {amount} is new again."
UNDO_CREDIT = (
    "This deposit kept a credit on a lease, which cannot be taken back here. Nothing was changed."
)
UNDO_REFUSED = "{description}: {message} Nothing was changed."
FETCHED = "Fetched from the bank: {added} new deposit(s)."
FETCH_FAILED = "The bank could not be read ({error}). Nothing new was fetched."
AUTO = " {count} confirmed automatically (exact matches)."
#: The daily job's line (``collections_tick``).
TICK_FETCHED = "Bank deposits fetched: {fetched} (new {added})."
TICK_AUTO = " Bank deposits confirmed automatically: {count}."
TICK_OFF = "Bank deposits: off. {message}"
TICK_FAILED = "Bank deposits: not fetched. {message}"
LINK_OFF = "Turn bank deposits on before connecting a bank account. Nothing was connected."
LINK_NO_KEYS = "The Plaid keys are missing in this environment. Nothing was connected."
LINK_EXISTS = "A bank account is already connected: disconnect it first. Nothing was connected."
LINK_BAD_TOKEN = "Plaid's window did not finish. Nothing was connected."  # noqa: S105
LINK_FAILED = "Plaid could not connect the account ({error}). Nothing was connected."
LINKED = "Bank account connected: {connection}."
DISCONNECTED = "Bank account disconnected: {connection}."
NOT_CONNECTED = "No bank account is connected. Nothing was changed."
SETTINGS_SAVED = "Bank deposit settings saved."
UNCHANGED = "Nothing changed."

#: The payment method a kind is recorded with (I4).
METHODS: dict[str, str] = {
    Kind.WIRE: PaymentMethod.WIRE,
    Kind.ZELLE: PaymentMethod.ZELLE,
    Kind.CHECK: PaymentMethod.CHECK,
    Kind.ACH_CREDIT: PaymentMethod.OTHER,
    Kind.OTHER: PaymentMethod.OTHER,
}


@dataclass(frozen=True, slots=True)
class Outcome:
    ok: bool
    message: str
    field: str | None = None


@dataclass(frozen=True, slots=True)
class Choice:
    """What a Confirm records: a lease, and one of its open charges or (``charge_id``
    ``None``) its whole open balance."""

    lease: uuid.UUID
    charge_type: str = ""
    charge_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class SplitLine:
    store: uuid.UUID
    charge_id: uuid.UUID
    amount: Decimal


class _Refused(Exception):  # noqa: N818 (reads as an outcome)
    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.field = field


def _billing(actor: User | None) -> bool:
    return actor is not None and meets(role_of(actor), Role.BILLING)


def _admin(actor: User | None) -> bool:
    return actor is not None and meets(role_of(actor), Role.ADMIN)


def _audit(
    actor: User | None,
    action: str,
    entity_type: str,
    entity_id: object,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> None:
    AuditLog.objects.create(
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        before=before,
        after=after,
    )


# --- State ------------------------------------------------------------------------------------


def active_connection() -> BankConnection | None:
    return BankConnection.objects.filter(active=True).first()


def readiness(
    company: CompanySettings | None = None, connection: BankConnection | None = None
) -> str:
    """Why nothing is fetched, in one sentence; ``""`` when the module is ready."""
    company = company or CompanySettings.read()
    if not company.bank_deposits_enabled:
        return OFF
    if get_source() is None:
        return NO_SOURCE
    if (connection or active_connection()) is None:
        return NO_CONNECTION
    return ""


def card() -> dict[str, Any]:
    """What Admin › Settings › Bank deposits shows (names only, never a value)."""
    company = CompanySettings.read()
    connection = active_connection()
    source = get_source()
    return {
        "keys": KEYS_PRESENT if keys_present() else KEYS_MISSING,
        "enabled": company.bank_deposits_enabled,
        "auto_confirm": company.bank_auto_confirm_exact,
        "connection": connection,
        "can_connect": company.bank_deposits_enabled and source is not None and connection is None,
        "fake": source is not None and source.source == Source.FAKE,
        "readiness": readiness(company, connection),
    }


def save_settings(data: Mapping[str, object], actor: User | None) -> Outcome:
    """The card's two switches (admin): a checkbox is on when it is posted."""
    if not _admin(actor):
        return Outcome(False, NOT_ALLOWED)
    found = {name: bool(data.get(name)) for name in SETTINGS_FIELDS}
    with transaction.atomic():
        row = CompanySettings.current()
        row = CompanySettings.objects.select_for_update().get(pk=row.pk)
        before = {name: getattr(row, name) for name in SETTINGS_FIELDS}
        if before == found:
            return Outcome(True, UNCHANGED)
        for name, value in found.items():
            setattr(row, name, value)
        row.save(update_fields=[*SETTINGS_FIELDS, "updated_at"])
        _audit(actor, f"{ENTITY_SETTINGS}.updated", ENTITY_SETTINGS, row.pk, before, found)
    return Outcome(True, SETTINGS_SAVED)


# --- Link (admin) -----------------------------------------------------------------------------


def _link_refusal(actor: User | None) -> tuple[str, BankTransactionSource | None]:
    if not _admin(actor):
        return NOT_ALLOWED, None
    if not CompanySettings.read().bank_deposits_enabled:
        return LINK_OFF, None
    source = get_source()
    if source is None:
        return LINK_NO_KEYS, None
    if active_connection() is not None:
        return LINK_EXISTS, None
    return "", source


def create_link_token(actor: User | None) -> tuple[Outcome, str]:
    """A link token for the browser's Link (admin); ``""`` with the refusal otherwise."""
    refusal, source = _link_refusal(actor)
    if source is None or actor is None:
        return Outcome(False, refusal), ""
    from banking.plaid.client import PlaidError

    try:
        token = source.create_link_token(str(actor.pk), str(CompanySettings.read().short_name))
    except PlaidError as error:
        return Outcome(False, LINK_FAILED.format(error=type(error).__name__)), ""
    return Outcome(True, ""), token


def link(actor: User | None, public_token: str, account_id: str, institution: str) -> Outcome:
    """Exchange the browser's public token and keep the item (admin; see the module
    docstring). The public token and the account id are identifiers; the institution's name
    is text the browser sent, cut and guarded."""
    refusal, source = _link_refusal(actor)
    if source is None:
        return Outcome(False, refusal)
    if not public_token or len(public_token) > PUBLIC_TOKEN_MAX_LENGTH:
        return Outcome(False, LINK_BAD_TOKEN)
    from banking.plaid.client import PlaidError

    name, _ = clean_text(institution, 200)
    try:
        linked = source.exchange(public_token, account_id[:128], name)
    except LinkRefusedError as refused:
        return Outcome(False, refused.message)
    except PlaidError as error:
        return Outcome(False, LINK_FAILED.format(error=type(error).__name__))
    stored = (
        FAKE_TOKEN_MARKER if source.source == Source.FAKE else crypto.encrypt(linked.access_token)
    )
    institution_name, _ = clean_text(linked.institution, 200)
    try:
        with transaction.atomic():
            connection = BankConnection.objects.create(
                item_id=linked.item_id,
                account_id=linked.account_id,
                institution=institution_name,
                mask=linked.mask,
                access_token_encrypted=stored,
                source=source.source,
                linked_by=actor,
                linked_at=timezone.now(),
            )
            _audit(
                actor,
                LINKED_ACTION,
                ENTITY_CONNECTION,
                connection.pk,
                None,
                {"institution": connection.institution, "mask": connection.mask},
            )
    except IntegrityError:
        return Outcome(False, LINK_EXISTS)
    return Outcome(True, LINKED.format(connection=connection))


def disconnect(actor: User | None) -> Outcome:
    """End the active connection and clear its token (admin)."""
    if not _admin(actor):
        return Outcome(False, NOT_ALLOWED)
    with transaction.atomic():
        connection = BankConnection.objects.select_for_update().filter(active=True).first()
        if connection is None:
            return Outcome(False, NOT_CONNECTED)
        connection.active = False
        connection.access_token_encrypted = ""
        connection.disconnected_at = timezone.now()
        connection.save(update_fields=["active", "access_token_encrypted", "disconnected_at"])
        _audit(
            actor,
            DISCONNECTED_ACTION,
            ENTITY_CONNECTION,
            connection.pk,
            {"institution": connection.institution, "mask": connection.mask},
            None,
        )
    return Outcome(True, DISCONNECTED.format(connection=connection))


# --- Fetch ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FetchReport:
    ok: bool
    message: str
    result: SyncResult | None = None
    auto_confirmed: int = 0
    #: The module was ready and the bank could not be read.
    failed: bool = False


def fetch(today: date, actor: User | None = None) -> FetchReport:
    """Sync the bank when the module is ready, refresh the new deposits' suggestions and,
    when asked, confirm the exact ones. "Fetch now" passes its staff user (billing)."""
    if actor is not None and not _billing(actor):
        return FetchReport(False, NOT_ALLOWED)
    company = CompanySettings.read()
    connection = active_connection()
    why = readiness(company, connection)
    if why:
        return FetchReport(False, why)
    source = get_source()
    if source is None or connection is None:
        return FetchReport(False, NO_SOURCE)
    from banking.plaid.client import PlaidError

    try:
        result = source.sync(connection)
    except (PlaidError, crypto.TokenUnreadableError, crypto.TokenKeyMissingError) as error:
        name = type(error).__name__
        BankConnection.objects.filter(pk=connection.pk).update(last_error=name)
        logger.warning("Bank sync failed: %s", name)
        return FetchReport(False, FETCH_FAILED.format(error=name), failed=True)
    refresh_suggestions(today)
    auto = auto_confirm(today) if company.bank_auto_confirm_exact else 0
    message = FETCHED.format(added=result.added)
    if auto:
        message += AUTO.format(count=auto)
    return FetchReport(True, message, result, auto)


def tick_line(report: FetchReport) -> str:
    """The daily job's part of its one line: :data:`TICK_FETCHED` (the credits the bank gave,
    and how many of them are new) after a sync, :data:`TICK_FAILED` with the sentence after a
    failed one, else :data:`TICK_OFF` with why nothing was fetched."""
    if report.ok and report.result is not None:
        result = report.result
        line = TICK_FETCHED.format(fetched=result.added + result.already, added=result.added)
        if report.auto_confirmed:
            line += TICK_AUTO.format(count=report.auto_confirmed)
        return line
    if report.failed:
        return TICK_FAILED.format(message=report.message)
    return TICK_OFF.format(message=report.message)


def refresh_suggestions(today: date) -> None:
    """Store the suggestion of every new deposit, read once over what is owed today."""
    rows = list(BankTransaction.objects.filter(status=Status.NEW))
    found = matching.suggest_all(rows, today)
    changed = []
    for txn in rows:
        suggestion = found[txn.pk]
        values = (
            suggestion.confidence,
            suggestion.lease,
            suggestion.charge_type,
            suggestion.charge_id,
        )
        if values != (
            txn.confidence,
            txn.suggested_lease_id,
            txn.suggested_charge_type,
            txn.suggested_charge_id,
        ):
            txn.confidence = suggestion.confidence
            txn.suggested_lease_id = suggestion.lease
            txn.suggested_charge_type = suggestion.charge_type if suggestion.charge_id else ""
            txn.suggested_charge_id = suggestion.charge_id
            changed.append(txn)
    if changed:
        BankTransaction.objects.bulk_update(
            changed,
            ["confidence", "suggested_lease", "suggested_charge_type", "suggested_charge_id"],
        )


def auto_confirm(today: date) -> int:
    """Confirm each new deposit whose stored suggestion is exact (no staff user)."""
    count = 0
    exact = BankTransaction.objects.filter(
        status=Status.NEW, confidence=Confidence.EXACT, suggested_lease__isnull=False
    ).values_list("pk", "suggested_lease", "suggested_charge_type", "suggested_charge_id")
    for pk, lease, kind, charge in list(exact):
        outcome = _confirm(
            pk,
            Choice(lease, kind, charge),
            actor=None,
            keep_credit=False,
            remember=False,
            today=today,
            action=AUTO_CONFIRMED_ACTION,
        )
        count += int(outcome.ok)
    return count


# --- Recording (billing) ----------------------------------------------------------------------


def _locked(pk: uuid.UUID) -> BankTransaction:
    txn = BankTransaction.objects.select_for_update().filter(pk=pk).first()
    if txn is None:
        raise _Refused(NOT_FOUND)
    return txn


def _attempt(txn: BankTransaction) -> int:
    """How many times this deposit was undone: a new creation key for each new recording."""
    return AuditLog.objects.filter(
        action=UNDONE_ACTION, entity_type=ENTITY_TRANSACTION, entity_id=str(txn.pk)
    ).count()


def entry_key(txn: BankTransaction, attempt: int, store: uuid.UUID) -> uuid.UUID:
    """The payment entry's creation key: from the transaction's id (I4)."""
    return uuid.uuid5(txn.pk, f"entry-{attempt}-{store}")


def note_for(txn: BankTransaction) -> str:
    text = txn.description or txn.counterparty or "no description"
    note = f"Bank deposit matched: {text}"
    if METHODS[txn.kind] == PaymentMethod.OTHER:
        note += f" ({Kind(txn.kind).label})"
    return note


def store_title(lease: Lease) -> str:
    store = lease.store
    number = f"#{store.store_number} " if store.store_number is not None else ""
    return f"{number}{store.external_label}"


def _entry(
    txn: BankTransaction,
    lease: Lease,
    allocations: Mapping[uuid.UUID, Decimal],
    amount: Decimal,
    *,
    keep_credit: bool,
    group: uuid.UUID,
    attempt: int,
    actor: User | None,
    today: date,
) -> payments_entry.EntryOutcome:
    outcome = payments_entry.record_entry(
        lease.store,
        payments_entry.EntryInput(
            creation_key=entry_key(txn, attempt, lease.store_id),
            method=METHODS[txn.kind],
            amount=amount,
            paid_on=txn.posted_on,
            reference=str(txn.pk),
            note=note_for(txn),
            allocations=allocations,
            keep_credit=keep_credit,
            allocation_group=group,
        ),
        actor=actor,
        today=today,
    )
    if not outcome.ok or outcome.replayed:
        raise _Refused(outcome.message, outcome.field)
    return outcome


def _allocations(
    txn: BankTransaction, lease: Lease, choice: Choice, keep_credit: bool, today: date
) -> dict[uuid.UUID, Decimal]:
    listed = [
        charge
        for charge in payments_entry.open_charges(lease, today)
        if charge.kind_label != "Deposit"
    ]
    if choice.charge_id is not None:
        chosen = [
            charge
            for charge in listed
            if charge.charge_id == choice.charge_id
            and (not choice.charge_type or charge.kind == choice.charge_type)
        ]
        if not chosen:
            raise _Refused(NOT_OPEN)
        listed = chosen
    if not listed:
        raise _Refused(NOTHING_OPEN)
    allocations: dict[uuid.UUID, Decimal] = {}
    left = Decimal(txn.amount)
    for charge in listed:
        part = min(left, charge.balance)
        if part > 0:
            allocations[charge.charge_id] = part
            left -= part
    if left > 0 and not keep_credit:
        paid = Decimal(txn.amount) - left
        raise _Refused(LEFT_OVER.format(amount=format_amount(txn.amount), paid=format_amount(paid)))
    return allocations


def _remember(txn: BankTransaction, lease: Lease, actor: User | None) -> str:
    """Save the sender as an alias of ``lease``; the sentence to add to the outcome."""
    text = " ".join(txn.counterparty.split())[:ALIAS_MAX_LENGTH]
    normalised = normalise(text)
    if len(normalised) < ALIAS_MIN_LENGTH:
        return ""
    if SenderAlias.objects.filter(lease=lease, normalised=normalised).exists():
        return ""
    try:
        with transaction.atomic():
            SenderAlias.objects.create(
                lease=lease, text=text, normalised=normalised, created_by=actor
            )
    except ValidationError:
        return ALIAS_REFUSED
    except IntegrityError:
        return ""
    return ALIAS_SAVED.format(text=text, store=store_title(lease))


def alias_offer(
    txn: BankTransaction, lease_id: uuid.UUID | None, known: set[tuple[Any, str]]
) -> str:
    """The sender's text when Confirm should offer to remember it for the lease, else ``""``."""
    text = " ".join(txn.counterparty.split())
    if lease_id is None or len(normalise(text)) < ALIAS_MIN_LENGTH:
        return ""
    return "" if (lease_id, normalise(text)) in known else text[:ALIAS_MAX_LENGTH]


# --- Sender names on the lease's billing details (I4) ---------------------------------------


def aliases_of(lease: Lease) -> list[SenderAlias]:
    """The lease's sender names, as the billing details dialog lists them."""
    return list(SenderAlias.objects.filter(lease=lease).order_by("normalised"))


def add_alias(lease: Lease, raw: object, *, actor: User | None) -> Outcome:
    """Remember ``raw`` as a sender name of ``lease`` (billing): one line, its whitespace
    collapsed, compared as :func:`banking.models.normalise`; refused with nothing written when
    it is too short, too long, holds payment data or is already one of the lease's. Audited
    ``sender_alias.added``."""
    if not _billing(actor):
        return Outcome(False, NOT_ALLOWED)
    text = " ".join(visible_text(raw if isinstance(raw, str) else "").split())
    normalised = normalise(text)
    if len(normalised) < ALIAS_MIN_LENGTH:
        return Outcome(False, ALIAS_TOO_SHORT, "text")
    if len(text) > ALIAS_MAX_LENGTH:
        return Outcome(False, ALIAS_TOO_LONG, "text")
    try:
        validate_no_payment_data(text)
    except ValidationError:
        return Outcome(False, ALIAS_PAYMENT_DATA, "text")
    duplicate = Outcome(False, ALIAS_DUPLICATE.format(text=text), "text")
    if SenderAlias.objects.filter(lease=lease, normalised=normalised).exists():
        return duplicate
    try:
        with transaction.atomic():
            alias = SenderAlias.objects.create(
                lease=lease, text=text, normalised=normalised, created_by=actor
            )
            _audit(
                actor,
                ALIAS_ADDED_ACTION,
                ENTITY_ALIAS,
                alias.pk,
                None,
                {"lease": str(lease.pk), "text": text, "normalised": normalised},
            )
    except IntegrityError:  # added by someone else meanwhile
        return duplicate
    return Outcome(True, ALIAS_ADDED.format(text=text, store=store_title(lease)))


def remove_alias(lease: Lease, alias_id: uuid.UUID, *, actor: User | None) -> Outcome:
    """Forget one sender name of ``lease`` (billing). Audited ``sender_alias.removed``."""
    if not _billing(actor):
        return Outcome(False, NOT_ALLOWED)
    with transaction.atomic():
        alias = SenderAlias.objects.select_for_update().filter(pk=alias_id, lease=lease).first()
        if alias is None:
            return Outcome(False, ALIAS_GONE)
        before = {"lease": str(lease.pk), "text": alias.text, "normalised": alias.normalised}
        alias.delete()
        _audit(actor, ALIAS_REMOVED_ACTION, ENTITY_ALIAS, alias_id, before, None)
    return Outcome(True, ALIAS_REMOVED.format(text=before["text"], store=store_title(lease)))


def confirm(
    pk: uuid.UUID,
    choice: Choice,
    *,
    actor: User | None,
    keep_credit: bool = False,
    remember: bool = False,
    today: date | None = None,
) -> Outcome:
    """Record the deposit on the chosen charge or lease (billing; see the module docstring)."""
    if not _billing(actor):
        return Outcome(False, NOT_ALLOWED)
    return _confirm(
        pk,
        choice,
        actor=actor,
        keep_credit=keep_credit,
        remember=remember,
        today=today or timezone.localdate(),
        action=CONFIRMED_ACTION,
    )


def _confirm(
    pk: uuid.UUID,
    choice: Choice,
    *,
    actor: User | None,
    keep_credit: bool,
    remember: bool,
    today: date,
    action: str,
) -> Outcome:
    try:
        with transaction.atomic():
            txn = _locked(pk)
            if txn.status != Status.NEW:
                raise _Refused(ALREADY)
            lease = Lease.objects.select_related("store").filter(pk=choice.lease).first()
            if lease is None:
                raise _Refused(NO_LEASE)
            allocations = _allocations(txn, lease, choice, keep_credit, today)
            group = uuid.uuid4()
            attempt = _attempt(txn)
            outcome = _entry(
                txn,
                lease,
                allocations,
                Decimal(txn.amount),
                keep_credit=keep_credit,
                group=group,
                attempt=attempt,
                actor=actor,
                today=today,
            )
            _applied(txn, Status.CONFIRMED, group, actor)
            _audit(
                actor,
                action,
                ENTITY_TRANSACTION,
                txn.pk,
                {"status": Status.NEW.value},
                {
                    "status": Status.CONFIRMED.value,
                    "allocation_group": str(group),
                    "lease": str(lease.pk),
                    "creation_key": str(entry_key(txn, attempt, lease.store_id)),
                    "payments": [str(payment.pk) for payment in outcome.payments],
                    "credit_kept": str(outcome.credit.pk) if outcome.credit else None,
                },
            )
            extra = _remember(txn, lease, actor) if remember else ""
    except _Refused as refused:
        return Outcome(False, refused.message, refused.field)
    return Outcome(
        True, CONFIRMED.format(amount=format_amount(txn.amount), store=store_title(lease)) + extra
    )


def _applied(txn: BankTransaction, status: str, group: uuid.UUID, actor: User | None) -> None:
    txn.status = status
    txn.allocation_group = group
    txn.confirmed_by = actor
    txn.confirmed_at = timezone.now()
    txn.save(
        update_fields=["status", "allocation_group", "confirmed_by", "confirmed_at", "updated_at"]
    )


def parse_amount(raw: object) -> Decimal | None:
    """A posted amount: ``None`` when empty; ``ValueError`` when it is not an amount above zero
    in cents."""
    text = raw.strip() if isinstance(raw, str) else ""
    if not text:
        return None
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise ValueError(SPLIT_AMOUNT) from None
    if not _is_cents(amount):
        raise ValueError(SPLIT_AMOUNT)
    return amount


def _is_cents(amount: Decimal) -> bool:
    """Whether ``amount`` is finite, above zero, at most :data:`MONEY_MAX` and in whole cents
    (checked in that order, so that ``1e400`` never reaches the quantize)."""
    return (
        amount.is_finite()
        and ZERO < amount <= MONEY_MAX
        and amount == amount.quantize(Decimal("0.01"))
    )


def split(
    pk: uuid.UUID,
    lines: Sequence[SplitLine],
    *,
    actor: User | None,
    keep_credit: bool = False,
    today: date | None = None,
) -> Outcome:
    """Record the deposit over several charges of one or more stores (billing)."""
    if not _billing(actor):
        return Outcome(False, NOT_ALLOWED)
    day = today or timezone.localdate()
    try:
        with transaction.atomic():
            txn = _locked(pk)
            if txn.status != Status.NEW:
                raise _Refused(ALREADY)
            outcomes = _split(txn, lines, actor=actor, keep_credit=keep_credit, today=day)
    except _Refused as refused:
        return Outcome(False, refused.message, refused.field)
    count = sum(len(outcome.payments) for outcome in outcomes)
    return Outcome(True, SPLIT_DONE.format(amount=format_amount(txn.amount), count=count))


def _split(
    txn: BankTransaction,
    lines: Sequence[SplitLine],
    *,
    actor: User | None,
    keep_credit: bool,
    today: date,
) -> list[payments_entry.EntryOutcome]:
    if not lines:
        raise _Refused(SPLIT_EMPTY)
    if not all(_is_cents(line.amount) for line in lines):
        raise _Refused(SPLIT_AMOUNT)
    amount = Decimal(txn.amount)
    allocated = sum((line.amount for line in lines), ZERO)
    if allocated > amount:
        raise _Refused(
            SPLIT_OVER.format(allocated=format_amount(allocated), amount=format_amount(amount))
        )
    remainder = amount - allocated
    if remainder > 0 and not keep_credit:
        raise _Refused(
            SPLIT_SHORT.format(allocated=format_amount(allocated), amount=format_amount(amount))
        )
    by_store: dict[uuid.UUID, dict[uuid.UUID, Decimal]] = {}
    for line in lines:
        charges = by_store.setdefault(line.store, {})
        charges[line.charge_id] = charges.get(line.charge_id, ZERO) + line.amount
    group = uuid.uuid4()
    attempt = _attempt(txn)
    outcomes = []
    for position, (store, charges) in enumerate(by_store.items()):
        lease = _current_lease(store, today)
        listed = {charge.charge_id for charge in payments_entry.open_charges(lease, today)}
        if set(charges) - listed:
            raise _Refused(SPLIT_NOT_OPEN)
        kept = remainder if position == 0 else ZERO
        outcomes.append(
            _entry(
                txn,
                lease,
                charges,
                sum(charges.values(), ZERO) + kept,
                keep_credit=kept > 0,
                group=group,
                attempt=attempt,
                actor=actor,
                today=today,
            )
        )
    _applied(txn, Status.SPLIT, group, actor)
    _audit(
        actor,
        CONFIRMED_ACTION,
        ENTITY_TRANSACTION,
        txn.pk,
        {"status": Status.NEW.value},
        {
            "status": Status.SPLIT.value,
            "split": True,
            "allocation_group": str(group),
            "stores": [str(store) for store in by_store],
            "payments": [str(payment.pk) for outcome in outcomes for payment in outcome.payments],
            "credit_kept": str(remainder) if remainder > 0 else None,
        },
    )
    return outcomes


def _current_lease(store: uuid.UUID, today: date) -> Lease:
    from ledger.models import Store

    found = Store.objects.filter(pk=store).first()
    lease = payments_entry.current_lease(found, today) if found is not None else None
    if lease is None:
        raise _Refused(SPLIT_NOT_OPEN)
    return Lease.objects.select_related("store").get(pk=lease.pk)


def ignore(pk: uuid.UUID, reason: str, note: str, *, actor: User | None) -> Outcome:
    """Set a new deposit aside with a reason (billing)."""
    if not _billing(actor):
        return Outcome(False, NOT_ALLOWED)
    if reason not in IgnoreReason.values:
        return Outcome(False, REASON_REQUIRED, "reason")
    text = visible_text(note or "")
    if reason == IgnoreReason.OTHER and not text:
        return Outcome(False, NOTE_REQUIRED, "note")
    if len(text) > IGNORE_NOTE_MAX_LENGTH:
        return Outcome(False, NOTE_TOO_LONG, "note")
    try:
        validate_no_payment_data(text)
    except ValidationError:
        return Outcome(False, PAYMENT_DATA, "note")
    try:
        with transaction.atomic():
            txn = _locked(pk)
            if txn.status != Status.NEW:
                raise _Refused(ALREADY)
            txn.status = Status.IGNORED
            txn.ignored_by = actor
            txn.ignored_at = timezone.now()
            txn.ignore_reason = reason
            txn.ignore_note = text
            txn.save(
                update_fields=[
                    "status",
                    "ignored_by",
                    "ignored_at",
                    "ignore_reason",
                    "ignore_note",
                    "updated_at",
                ]
            )
            _audit(
                actor,
                IGNORED_ACTION,
                ENTITY_TRANSACTION,
                txn.pk,
                {"status": Status.NEW.value},
                {"status": Status.IGNORED.value, "reason": reason, "note": text},
            )
    except _Refused as refused:
        return Outcome(False, refused.message)
    return Outcome(True, IGNORED.format(amount=format_amount(txn.amount)))


def undo(pk: uuid.UUID, *, actor: User | None, today: date | None = None) -> Outcome:
    """Make a recorded or ignored deposit new again (billing; see the module docstring)."""
    if not _billing(actor):
        return Outcome(False, NOT_ALLOWED)
    try:
        with transaction.atomic():
            txn = _locked(pk)
            before: dict[str, Any] = {"status": txn.status}
            voided: list[str] = []
            if txn.status == Status.IGNORED:
                before |= {"reason": txn.ignore_reason, "note": txn.ignore_note}
            elif txn.status in APPLIED_STATUSES:
                before["allocation_group"] = str(txn.allocation_group)
                voided = _void_lines(txn, actor)
            else:
                raise _Refused(NOT_APPLIED)
            attempt = _attempt(txn)
            txn.status = Status.NEW
            txn.allocation_group = None
            txn.confirmed_by = None
            txn.confirmed_at = None
            txn.ignored_by = None
            txn.ignored_at = None
            txn.ignore_reason = ""
            txn.ignore_note = ""
            txn.undone_at = timezone.now()
            txn.save()
            _audit(
                actor,
                UNDONE_ACTION,
                ENTITY_TRANSACTION,
                txn.pk,
                before,
                {"status": Status.NEW.value, "voided": voided, "attempt": attempt + 1},
            )
    except _Refused as refused:
        return Outcome(False, refused.message)
    refresh_suggestions(today or timezone.localdate())
    return Outcome(True, UNDONE.format(amount=format_amount(txn.amount)))


def _void_lines(txn: BankTransaction, actor: User | None) -> list[str]:
    group = txn.allocation_group
    if group is None:
        return []
    if LeaseCredit.objects.filter(allocation_group=group).exists():
        raise _Refused(UNDO_CREDIT)
    payments = list(
        Payment.objects.filter(allocation_group=group)
        .exclude(status=PaymentStatus.VOIDED)
        .order_by("created_at", "pk")
    )
    for payment in payments:
        refusal = refunds.manual_void_refusal(payment)
        if refusal:
            raise _Refused(
                UNDO_REFUSED.format(description=format_amount(payment.amount), message=refusal)
            )
    for payment in payments:
        outcome = refunds.void_manual_payment(
            payment.pk,
            reason=UNDO_REASON,
            actor=actor,
            creation_key=uuid.uuid5(txn.pk, f"undo-{payment.pk}"),
        )
        if not outcome.ok:
            raise _Refused(
                UNDO_REFUSED.format(
                    description=format_amount(payment.amount), message=outcome.message
                )
            )
    return [str(payment.pk) for payment in payments]


# --- The page -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Row:
    """One deposit as the page shows it."""

    txn: BankTransaction
    suggestion: matching.Suggestion | None
    #: The suggested store's title and what the suggestion pays.
    store: str = ""
    charge: str = ""
    alias: str = ""
    #: A tie's candidates: the stores' titles.
    candidates: tuple[str, ...] = ()
    #: A recorded deposit's lines: (store, what, amount, voided).
    lines: tuple[tuple[str, str, Decimal, bool], ...] = ()


def _charge_text(context: matching.Context, suggestion: matching.Suggestion) -> str:
    if suggestion.lease is None:
        return ""
    if suggestion.charge_id is None:
        total = matching.whole_balance(context, suggestion.lease)
        return f"Whole balance ({format_amount(total)})"
    row = context.by_charge.get((suggestion.charge_type, suggestion.charge_id))
    return f"{row.description} ({format_amount(row.balance)})" if row is not None else ""


def rows(transactions: Sequence[BankTransaction], today: date) -> list[Row]:
    """The page's rows: a live suggestion for each new deposit and the recorded lines of each
    applied one, in a number of queries that does not grow with the rows."""
    new = [txn for txn in transactions if txn.status == Status.NEW]
    context = matching.build_context(new, today) if new else None
    suggestions = {txn.pk: matching.suggest_with(txn, context) for txn in new} if context else {}
    lease_ids = {s.lease for s in suggestions.values() if s.lease is not None}
    lease_ids |= {c.lease for s in suggestions.values() for c in s.candidates}
    leases = {
        lease.pk: lease for lease in Lease.objects.filter(pk__in=lease_ids).select_related("store")
    }
    known = {(lease, alias) for lease, alias in (context.aliases if context else [])}
    groups = [txn.allocation_group for txn in transactions if txn.allocation_group is not None]
    lines: dict[uuid.UUID, list[tuple[str, str, Decimal, bool]]] = {}
    if groups:
        for payment in (
            Payment.objects.filter(allocation_group__in=groups)
            .select_related(
                "rent_charge__lease__store",
                "one_off_charge__lease__store",
                "insurance_charge__lease__store",
            )
            .order_by("created_at", "pk")
        ):
            charge: Any = payment.rent_charge or payment.one_off_charge or payment.insurance_charge
            lease = getattr(charge, "lease", None)
            title = store_title(lease) if lease is not None else ""
            group = payment.allocation_group
            if group is None:
                continue
            lines.setdefault(group, []).append(
                (
                    title,
                    payment.get_kind_display(),
                    Decimal(payment.amount),
                    payment.status == PaymentStatus.VOIDED,
                )
            )
    found = []
    for txn in transactions:
        suggestion = suggestions.get(txn.pk)
        if suggestion is None or context is None:
            group = txn.allocation_group
            found.append(Row(txn, None, lines=tuple(lines.get(group, ())) if group else ()))
            continue
        lease = leases.get(suggestion.lease) if suggestion.lease else None
        found.append(
            Row(
                txn,
                suggestion,
                store=store_title(lease) if lease is not None else "",
                charge=_charge_text(context, suggestion),
                alias=alias_offer(txn, suggestion.lease, known),
                candidates=tuple(
                    dict.fromkeys(
                        store_title(leases[candidate.lease])
                        for candidate in suggestion.candidates
                        if candidate.lease in leases and suggestion.lease is None
                    )
                ),
            )
        )
    return found

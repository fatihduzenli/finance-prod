"""The audit log page, its CSV export and the Activity list of the Documents sections
(docs/qa/PLAN-010d-one-off-charges-and-audit-page.md D8).

Read only. :func:`parse_query` turns query-string values into an :class:`AuditQuery` and
never fails: what it cannot use is replaced or dropped, and the page says so, one
sentence each. Filters: ``action`` (exact, or a prefix when it ends with ``*``),
``user`` (a username, exact), ``entity_type`` (exact), ``entity_id`` (exact, or a prefix
when it ends with ``*``; ``%`` and ``_`` are plain characters), ``from`` and ``to``
(New York calendar days, both included; by default the last :data:`DEFAULT_DAYS` days,
at most :data:`MAX_DAYS`; a year outside :data:`MIN_YEAR`..:data:`MAX_YEAR` is unreadable,
so no date arithmetic can leave the calendar), ``documents_of`` (``store:<id>`` or
``lease:<id>``: the ``document.*`` rows of that record, as its Activity list reads them),
``store_of`` (a store id: the rows of the store and of its records, as the store page's
History tab reads them, :func:`store_filter`; PLAN-012d D3) and ``page``.
A filter longer than :data:`FILTER_MAX_LENGTH` characters is dropped.

Rows are newest first, :data:`PAGE_SIZE` per page, read with one query (no count). The
CSV holds at most :data:`CSV_MAX_ROWS` rows; it is written by
:class:`ledger.services.csv_export.CsvWriter` (formula-safe text) and
``before``/``after`` are compact JSON. On the page and in the file the entity id is
masked, and every value of ``before``/``after`` that looks like an identifier
(:func:`looks_like_identifier`) is masked by :func:`payments.provider.mask_identifier`;
money stays the text it was stored as. The caller runs :func:`rows` and
:func:`csv_records` under the report statement timeout (``ledger.views.reports.bounded``).
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.db.models import CharField, Q, QuerySet, Value
from django.utils import timezone

from finance.dates import day_text, moment_text
from ledger.models import (
    AuditLog,
    AutopayPlan,
    CollectionSchedule,
    ContactEntry,
    Document,
    InstallmentPlan,
    InsuranceCharge,
    Invoice,
    InvoiceDraft,
    Lease,
    LeaseEnd,
    LeaseTransfer,
    OneOffCharge,
    Operator,
    Payment,
    RentCharge,
    Store,
    StoreCost,
)
from ledger.services import csv_export
from ledger.services.csv_export import CsvWriter
from payments.provider import mask_identifier

NEW_YORK = ZoneInfo("America/New_York")
PAGE_SIZE = 100
#: Pages beyond this are not read (100 rows each: the CSV's cap).
MAX_PAGE = 500
DEFAULT_DAYS = 7
MAX_DAYS = 366
#: Dates outside these years are unreadable: the New York day bounds and the range cap
#: stay far from ``date.min`` and ``date.max``.
MIN_YEAR = 2000
MAX_YEAR = 2100
CSV_MAX_ROWS = csv_export.CSV_MAX_ROWS
FILTER_MAX_LENGTH = 200
ACTIVITY_SIZE = 10
DOCUMENT_ACTIONS = "document."
ENTITY_DOCUMENT = "document"
ENTITY_OPERATOR = "operator"
OWNER_STORE = "store"
OWNER_LEASE = "lease"
PREFIX_MARK = "*"

#: The entity types whose rows link to a record page; a document links to its owner's.
LINKED_TYPES: frozenset[str] = frozenset(
    {"operator", "store", "lease", "one_off_charge", "rent_charge", "payment"}
)

CSV_HEADER: tuple[str, ...] = (
    "Audit entry",
    "Time (UTC)",
    "Time (New York)",
    "Action",
    "User",
    "Record type",
    "Record (masked)",
    "Before (JSON)",
    "After (JSON)",
)

# --- Sentences ------------------------------------------------------------------------

UNREADABLE_DATES = "The dates could not be read; the last {days} days are shown."
SWAPPED = "The end date was before the start date; they were swapped."
CAPPED = "The range is at most {days} days; it now starts on {start}."
TOO_LONG = "A filter longer than {limit} characters was ignored: {name}."
UNKNOWN_USER = "No user has that username; no rows match."
BAD_OWNER = "The documents filter could not be read and was ignored."
BAD_STORE = "The store filter could not be read and was ignored."
#: The newest rows the store page's History tab shows (PLAN-012d D3).
STORE_HISTORY_SIZE = 200
PAGE_CAPPED = "Pages after {limit} are not shown; narrow the filters."

_ISO_DATE = re.compile(r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
#: An identifier-shaped value: letters, digits, ``_`` and ``-`` only, 9 to 64 of them,
#: with at least one digit. Our invoice numbers and ISO dates are excepted.
_IDENTIFIER = re.compile(r"\A(?=[^0-9]*[0-9])[A-Za-z0-9_-]{9,64}\Z")
_INVOICE_NUMBER = re.compile(r"\AINV-[0-9]{4}-[0-9]{4,}\Z")


# --- The query ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AuditQuery:
    date_from: date
    date_to: date
    action: str = ""
    user: str = ""
    entity_type: str = ""
    entity_id: str = ""
    #: ``(owner type, owner id)`` of the ``documents_of`` filter, or ``None``.
    documents_of: tuple[str, uuid.UUID] | None = None
    #: The store of the ``store_of`` filter, or ``None``.
    store_of: uuid.UUID | None = None
    page: int = 1
    #: Why the query differs from what was asked, one sentence each.
    messages: tuple[str, ...] = ()

    def as_query(self, *, page: int | None = None) -> dict[str, str]:
        """The query-string values that give this query back (``page`` replaced)."""
        values = {"from": self.date_from.isoformat(), "to": self.date_to.isoformat()}
        for name, value in (
            ("action", self.action),
            ("user", self.user),
            ("entity_type", self.entity_type),
            ("entity_id", self.entity_id),
        ):
            if value:
                values[name] = value
        if self.documents_of is not None:
            values["documents_of"] = f"{self.documents_of[0]}:{self.documents_of[1]}"
        if self.store_of is not None:
            values["store_of"] = str(self.store_of)
        shown = self.page if page is None else page
        if shown > 1:
            values["page"] = str(shown)
        return values

    @property
    def start(self) -> datetime:
        """The first moment of ``date_from`` in New York."""
        return datetime.combine(self.date_from, time.min, tzinfo=NEW_YORK)

    @property
    def end(self) -> datetime:
        """The first moment after ``date_to`` in New York."""
        return datetime.combine(self.date_to + timedelta(days=1), time.min, tzinfo=NEW_YORK)


def today_in_new_york(now: datetime | None = None) -> date:
    return (now or timezone.now()).astimezone(NEW_YORK).date()


def _date(raw: str) -> date | None:
    if not _ISO_DATE.match(raw):
        return None
    try:
        found = date.fromisoformat(raw)
    except ValueError:
        return None
    return found if MIN_YEAR <= found.year <= MAX_YEAR else None


def _owner(raw: str) -> tuple[str, uuid.UUID] | None:
    kind, _, key = raw.partition(":")
    if kind not in (OWNER_STORE, OWNER_LEASE):
        return None
    try:
        return kind, uuid.UUID(key)
    except ValueError:
        return None


def parse_query(params: Mapping[str, str], today: date | None = None) -> AuditQuery:
    """The query from query-string values; never raises (see the module docstring)."""
    day = today or today_in_new_york()
    messages: list[str] = []
    text: dict[str, str] = {}
    for name in ("action", "user", "entity_type", "entity_id", "documents_of", "store_of"):
        value = (params.get(name, "") or "").strip()
        if len(value) > FILTER_MAX_LENGTH:
            messages.append(TOO_LONG.format(limit=FILTER_MAX_LENGTH, name=name))
            value = ""
        text[name] = value
    raw_from = (params.get("from", "") or "").strip()[:FILTER_MAX_LENGTH]
    raw_to = (params.get("to", "") or "").strip()[:FILTER_MAX_LENGTH]
    start = _date(raw_from) if raw_from else None
    end = _date(raw_to) if raw_to else None
    if (raw_from and start is None) or (raw_to and end is None):
        start, end = None, None
        messages.append(UNREADABLE_DATES.format(days=DEFAULT_DAYS))
    end = end or day
    start = start or end - timedelta(days=DEFAULT_DAYS - 1)
    if end < start:
        start, end = end, start
        messages.append(SWAPPED)
    if (end - start).days + 1 > MAX_DAYS:
        start = end - timedelta(days=MAX_DAYS - 1)
        messages.append(CAPPED.format(days=MAX_DAYS, start=day_text(start)))
    owner = None
    if text["documents_of"]:
        owner = _owner(text["documents_of"])
        if owner is None:
            messages.append(BAD_OWNER)
    store_of = None
    if text["store_of"]:
        try:
            store_of = uuid.UUID(text["store_of"])
        except ValueError:
            messages.append(BAD_STORE)
    page = _page(params.get("page", "") or "")
    if page > MAX_PAGE:
        page = MAX_PAGE
        messages.append(PAGE_CAPPED.format(limit=MAX_PAGE))
    return AuditQuery(
        date_from=start,
        date_to=end,
        action=text["action"],
        user=text["user"],
        entity_type=text["entity_type"],
        entity_id=text["entity_id"],
        documents_of=owner,
        store_of=store_of,
        page=page,
        messages=tuple(messages),
    )


def _page(raw: str) -> int:
    value = raw.strip()
    if not value.isascii() or not value.isdigit() or len(value) > 6:
        return 1
    return max(1, int(value))


# --- Reading --------------------------------------------------------------------------


def _text_filter(column: str, value: str) -> Q:
    if value.endswith(PREFIX_MARK):
        return Q(**{f"{column}__startswith": value.removesuffix(PREFIX_MARK)})
    return Q(**{column: value})


def _document_ids(owner_type: str, owner_id: object) -> list[str]:
    owner_filter = {"store_id": owner_id} if owner_type == OWNER_STORE else {"lease_id": owner_id}
    return [str(pk) for pk in Document.objects.filter(**owner_filter).values_list("pk", flat=True)]


def _documents_filter(owner_type: str, owner_id: object) -> Q:
    """The ``document.*`` rows of a store or lease: those about the record itself (a
    refused upload) and those about one of its documents."""
    return Q(action__startswith=DOCUMENT_ACTIONS) & (
        Q(entity_type=owner_type, entity_id=str(owner_id))
        | Q(entity_type=ENTITY_DOCUMENT, entity_id__in=_document_ids(owner_type, owner_id))
    )


def _filtered(query: AuditQuery) -> QuerySet[AuditLog] | None:
    """The rows of ``query``, newest first; ``None`` when no row can match."""
    found = AuditLog.objects.filter(at__gte=query.start, at__lt=query.end)
    if query.user:
        actor = User.objects.filter(username=query.user).values_list("pk", flat=True).first()
        if actor is None:
            return None
        found = found.filter(actor_id=actor)
    if query.action:
        found = found.filter(_text_filter("action", query.action))
    if query.entity_type:
        found = found.filter(entity_type=query.entity_type)
    if query.entity_id:
        found = found.filter(_text_filter("entity_id", query.entity_id))
    if query.documents_of is not None:
        found = found.filter(_documents_filter(*query.documents_of))
    if query.store_of is not None:
        found = found.filter(store_filter(query.store_of))
    return found.select_related("actor").order_by("-at", "-id")


def _store_records(store_id: uuid.UUID) -> dict[str, list[str]]:
    """Per entity type, the ids of the store's records (one query, a union): the store,
    its leases and cost rows, their rent, one-off and insurance charges, invoices, invoice
    drafts, payments, schedules, documents, contact entries, lease ends, transfers,
    installment plans and autopay plans."""
    on_store = Q(lease__store_id=store_id)
    on_charge = (
        Q(rent_charge__lease__store_id=store_id)
        | Q(one_off_charge__lease__store_id=store_id)
        | Q(insurance_charge__lease__store_id=store_id)
    )
    sources: list[tuple[str, QuerySet[Any]]] = [
        ("store", Store.objects.filter(pk=store_id)),
        ("store_cost", StoreCost.objects.filter(store_id=store_id)),
        ("lease", Lease.objects.filter(store_id=store_id)),
        ("rent_charge", RentCharge.objects.filter(on_store)),
        ("one_off_charge", OneOffCharge.objects.filter(on_store)),
        ("insurance_charge", InsuranceCharge.objects.filter(on_store)),
        (
            "invoice",
            Invoice.objects.filter(
                on_charge
                | Q(line_items__one_off_charge__lease__store_id=store_id)
                | Q(line_items__rent_charge__lease__store_id=store_id)
            ),
        ),
        ("invoice_draft", InvoiceDraft.objects.filter(on_store)),
        ("payment", Payment.objects.filter(on_charge)),
        ("collection_schedule", CollectionSchedule.objects.filter(on_store)),
        ("document", Document.objects.filter(Q(store_id=store_id) | on_store)),
        ("contact_entry", ContactEntry.objects.filter(on_store)),
        ("lease_end", LeaseEnd.objects.filter(on_store)),
        ("lease_transfer", LeaseTransfer.objects.filter(store_id=store_id)),
        ("installment_plan", InstallmentPlan.objects.filter(on_store)),
        ("autopay_plan", AutopayPlan.objects.filter(store_id=store_id)),
    ]
    parts = [
        rows.annotate(record_type=Value(kind, output_field=CharField())).values_list(
            "record_type", "pk"
        )
        for kind, rows in sources
    ]
    found: dict[str, list[str]] = {}
    for kind, pk in parts[0].union(*parts[1:]):
        found.setdefault(kind, []).append(str(pk))
    return found


def store_filter(store_id: uuid.UUID) -> Q:
    """The audit rows of a store and of its records (:func:`_store_records`); a filter
    that matches nothing when the store has none."""
    condition = Q(pk__in=[])
    for kind, ids in _store_records(store_id).items():
        condition |= Q(entity_type=kind, entity_id__in=ids)
    return condition


def rows_for_store(store: Store, *, operator: Operator | None = None) -> list[AuditRow]:
    """The store page's History tab (PLAN-012d D3): the newest
    :data:`STORE_HISTORY_SIZE` rows of the store and of its records, newest first,
    masked as on the audit page; with ``operator`` (the operator page, D14), the
    operator's own rows too."""
    condition = store_filter(store.pk)
    if operator is not None:
        condition |= Q(entity_type=ENTITY_OPERATOR, entity_id=str(operator.pk))
    found = list(
        AuditLog.objects.filter(condition)
        .select_related("actor")
        .order_by("-at", "-id")[:STORE_HISTORY_SIZE]
    )
    owners = _document_owners(row.entity_id for row in found if row.entity_type == ENTITY_DOCUMENT)
    return [_row(row, owners) for row in found]


def store_query(store: Store, today: date | None = None) -> dict[str, str]:
    """The ``/audit/`` query of the store's rows, over the longest range the page
    allows (the History tab's link)."""
    end = today or today_in_new_york()
    return {
        "store_of": str(store.pk),
        "from": (end - timedelta(days=MAX_DAYS - 1)).isoformat(),
        "to": end.isoformat(),
    }


@dataclass(frozen=True, slots=True)
class AuditRow:
    id: int
    #: ``YYYY-MM-DD HH:MM:SS``; New York with its zone abbreviation (EDT or EST).
    at_utc: str
    at_new_york: str
    action: str
    #: The admin who acted, or ``None``; a page shows it through ``|staff``.
    user: User | None
    entity_type: str
    entity_id: str
    before: list[tuple[str, str]]
    after: list[tuple[str, str]]
    #: ``(entity type, id)`` of the record page this row links to, or ``None``.
    record: tuple[str, str] | None = None


@dataclass(frozen=True, slots=True)
class AuditPage:
    rows: list[AuditRow] = field(default_factory=list)
    has_next: bool = False
    messages: tuple[str, ...] = ()


def rows(query: AuditQuery) -> AuditPage:
    """One page of rows (:data:`PAGE_SIZE`), newest first, without a count."""
    found = _filtered(query)
    if found is None:
        return AuditPage(messages=(*query.messages, UNKNOWN_USER))
    offset = (query.page - 1) * PAGE_SIZE
    read = list(found[offset : offset + PAGE_SIZE + 1])
    shown = read[:PAGE_SIZE]
    owners = _document_owners(row.entity_id for row in shown if row.entity_type == ENTITY_DOCUMENT)
    return AuditPage(
        rows=[_row(row, owners) for row in shown],
        has_next=len(read) > PAGE_SIZE,
        messages=query.messages,
    )


def _document_owners(ids: Iterable[str]) -> dict[str, tuple[str, str]]:
    keys = []
    for raw in ids:
        try:
            keys.append(uuid.UUID(raw))
        except ValueError:
            continue
    owners: dict[str, tuple[str, str]] = {}
    for pk, store_id, lease_id in Document.objects.filter(pk__in=keys).values_list(
        "pk", "store_id", "lease_id"
    ):
        # A document belongs to exactly one store or one lease (a CHECK, PLAN-010c D4).
        kind = OWNER_STORE if store_id else OWNER_LEASE
        owners[str(pk)] = (kind, str(store_id or lease_id))
    return owners


def _record(row: AuditLog, owners: Mapping[str, tuple[str, str]]) -> tuple[str, str] | None:
    if row.entity_type == ENTITY_DOCUMENT:
        return owners.get(row.entity_id)
    if row.entity_type not in LINKED_TYPES:
        return None
    try:
        return row.entity_type, str(uuid.UUID(row.entity_id))
    except ValueError:
        return None


def _row(row: AuditLog, owners: Mapping[str, tuple[str, str]]) -> AuditRow:
    return AuditRow(
        id=row.id,
        at_utc=utc_text(row.at),
        at_new_york=new_york_text(row.at),
        action=row.action,
        user=row.actor,
        entity_type=row.entity_type,
        entity_id=mask_identifier(row.entity_id),
        before=pairs(row.before),
        after=pairs(row.after),
        record=_record(row, owners),
    )


def utc_text(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def new_york_text(moment: datetime) -> str:
    """The Time column as a page reads it (DESIGN-SPEC §5): ``Oct 7, 2026 · 12:30``, New
    York; the export keeps both ISO times (:func:`_csv_record`)."""
    return moment_text(moment)


# --- Masking --------------------------------------------------------------------------


def looks_like_identifier(value: str) -> bool:
    """A UUID, or 9 to 64 letters, digits, ``_`` and ``-`` with a digit among them; not
    an ISO date and not one of our invoice numbers."""
    if _ISO_DATE.match(value) or _INVOICE_NUMBER.match(value):
        return False
    return bool(_IDENTIFIER.match(value))


def masked(value: Any) -> Any:
    """``value`` with every identifier-shaped string masked, at any depth."""
    if isinstance(value, str):
        return mask_identifier(value) if looks_like_identifier(value) else value
    if isinstance(value, dict):
        return {str(key): masked(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [masked(item) for item in value]
    return value


def compact_json(value: Any) -> str:
    if value is None:
        return ""
    return json.dumps(masked(value), separators=(",", ":"), ensure_ascii=False, sort_keys=True)


def _shown(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    return compact_json(value)


def pairs(value: Any) -> list[tuple[str, str]]:
    """``before``/``after`` as (key, text) pairs, masked; a value that is not an
    object is one pair with an empty key."""
    if value is None:
        return []
    shown = masked(value)
    if isinstance(shown, dict):
        return [(key, _shown(item)) for key, item in sorted(shown.items())]
    return [("", _shown(shown))]


# --- CSV ------------------------------------------------------------------------------


def _csv_record(row: AuditLog) -> list[object]:
    return [
        row.id,
        row.at.astimezone(UTC),
        row.at.astimezone(NEW_YORK),
        row.action,
        row.actor.get_username() if row.actor is not None else "",
        row.entity_type,
        mask_identifier(row.entity_id),
        compact_json(row.before),
        compact_json(row.after),
    ]


def csv_records(query: AuditQuery) -> Iterator[list[object]]:
    """The rows of ``query`` (every page) as CSV records, at most one more than
    :data:`CSV_MAX_ROWS`: the writer turns that one into its ``truncated`` row."""
    found = _filtered(query)
    if found is None:
        return
    for row in found[: CSV_MAX_ROWS + 1].iterator(chunk_size=2_000):
        yield _csv_record(row)


def csv_lines_of(records: Iterable[list[object]]) -> Iterator[str]:
    """The header, then ``records``, as CSV lines (the export reads its records first,
    inside the statement timeout, and streams these)."""
    return CsvWriter(CSV_HEADER, limit=CSV_MAX_ROWS).lines(records)


# --- Activity of a Documents section --------------------------------------------------


@dataclass(frozen=True, slots=True)
class Activity:
    at_new_york: str
    action: str
    #: The staff member, or ``None``; a page shows it through ``|staff``.
    user: User | None
    kind: str
    outcome: str


def activity_for(owner: Store | Lease) -> list[Activity]:
    """The last :data:`ACTIVITY_SIZE` ``document.*`` rows of a store or lease, newest
    first: its uploads (refused ones too, with their reason code), previews, downloads
    and deletions."""
    owner_type = OWNER_STORE if isinstance(owner, Store) else OWNER_LEASE
    found = (
        AuditLog.objects.filter(_documents_filter(owner_type, owner.pk))
        .select_related("actor")
        .order_by("-at", "-id")[:ACTIVITY_SIZE]
    )
    return [_activity(row) for row in found]


def _activity(row: AuditLog) -> Activity:
    after = row.after if isinstance(row.after, dict) else {}
    reason = after.get("reason")
    result = after.get("result")
    outcome = (
        str(reason) if isinstance(reason, str) else str(result) if isinstance(result, str) else ""
    )
    kind = after.get("kind")
    return Activity(
        at_new_york=new_york_text(row.at),
        action=row.action.removeprefix(DOCUMENT_ACTIONS),
        user=row.actor,
        kind=str(kind) if isinstance(kind, str) else "",
        outcome=outcome,
    )


def activity_query(owner: Store | Lease, today: date | None = None) -> dict[str, str]:
    """The ``/audit/`` query that lists the rows of the owner's Activity list, over the
    longest range the page allows."""
    owner_type = OWNER_STORE if isinstance(owner, Store) else OWNER_LEASE
    end = today or today_in_new_york()
    return {
        "documents_of": f"{owner_type}:{owner.pk}",
        "from": (end - timedelta(days=MAX_DAYS - 1)).isoformat(),
        "to": end.isoformat(),
    }

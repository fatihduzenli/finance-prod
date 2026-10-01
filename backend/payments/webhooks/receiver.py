"""``POST /api/webhooks/stax/<token>/`` (docs/qa/PLAN-008-webhooks.md D2, D3, D11).

The provider signs nothing, so the secret is the token in the URL. The view compares
it in constant time with ``settings.STAX_WEBHOOK_URL_TOKEN``. A wrong or missing token,
an empty configured token, any method but POST and a body over 256 KB all get the same
answer: 404 with an empty body, no database write, and one log line that names the path
as ``/api/webhooks/stax/[token]/``. The response is returned, never raised, and marked
as logged, so Django does not log the real path (which would hold the attempted token).

An accepted delivery is stored (redacted, with its deduplication hash) and answered
with 200 at once; its ID is queued after the commit. Nothing here calls the provider,
and nothing here imports the adapter package. A byte-identical redelivery stores
nothing new: the first row's ``duplicate_count`` goes up and its outcome stays.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import uuid
from collections.abc import Mapping
from decimal import Decimal
from functools import partial

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F
from django.http import HttpRequest, HttpResponse, HttpResponseNotFound
from django.views.decorators.csrf import csrf_exempt

from ledger.validators import validate_json_has_no_payment_data
from payments.models import WebhookEvent, WebhookEventSource, WebhookOutcome
from payments.redaction import DROPPED, is_safe_text, redact
from payments.webhooks import queue
from payments.webhooks.events import EVENT_NAMES, UNKNOWN_TYPE

#: Largest body accepted, in bytes (D2).
MAX_BODY_BYTES = 256 * 1024
#: How the route appears in this view's log lines: never with the token.
LOGGED_PATH = "/api/webhooks/stax/[token]/"
#: TODO-VERIFY-015: the header that carries the event name.
EVENT_HEADER = "stax-event-name"
#: Body keys read for the event name, in this order, when the header is absent.
BODY_EVENT_KEYS: tuple[str, ...] = ("event", "type", "event_name")
#: Deepest JSON nesting stored; a deeper body is kept as its hash only.
MAX_PAYLOAD_DEPTH = 32
#: Methods named in the refusal log line; anything else is logged as "other".
_LOGGED_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"})

_EVENT_NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_OBJECT_ID = re.compile(r"[A-Za-z0-9_-]{1,255}")


class _NotJson:
    """Marker for a body that is not a JSON value."""


NOT_JSON = _NotJson()

logger = logging.getLogger(__name__)


def _not_found(request: HttpRequest, reason: str) -> HttpResponseNotFound:
    method = request.method if request.method in _LOGGED_METHODS else "other"
    logger.warning("Webhook refused (%s): method=%s path=%s", reason, method, LOGGED_PATH)
    response = HttpResponseNotFound()
    # Django's request logger skips a response marked as logged; its line would carry
    # the real path, attempted token included.
    response._has_been_logged = True  # type: ignore[attr-defined]
    return response


def _token_matches(rest: str) -> bool:
    configured: str = settings.STAX_WEBHOOK_URL_TOKEN
    expected = f"{configured}/".encode()
    return hmac.compare_digest(rest.encode("utf-8", "surrogatepass"), expected)


def _read_body(request: HttpRequest) -> bytes | None:
    """The body, or None when it is (or claims to be) larger than the limit."""
    declared = request.META.get("CONTENT_LENGTH") or "0"
    try:
        length = int(declared)
    except ValueError:
        return None
    if length > MAX_BODY_BYTES:
        return None
    body = request.read(MAX_BODY_BYTES + 1)
    return None if len(body) > MAX_BODY_BYTES else body


@csrf_exempt
def receive(request: HttpRequest, rest: str) -> HttpResponse:
    """``rest`` is everything after ``/api/webhooks/stax/``: the token and a slash."""
    if request.method != "POST":
        return _not_found(request, "method")
    if not settings.STAX_WEBHOOK_URL_TOKEN:
        return _not_found(request, "no token configured")
    if not _token_matches(rest):
        return _not_found(request, "token")
    body = _read_body(request)
    if body is None:
        return _not_found(request, "body too large")
    event_id = store_event(body, request.headers.get(EVENT_HEADER))
    if event_id is not None:
        transaction.on_commit(partial(queue.enqueue, event_id))
    return HttpResponse(status=200)


# --- Storage --------------------------------------------------------------------------


def _refuse_constant(name: str) -> None:
    raise ValueError("Non-finite numbers are not accepted")


def parse_body(raw: bytes) -> object:
    """The body as JSON with ``Decimal`` numbers, or :data:`NOT_JSON`."""
    try:
        return json.loads(raw, parse_float=Decimal, parse_constant=_refuse_constant)
    except (ValueError, RecursionError):
        # json.JSONDecodeError and UnicodeDecodeError are ValueErrors.
        return NOT_JSON


def _event_name(value: object) -> str | None:
    if isinstance(value, str) and _EVENT_NAME.fullmatch(value) and is_safe_text(value):
        return value
    return None


def _shape_type(body: Mapping[str, object]) -> str | None:
    if "invoice_date_at" in body:
        return "invoice"
    if "rule" in body:
        return "schedule"
    if any(key in body for key in ("firstname", "lastname", "email")):
        return "customer"
    if "total" in body and "method" in body:
        return "transaction"
    if "batched_at" in body:
        return "deposit"
    return None


def resolve_event_type(header: str | None, body: object) -> tuple[str, WebhookEventSource]:
    """Header, then body keys, then the object's shape (amendment 6).

    The header is taken when it is a plain name. A body key is taken only when its value
    is one of the registered event names: a transaction object's own ``type``
    (``charge``, ``refund``) is not an event name.
    """
    from_header = _event_name(header)
    if from_header is not None:
        return from_header, WebhookEventSource.HEADER
    if isinstance(body, Mapping):
        for key in BODY_EVENT_KEYS:
            name = _event_name(body.get(key))
            if name in EVENT_NAMES:
                return name, WebhookEventSource.BODY
        shape = _shape_type(body)
        if shape is not None:
            return shape, WebhookEventSource.SHAPE
    return UNKNOWN_TYPE, WebhookEventSource.UNKNOWN


#: Keys read for the object's ID, in this order. A deposit is identified by its batch
#: (TODO-VERIFY-030, TODO-VERIFY-054: a deposit event's body carries ``batch_id`` and
#: no ``id``).
OBJECT_ID_KEYS: tuple[str, ...] = ("id", "batch_id")


def object_id(body: object) -> str:
    """The body's ``id`` (or, without one, ``batch_id``) when it is a plain identifier,
    else ``""``."""
    if not isinstance(body, Mapping):
        return ""
    value = next((body[key] for key in OBJECT_ID_KEYS if body.get(key) is not None), None)
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if isinstance(value, str) and _OBJECT_ID.fullmatch(value) and is_safe_text(value):
        return value
    return ""


def _depth_within(value: object, limit: int) -> bool:
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, Mapping | list):
            if depth > limit:
                return False
            children = item.values() if isinstance(item, Mapping) else item
            stack.extend((child, depth + 1) for child in children)
    return True


def stored_payload(body: object) -> object:
    """The redacted body, or None when nothing of it may be stored."""
    if body is NOT_JSON or not _depth_within(body, MAX_PAYLOAD_DEPTH):
        return None
    payload = redact(body)
    if payload is DROPPED:
        return None
    try:
        # Each value passed on its own; together they may still read as a routing and
        # account number. Then only the hash is kept.
        validate_json_has_no_payment_data(payload)
    except ValidationError:
        return None
    return payload


def dedupe_hash(event_type: str, raw: bytes) -> str:
    return hashlib.sha256(event_type.encode() + b"\n" + raw).hexdigest()


def store_event(raw: bytes, header: str | None) -> uuid.UUID | None:
    """Store one delivery; return its ID, or None for a byte-identical redelivery."""
    body = parse_body(raw)
    event_type, source = resolve_event_type(header, body)
    digest = dedupe_hash(event_type, raw)
    try:
        with transaction.atomic():
            event = WebhookEvent.objects.create(
                event_type=event_type,
                event_source=source,
                provider_object_id=object_id(body),
                dedupe_hash=digest,
                payload=stored_payload(body),
                outcome=WebhookOutcome.RECEIVED,
            )
    except IntegrityError:
        updated = WebhookEvent.objects.filter(dedupe_hash=digest).update(
            duplicate_count=F("duplicate_count") + 1
        )
        if updated != 1:
            raise
        return None
    return event.pk

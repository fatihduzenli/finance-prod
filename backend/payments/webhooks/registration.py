"""Registering this app's webhooks with the provider (docs/qa/PLAN-008-webhooks.md D9, D10).

The target URL is ``APP_BASE_URL`` + ``/api/webhooks/stax/<token>/``. It holds the
secret token, so it is built here, sent, compared, and never returned to a caller or
printed: reports carry event names and masked registration IDs only.

The registration ID of each event is stored in ``external_refs`` (``entity_type =
webhook_registration``, ``entity_id`` = UUID5 of the event name, ``external_kind =
webhook``). :func:`register` lists what the provider holds first and creates only the
registrations that are missing; :func:`unregister` deletes by the stored IDs. Both
refuse to run when ``APP_ENV`` is ``local``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

from django.conf import settings
from django.db import transaction
from django.utils import timezone

import payments
from payments.errors import ProviderRejected
from payments.lease_sync import PROVIDER_KEY
from payments.models import ExternalRef
from payments.provider import WebhookRecord, mask_identifier
from payments.webhooks.events import EVENT_NAMES

ENTITY_REGISTRATION = "webhook_registration"
KIND_WEBHOOK = "webhook"
RECEIVER_PATH = "/api/webhooks/stax/"
LOCAL = "local"
NOT_FOUND = 404

REFUSED_LOCAL = "Webhook registration is refused when APP_ENV is local."
UNCONFIGURED = "STAX_WEBHOOK_URL_TOKEN is empty; nothing was registered."
NOT_HTTPS = "APP_BASE_URL must be an https URL with a host; nothing was registered."

CREATED = "created"
EXISTS = "exists"
DELETED = "deleted"
GONE = "already gone"


class RegistrationRefused(Exception):  # noqa: N818 (a refusal, not a failure)
    """The configuration does not allow registration; the message is value-free."""


@dataclass(frozen=True, slots=True)
class Line:
    event: str
    action: str
    #: The registration ID, masked.
    registration: str


def entity_id(event: str) -> uuid.UUID:
    """The deterministic ``external_refs.entity_id`` of one event's registration."""
    return uuid.uuid5(uuid.NAMESPACE_URL, event)


def target_url() -> str:
    """The receiver's URL with the token. Never printed or logged."""
    if settings.APP_ENV == LOCAL:
        raise RegistrationRefused(REFUSED_LOCAL)
    token: str = settings.STAX_WEBHOOK_URL_TOKEN
    if not token:
        raise RegistrationRefused(UNCONFIGURED)
    base: str = settings.APP_BASE_URL
    parts = urlsplit(base)
    if parts.scheme != "https" or not parts.hostname or parts.query or parts.fragment:
        raise RegistrationRefused(NOT_HTTPS)
    return f"{base.rstrip('/')}{RECEIVER_PATH}{token}/"


def _stored(event: str) -> ExternalRef | None:
    return ExternalRef.objects.filter(
        entity_type=ENTITY_REGISTRATION,
        entity_id=entity_id(event),
        provider=PROVIDER_KEY,
        external_kind=KIND_WEBHOOK,
        superseded_at__isnull=True,
    ).first()


def _store(event: str, registration_id: str) -> None:
    """Make ``registration_id`` the event's current stored ID."""
    with transaction.atomic():
        current = _stored(event)
        if current is not None and current.external_id == registration_id:
            return
        if current is not None:
            current.superseded_at = timezone.now()
            current.save(update_fields=["superseded_at"])
        ExternalRef.objects.create(
            entity_type=ENTITY_REGISTRATION,
            entity_id=entity_id(event),
            provider=PROVIDER_KEY,
            external_kind=KIND_WEBHOOK,
            external_id=registration_id,
        )


def register() -> list[Line]:
    """Create the registrations that are missing for :data:`EVENT_NAMES`; store every ID.

    An event's registration exists when the provider lists the ID we stored for it (with
    our URL, or with no URL in the listing, TODO-VERIFY-050), or lists one for that event
    with our URL. A provider failure stops the run; the IDs stored before it are kept,
    and a rerun creates only what is still missing.
    """
    url = target_url()
    provider = payments.get_provider()
    listed = {record.external_id: record for record in provider.list_webhooks()}
    by_event: dict[str, WebhookRecord] = {}
    for record in listed.values():
        if record.target_url == url and record.event in EVENT_NAMES:
            by_event.setdefault(record.event, record)
    lines: list[Line] = []
    for event in EVENT_NAMES:
        stored = _stored(event)
        known = listed.get(stored.external_id) if stored is not None else None
        if known is not None and known.target_url not in ("", url):
            known = None  # registered for another URL (an earlier token): not ours now
        existing = known or by_event.get(event)
        record = existing if existing is not None else provider.create_webhook(url, event)
        _store(event, record.external_id)
        action = EXISTS if existing is not None else CREATED
        lines.append(Line(event, action, mask_identifier(record.external_id)))
    return lines


def unregister() -> list[Line]:
    """Delete every stored registration at the provider and retire its reference."""
    if settings.APP_ENV == LOCAL:
        raise RegistrationRefused(REFUSED_LOCAL)
    provider = payments.get_provider()
    lines: list[Line] = []
    for event in EVENT_NAMES:
        ref = _stored(event)
        if ref is None:
            continue
        action = DELETED
        try:
            provider.delete_webhook(ref.external_id)
        except ProviderRejected as error:
            if error.status != NOT_FOUND:
                raise
            action = GONE
        ref.superseded_at = timezone.now()
        ref.save(update_fields=["superseded_at"])
        lines.append(Line(event, action, mask_identifier(ref.external_id)))
    return lines

"""Outbound e-mails of every kind: the queue and the dispatch to their senders
(docs/qa/PLAN-012e-lease-end-transfer-installments.md D8).

:func:`queue` writes one ``queued`` :class:`~ledger.models.OutboundMessage` and its
``message_send`` job, in the caller's transaction: a collection e-mail (``ladder``, with
its step, :mod:`ledger.services.collections`) or a final statement (``final_statement``,
with its PDF as ``document``, :mod:`ledger.services.lease_end`). ``context`` names what
the message is about; the figures are read when it is rendered, by the worker, and the
body is never stored.

:func:`send` (the ``message_send`` job) hands each kind to its sender. Each sender
delivers through :func:`mail.get_sender`, so mail off records ``suppressed`` and,
outside production, every envelope goes to the catch-all address with its attachment.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from django.contrib.auth.models import User

from ledger.models import (
    CollectionStep,
    Document,
    Lease,
    MessageKind,
    MessageTemplate,
    Operator,
    OutboundMessage,
)
from payments import jobs, lease_sync
from payments.models import JobKind


@dataclass(frozen=True, slots=True)
class MessageContext:
    """What a message is about: a charge of the ladder, or a lease end."""

    entity_type: str
    entity_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class Sent:
    ok: bool
    message: str


def queue(
    kind: str,
    operator: Operator,
    template: MessageTemplate,
    context: MessageContext,
    *,
    lease: Lease | None,
    document: Document | None = None,
    actor: User | None = None,
    step: CollectionStep | None = None,
    manual: bool = False,
) -> OutboundMessage:
    """One queued message and its job. The database refuses a second ladder row of a
    (charge, step), a second final statement of a lease end, a ladder row without a step
    and a document on anything but a final statement (``IntegrityError``); the caller
    holds the transaction. The address is the billing e-mail of ``lease``, the lease that
    bills the charge (PLAN-014 C5); none without one."""
    message = OutboundMessage.objects.create(
        operator=operator,
        template=template,
        kind=MessageKind(kind),
        step=step,
        charge_entity_type=context.entity_type,
        charge_entity_id=context.entity_id,
        lease=lease,
        document=document,
        to_address=lease_sync.billing_of(lease).email if lease is not None else "",
        manual=manual,
        sent_by=actor,
    )
    jobs.enqueue(JobKind.MESSAGE_SEND, message, None, actor)
    return message


def send(message_id: uuid.UUID, today: date | None = None) -> Sent:
    """The ``message_send`` job: render, deliver and record one queued message of any
    kind."""
    from ledger.services import collections, lease_end

    kind = OutboundMessage.objects.filter(pk=message_id).values_list("kind", flat=True).first()
    if kind == MessageKind.FINAL_STATEMENT:
        outcome = lease_end.send_statement(message_id)
        return Sent(outcome.ok, outcome.message)
    ladder = collections.send(message_id, today)
    return Sent(ladder.ok, ladder.message)

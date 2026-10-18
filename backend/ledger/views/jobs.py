"""Queueing payment-provider work from the screens, and showing its state
(docs/qa/PLAN-010-worker-and-reconciliation.md D3).

No view calls the provider. A view that needs it calls :func:`submit` after its local
write; the ``worker`` process runs the job. The entity's page shows its latest job
through :func:`job_context`; while that job is queued or running the page refreshes
itself every five seconds (``job_refresh``, read by ``finance/base.html``).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from django.contrib import messages
from django.http import HttpRequest

from ledger.wording import provider_sentence
from payments import jobs
from payments.models import Job

REPLAY_REFUSED = "This form was already used for another request. Nothing was sent."
BUSY = provider_sentence("A change is still in progress in {provider}: try again in a minute.")


def submit(
    request: HttpRequest,
    kind: str,
    entity: jobs.Entity,
    params: Mapping[str, object] | None = None,
    *,
    replay_key: uuid.UUID | None = None,
) -> Job | None:
    """Queue one job for ``entity``; ``None`` (with an error message) when refused."""
    try:
        return jobs.enqueue(kind, entity, params, request.user, replay_key)
    except jobs.ReplayKeyUsed:
        messages.error(request, REPLAY_REFUSED)
        return None


def already_submitted(replay_key: uuid.UUID) -> bool:
    """Whether a job was already queued with this form's one-time key."""
    return Job.objects.filter(replay_key=replay_key).exists()


def busy(request: HttpRequest, entity: jobs.Entity, kinds: Iterable[str] | None = None) -> bool:
    """True, with a warning, while the entity's latest job (of ``kinds``, see
    :func:`payments.jobs.latest_for`) is queued or running."""
    if jobs.is_active(jobs.latest_for(entity, kinds)):
        messages.warning(request, BUSY)
        return True
    return False


def job_context(entity: jobs.Entity, kinds: Iterable[str] | None = None) -> dict[str, Any]:
    """The latest job of ``entity`` (of ``kinds``, see :func:`payments.jobs.latest_for`)
    for its page."""
    job = jobs.latest_for(entity, kinds)
    return {
        "job": job,
        "job_text": jobs.describe(job) if job is not None else "",
        "job_refresh": jobs.is_active(job),
    }

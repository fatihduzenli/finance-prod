"""Running queued jobs in tests (docs/qa/PLAN-010-worker-and-reconciliation.md D2, D3).

Views queue provider work; the ``worker`` process runs it. :func:`run_queued_jobs` does
what the worker's job half does, through the same :func:`payments.jobs.claim_next` and
:func:`payments.jobs.run`. The :func:`worker_between_requests` fixture runs it after
every test-client request, as if the worker had run between the POST and the next
page load, so the screen tests written before PLAN-010 keep checking the outcome
staff see. The queued state itself is tested in ``test_jobs.py`` without it.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.test import Client

from payments import jobs
from payments.models import Job


def run_queued_jobs() -> list[Job]:
    """Run every queued job, oldest first; the jobs run."""
    done: list[Job] = []
    while (job := jobs.claim_next()) is not None:
        done.append(jobs.run(job))
    return done


@pytest.fixture
def worker_between_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    original = Client.request

    def request(self: Client, **values: Any) -> Any:
        response = original(self, **values)
        run_queued_jobs()
        return response

    monkeypatch.setattr(Client, "request", request)

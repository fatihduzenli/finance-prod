"""Where the breaker and the failure budget keep their state (PLAN-005 D9).

One ``sync_state`` row per component, keyed by ``job_name``, with the state as JSON in
``cursor``. The rows survive a restart and are readable by a future dashboard. The
JSON holds only states, counters and ISO timestamps: nothing identifying, nothing a
card-number guard could object to.
"""

from __future__ import annotations

from typing import Protocol

from payments.models import SyncState

BREAKER_ROW = "stax_breaker"
BUDGET_ROW = "stax_failure_budget"


class StateStore(Protocol):
    def load(self, name: str) -> str | None: ...

    def save(self, name: str, text: str) -> None: ...


class SyncStateStore:
    """The production store: ``payments.SyncState`` rows."""

    def load(self, name: str) -> str | None:
        row = SyncState.objects.filter(job_name=name).first()
        return row.cursor if row is not None and row.cursor else None

    def save(self, name: str, text: str) -> None:
        SyncState.objects.update_or_create(job_name=name, defaults={"cursor": text})


class MemoryStateStore:
    """A store for tests that do not exercise persistence."""

    def __init__(self) -> None:
        self.rows: dict[str, str] = {}

    def load(self, name: str) -> str | None:
        return self.rows.get(name)

    def save(self, name: str, text: str) -> None:
        self.rows[name] = text

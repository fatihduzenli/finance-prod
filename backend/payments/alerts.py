"""Alerts that must not be missed (PLAN-005 D10, PLAN-008 D8).

:func:`breaker_opened` is called by the Stax circuit breaker exactly once per opening,
after the breaker has written its ``sync_state`` row. It logs at CRITICAL and sends a
Sentry event with level ``fatal`` and a fixed fingerprint, on which the owner's Sentry
alert rule pages by e-mail. Without a Sentry DSN the event is dropped by the SDK and
the log line and the ``sync_state`` row remain the only signals.

:func:`webhook_dead_lettered` is called once when a webhook event enters dead letter
after its third failed attempt, the same way: CRITICAL, then a ``fatal`` Sentry event
with its own fixed fingerprint. Neither carries the payload, the error or the token.

:func:`reconciliation_mismatches` and :func:`reconciliation_failed` (PLAN-010 D8) are the
reconciliation's two: one ``fatal`` event per run that found at least one new mismatch,
carrying the number of new mismatches of each kind, and one per failed run, carrying the
step reached and the error's class name. Never an identifier, an amount or a name.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime

import sentry_sdk

BREAKER_FINGERPRINT = "stax-breaker-open"
BREAKER_MESSAGE = "Payment provider circuit breaker opened"
DEAD_LETTER_FINGERPRINT = "webhook-dead-letter"
DEAD_LETTER_MESSAGE = "Webhook event moved to dead letter"
MISMATCH_FINGERPRINT = "reconciliation-mismatch"
#: Plain text, not lazy: written to the log and to Sentry, never shown on a page.
MISMATCH_MESSAGE = "Nightly check found new findings"
FAILED_FINGERPRINT = "reconciliation-failed"
FAILED_MESSAGE = "Nightly check failed"

logger = logging.getLogger(__name__)


def breaker_opened(opened_at: datetime, reopen_at: datetime) -> None:
    logger.critical(
        "%s at %s; calls refused until %s",
        BREAKER_MESSAGE,
        opened_at.isoformat(),
        reopen_at.isoformat(),
    )
    sentry_sdk.capture_message(BREAKER_MESSAGE, level="fatal", fingerprint=[BREAKER_FINGERPRINT])


def webhook_dead_lettered(event_id: str, event_type: str, attempts: int) -> None:
    logger.critical(
        "%s: event %s, type %s, after %d failed attempts",
        DEAD_LETTER_MESSAGE,
        event_id,
        event_type,
        attempts,
    )
    sentry_sdk.capture_message(
        DEAD_LETTER_MESSAGE, level="fatal", fingerprint=[DEAD_LETTER_FINGERPRINT]
    )


def reconciliation_mismatches(new_by_kind: Mapping[str, int]) -> None:
    """``new_by_kind``: mismatch kind -> number of new mismatches of that kind."""
    counts = {str(kind): int(number) for kind, number in sorted(new_by_kind.items())}
    summary = ", ".join(f"{kind}={number}" for kind, number in counts.items())
    logger.critical("%s: %s", MISMATCH_MESSAGE, summary)
    sentry_sdk.capture_message(
        MISMATCH_MESSAGE,
        level="fatal",
        fingerprint=[MISMATCH_FINGERPRINT],
        extras={"new_mismatches_by_kind": counts},
    )


def reconciliation_failed(step_reached: str, error_class: str) -> None:
    logger.critical("%s at step %s: %s", FAILED_MESSAGE, step_reached or "-", error_class)
    sentry_sdk.capture_message(
        FAILED_MESSAGE,
        level="fatal",
        fingerprint=[FAILED_FINGERPRINT],
        extras={"step_reached": step_reached, "error": error_class},
    )

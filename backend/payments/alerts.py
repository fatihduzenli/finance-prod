"""Alerts that must not be missed (PLAN-005 D10, PLAN-008 D8).

:func:`breaker_opened` is called by the Stax circuit breaker exactly once per opening,
after the breaker has written its ``sync_state`` row. It logs at CRITICAL and sends a
Sentry event with level ``fatal`` and a fixed fingerprint, on which the owner's Sentry
alert rule pages by e-mail. Without a Sentry DSN the event is dropped by the SDK and
the log line and the ``sync_state`` row remain the only signals.

:func:`webhook_dead_lettered` is called once when a webhook event enters dead letter
after its third failed attempt, the same way: CRITICAL, then a ``fatal`` Sentry event
with its own fixed fingerprint. Neither carries the payload, the error or the token.
"""

from __future__ import annotations

import logging
from datetime import datetime

import sentry_sdk

BREAKER_FINGERPRINT = "stax-breaker-open"
BREAKER_MESSAGE = "Payment provider circuit breaker opened"
DEAD_LETTER_FINGERPRINT = "webhook-dead-letter"
DEAD_LETTER_MESSAGE = "Webhook event moved to dead letter"

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

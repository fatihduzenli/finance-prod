"""The signed statement link of the collection e-mails (docs/qa/PLAN-011b D4).

The ``{statement_link}`` merge field names a page the operator can open without an
account: ``/statement/<token>/``. The token is a :class:`django.core.signing.TimestampSigner`
value with a salt of its own (:data:`SALT`) carrying the operator's id: tampering breaks
the signature, and :func:`operator_for` refuses a token older than :data:`MAX_AGE_DAYS`
days. Billing revokes every link of an operator at once (:func:`revoke`): a token issued
before ``operators.statement_links_revoked_at`` (or in the same second) is refused. A
refused token is "no operator", whatever the reason; nothing here logs a token.

What the page shows is :func:`ledger.services.reports.statement` for the last
:data:`HISTORY_MONTHS` months; every open of the page is audited with the operator's id
only (:func:`record_open`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.core import signing
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from ledger.models import AuditLog, Operator
from ledger.services import reports
from ledger.services.pnl import add_months

#: The signer's own salt: no other signed value of the app is a statement token.
SALT = "ledger.statement-link"
MAX_AGE_DAYS = 30
#: Payment history shown: this month and the eleven before.
HISTORY_MONTHS = 12
#: A token longer than this is refused before any work (an id, a timestamp, a signature).
TOKEN_MAX_LENGTH = 200
ENTITY_OPERATOR = "operator"
OPENED_ACTION = "statement_link.opened"
REVOKED_ACTION = "operator.statement_links_revoked"

REVOKED = (
    "Every statement link sent to this operator so far no longer works. The next collection "
    "e-mail carries a new one."
)


@dataclass(frozen=True, slots=True)
class Outcome:
    ok: bool
    message: str


def _signer() -> signing.TimestampSigner:
    return signing.TimestampSigner(salt=SALT)


def make_token(operator: Operator) -> str:
    """A new signed token for ``operator``, valid :data:`MAX_AGE_DAYS` days."""
    return _signer().sign(str(operator.pk))


def link(operator: Operator) -> str:
    """The absolute URL of a new signed statement link for ``operator``."""
    path = reverse("ledger:statement_link", kwargs={"token": make_token(operator)})
    return f"{str(settings.APP_BASE_URL).rstrip('/')}{path}"


def _issued_at(token: str) -> int | None:
    """The signing time (whole seconds) of a token whose signature was checked."""
    parts = token.rsplit(_signer().sep, 2)
    if len(parts) != 3:
        return None
    try:
        return signing.b62_decode(parts[1])
    except ValueError:
        return None


def operator_for(token: str) -> Operator | None:
    """The operator a valid, unexpired, unrevoked token names; ``None`` otherwise."""
    if not token or len(token) > TOKEN_MAX_LENGTH:
        return None
    try:
        value = _signer().unsign(token, max_age=timedelta(days=MAX_AGE_DAYS))
        key = uuid.UUID(value)
    except (signing.BadSignature, ValueError):
        return None
    issued = _issued_at(token)
    if issued is None:
        return None
    operator = Operator.objects.filter(pk=key).first()
    if operator is None:
        return None
    revoked = operator.statement_links_revoked_at
    if revoked is not None and issued <= revoked.timestamp():
        return None
    return operator


def history_range(today: date) -> reports.ReportRange:
    """This month and the :data:`HISTORY_MONTHS` - 1 before it."""
    month = today.replace(day=1)
    return reports.ReportRange(add_months(month, -(HISTORY_MONTHS - 1)), month)


def statement(operator: Operator, today: date) -> reports.Statement:
    return reports.statement(operator, history_range(today), today)


def record_open(operator: Operator) -> None:
    """Audit one open of the page: the operator's id, never the token."""
    AuditLog.objects.create(
        actor=None,
        action=OPENED_ACTION,
        entity_type=ENTITY_OPERATOR,
        entity_id=str(operator.pk),
        before=None,
        after={"operator": str(operator.pk)},
    )


def revoke(operator_id: uuid.UUID, *, actor: User | None, now: datetime | None = None) -> Outcome:
    """Refuse every link issued so far (billing; an archived operator too), audited."""
    with transaction.atomic():
        operator = Operator.objects.select_for_update().get(pk=operator_id)
        before = operator.statement_links_revoked_at
        operator.statement_links_revoked_at = now or timezone.now()
        operator.save(update_fields=["statement_links_revoked_at", "updated_at"])
        AuditLog.objects.create(
            actor=actor,
            action=REVOKED_ACTION,
            entity_type=ENTITY_OPERATOR,
            entity_id=str(operator.pk),
            before={"statement_links_revoked_at": before.isoformat() if before else None},
            after={"statement_links_revoked_at": operator.statement_links_revoked_at.isoformat()},
        )
    return Outcome(True, REVOKED)

"""Credits held on a lease (docs/qa/PLAN-012d-store-workspace.md D6b, ADR-0013).

Batch A of PLAN-012d reads the figure only, for the store page's header: there is no
credit yet, so :func:`balance` answers zero. Batch B adds the ``LeaseCredit`` table and
fills this module (the balance held, applying a credit, the credits held and applied).
"""

from __future__ import annotations

from decimal import Decimal

from ledger.models import Lease


def balance(lease: Lease) -> Decimal:
    """The credit held on ``lease``: what was received and not yet applied to a charge.
    Always ``Decimal("0.00")`` until Batch B of PLAN-012d fills it."""
    del lease
    return Decimal("0.00")

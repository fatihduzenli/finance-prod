"""Whether the payment provider may be called now, for screens (PLAN-007 §4.1, run page).

The ``ledger`` package never imports ``payments.stax``; it reads the circuit breaker and
the failure budget through :func:`provider_health`. Reading changes no state: an open
breaker is not moved to half-open and no failure is recorded.
"""

from __future__ import annotations

from dataclasses import dataclass

from payments.errors import FailureBudgetExhausted

BREAKER_OPEN = (
    "Calls to the payment provider are paused after repeated failures. "
    "Matching is unavailable for a few minutes."
)
BUDGET_EXHAUSTED = (
    "Too many recent payment provider failures: a debit would be refused now. "
    "Matching is unavailable until the failure count drops."
)


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    breaker_open: bool
    budget_exhausted: bool

    @property
    def ok(self) -> bool:
        return not (self.breaker_open or self.budget_exhausted)

    @property
    def messages(self) -> tuple[str, ...]:
        """Fixed sentences for a banner, one per problem."""
        return (BREAKER_OPEN,) * self.breaker_open + (BUDGET_EXHAUSTED,) * self.budget_exhausted


def provider_health() -> ProviderHealth:
    from payments.stax.breaker import get_breaker
    from payments.stax.budget import get_budget

    try:
        get_budget().check_can_debit()
    except FailureBudgetExhausted:
        exhausted = True
    else:
        exhausted = False
    return ProviderHealth(breaker_open=get_breaker().is_open(), budget_exhausted=exhausted)

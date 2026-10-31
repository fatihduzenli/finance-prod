"""The processor's hosted bank-account fields (docs/qa/PLAN-014 C7).

The bank-entry page (``ledger:leases:bank``) shows the processor's own fields for a bank
account. The browser sends the account to the processor, under the lease's customer, and
the app receives only the new **payment-method id**, which the worker verifies before it
links it (``JobKind.LEASE_METHOD_ATTACH``, ``ledger.services.leases.attach_bank_method``).
No account or routing number ever reaches this app (Rule 1).

:func:`get_hosted_fields` picks the implementation:

- ``STAX_WEB_PAYMENTS_TOKEN`` set: :class:`payments.stax.hosted.StaxHostedFields` (the
  processor's script from its documented CDN URL, TODO-VERIFY-066);
- ``STAX_PROVIDER=fake`` (local only, as :func:`payments.get_provider` gates it):
  :class:`payments.fake_hosted.FakeHostedFields`, a plain form with one field
  ``payment_method_id`` (local development and tests);
- otherwise :class:`NoHostedFields`: the page says bank entry is not set up here.

Imports are made inside :func:`get_hosted_fields` so that loading this module touches
neither the adapter nor the settings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True, slots=True)
class HostedFieldsConfig:
    """What the bank-entry page needs for one customer.

    ``script_url`` and ``provider_host`` are empty for the fake (no script is loaded and no
    Content-Security-Policy is needed). The token is publishable to the browser by the
    processor's design, but is handled as a secret everywhere else: it is kept out of
    ``repr`` and never logged.
    """

    script_url: str
    web_payments_token: str = field(repr=False)
    customer_external_id: str
    provider_host: str


class HostedFields(Protocol):
    def config_for(self, customer_external_id: str) -> HostedFieldsConfig | None:
        """The page's configuration for this customer; ``None`` when not set up."""
        ...

    def available(self) -> bool:
        """Whether bank-account entry is set up on this environment."""
        ...


class NoHostedFields:
    """Bank-account entry is not set up on this environment."""

    def config_for(self, customer_external_id: str) -> HostedFieldsConfig | None:
        return None

    def available(self) -> bool:
        return False


def get_hosted_fields() -> HostedFields:
    """The hosted fields this environment uses (see the module docstring)."""
    from django.conf import settings

    import payments

    if settings.STAX_WEB_PAYMENTS_TOKEN:
        from payments.stax.hosted import StaxHostedFields

        return StaxHostedFields(settings.STAX_WEB_PAYMENTS_TOKEN)
    if settings.STAX_PROVIDER == payments.FAKE:
        from payments.fake_hosted import FakeHostedFields

        return FakeHostedFields()
    return NoHostedFields()

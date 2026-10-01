"""``provider_ping``: one authentication check against the configured provider.

docs/qa/PLAN-005-stax-client.md §4.4 and D11. Calls ``ping()`` (GET /self) once, through
the real adapter and therefore through the caller check, breaker and limiter, and
prints only the provider name, the environment, the merchant ID masked to its first and
last four characters, and whether it matches ``STAX_EXPECTED_MERCHANT_ID``.

Exit codes: 0 success, 1 identity mismatch, 2 any other failure. There is no retry
loop: a failed authentication is reported once, and only the class of the failure is
printed, never a message from the provider.

On staging the web container runs with ``STAX_CALLER=worker`` and one replica (since
PLAN-006), so the command runs there as it is; setting the variable for this one
process, as below, also works where the container runs with ``none``::

    az containerapp exec -g finance-sandbox -n finance-staging-web --command "sh -c 'STAX_CALLER=worker python backend/manage.py provider_ping'"
"""  # noqa: E501 (the exact staging command, kept on one line to copy)

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand

from payments.errors import IdentityMismatch
from payments.provider import mask_identifier
from payments.stax.adapter import PROVIDER_NAME, StaxProvider

EXIT_MISMATCH = 1
EXIT_FAILURE = 2


class Command(BaseCommand):
    help = "Check the payment provider credential once (GET /self) and report the identity."

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            identity = StaxProvider().ping()
        except IdentityMismatch as mismatch:
            self._report(mask_identifier(mismatch.merchant_id), "no")
            raise SystemExit(EXIT_MISMATCH) from None
        except Exception as error:  # any other failure: exit 2, class name only
            self.stderr.write(f"provider_ping failed: {type(error).__name__}")
            raise SystemExit(EXIT_FAILURE) from None
        if identity.matches_expected is None:
            matches = "not set"
        else:
            matches = "yes" if identity.matches_expected else "no"
        self._report(mask_identifier(identity.merchant_id), matches)

    def _report(self, masked_merchant_id: str, matches: str) -> None:
        self.stdout.write(f"provider: {PROVIDER_NAME}")
        self.stdout.write(f"environment: {settings.STAX_ENV}")
        self.stdout.write(f"merchant: {masked_merchant_id}")
        self.stdout.write(f"merchant matches expected: {matches}")

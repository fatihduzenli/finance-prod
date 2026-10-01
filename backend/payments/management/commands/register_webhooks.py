"""``register_webhooks``: register this app's webhooks with the provider (PLAN-008 D9).

Run inside the staging container (``STAX_CALLER=worker``). Lists the registrations the
provider holds, creates only the missing ones for the events of PLAN-008 D10, and
stores every registration ID. Refused when ``APP_ENV`` is ``local``. Prints event names
and masked registration IDs only: never the target URL, which holds the token.

Exit codes: 0 success, 1 refused by the configuration, 2 provider failure (class name
only; IDs stored before the failure are kept, and a rerun creates only what is missing).
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from payments.errors import ProviderError
from payments.webhooks import registration

EXIT_REFUSED = 1
EXIT_FAILURE = 2


class Command(BaseCommand):
    help = "Register the app's webhooks with the payment provider (creates only missing ones)."

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            lines = registration.register()
        except registration.RegistrationRefused as refused:
            self.stderr.write(str(refused))
            raise SystemExit(EXIT_REFUSED) from None
        except ProviderError as error:
            self.stderr.write(f"register_webhooks failed: {type(error).__name__}")
            raise SystemExit(EXIT_FAILURE) from None
        for line in lines:
            self.stdout.write(f"{line.event}: {line.action} ({line.registration})")
        created = sum(1 for line in lines if line.action == registration.CREATED)
        self.stdout.write(f"Registrations: {len(lines)}. Created: {created}.")

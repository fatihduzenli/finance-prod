"""``unregister_webhooks``: delete this app's webhook registrations (PLAN-008 D9).

Deletes, at the provider, every registration whose ID ``register_webhooks`` stored, and
retires the stored reference; one the provider no longer has is reported as already
gone. Refused when ``APP_ENV`` is ``local``. Prints event names and masked IDs only.

Exit codes: 0 success, 1 refused by the configuration, 2 provider failure (class name
only; registrations deleted before the failure stay deleted).
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from payments.errors import ProviderError
from payments.webhooks import registration

EXIT_REFUSED = 1
EXIT_FAILURE = 2


class Command(BaseCommand):
    help = "Delete the app's webhook registrations at the payment provider, by stored ID."

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            lines = registration.unregister()
        except registration.RegistrationRefused as refused:
            self.stderr.write(str(refused))
            raise SystemExit(EXIT_REFUSED) from None
        except ProviderError as error:
            self.stderr.write(f"unregister_webhooks failed: {type(error).__name__}")
            raise SystemExit(EXIT_FAILURE) from None
        for line in lines:
            self.stdout.write(f"{line.event}: {line.action} ({line.registration})")
        self.stdout.write(f"Registrations removed: {len(lines)}.")

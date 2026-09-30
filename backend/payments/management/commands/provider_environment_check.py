"""``provider_environment_check``: refuse to start when the Stax key disagrees with us.

docs/qa/PLAN-006-operators.md D2, docs/CLAUDE.md Rule 6. Runs in the image ``CMD``
after ``deploy_migrate`` and before gunicorn. Sends ``GET /self`` once per process
start through the transport's boot path (``boot_check=True``), which skips only the
``STAX_CALLER`` gate; the breaker, limiter, budget, retry policy and logging still
apply. The command itself never repeats the request.

Exit codes, every non-zero one stopping the container from starting:

- 0: ``merchant.is_sandbox`` agrees with ``STAX_ENV`` and, when
  ``STAX_EXPECTED_MERCHANT_ID`` is set, the merchant is the expected one. Also 0, with
  "skipped", when ``APP_ENV=local`` and no key is configured.
- 1: the merchant is not ``STAX_EXPECTED_MERCHANT_ID``.
- 2: Stax could not be asked (unreachable, refused, unreadable answer, no key).
- 3: ``merchant.is_sandbox`` disagrees with ``STAX_ENV``, or is missing: an unknown
  environment is not agreement.

Output is the environment, the merchant ID masked to its first and last four
characters, the sandbox flag and the verdict. On failure only the class name of the
error is printed, never a message or body from the provider.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NoReturn

from django.conf import settings
from django.core.management.base import BaseCommand

from payments.provider import mask_identifier
from payments.stax import mapping
from payments.stax.adapter import SELF
from payments.stax.transport import Method, StaxTransport

EXIT_OK = 0
EXIT_MERCHANT_MISMATCH = 1
EXIT_UNREACHABLE = 2
EXIT_ENVIRONMENT_MISMATCH = 3
SANDBOX = "sandbox"
LOCAL = "local"


def is_sandbox_flag(data: object) -> bool | None:
    """``merchant.is_sandbox`` of a GET /self response, or ``None`` when absent.

    Field observed on staging on 2026-09-30 (TODO-VERIFY-005, closed). Anything but a
    JSON boolean counts as absent.
    """
    if not isinstance(data, Mapping):
        return None
    merchant = data.get("merchant")
    if not isinstance(merchant, Mapping):
        return None
    flag = merchant.get("is_sandbox")
    return flag if isinstance(flag, bool) else None


class Command(BaseCommand):
    help = "Refuse to start unless the provider key's environment and merchant match ours."

    def handle(self, *args: Any, **options: Any) -> None:
        if not settings.STAX_API_KEY:
            if settings.APP_ENV == LOCAL:
                self.stdout.write("provider environment check: skipped (local, no key)")
                return
            self._fail("provider environment check failed: no key", EXIT_UNREACHABLE)

        try:
            data = StaxTransport().send(Method.GET, SELF, idempotent=True, boot_check=True)
            merchant_id = mapping.merchant_id(data)
        except Exception as error:  # any failure to ask: exit 2, class name only
            self._fail(
                f"provider environment check failed: {type(error).__name__}", EXIT_UNREACHABLE
            )

        flag = is_sandbox_flag(data)
        masked = mask_identifier(merchant_id)
        self.stdout.write(f"environment: {settings.STAX_ENV}")
        self.stdout.write(f"merchant: {masked}")
        self.stdout.write(f"merchant is_sandbox: {'unknown' if flag is None else flag}")

        if flag is None or flag != (settings.STAX_ENV == SANDBOX):
            self._fail(
                "provider environment check failed: the key's environment does not match STAX_ENV",
                EXIT_ENVIRONMENT_MISMATCH,
            )
        expected = settings.STAX_EXPECTED_MERCHANT_ID
        if expected and merchant_id != expected:
            self._fail(
                "provider environment check failed: the key belongs to a different merchant "
                "than STAX_EXPECTED_MERCHANT_ID",
                EXIT_MERCHANT_MISMATCH,
            )
        self.stdout.write("merchant matches expected: " + ("yes" if expected else "not set"))
        self.stdout.write("provider environment check: ok")

    def _fail(self, message: str, code: int) -> NoReturn:
        self.stderr.write(message)
        raise SystemExit(code)

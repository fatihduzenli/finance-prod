"""One error hierarchy for every payment-provider call (docs/qa/PLAN-005-stax-client.md §4.1).

Messages are value-free by construction: none of them carries a request or response
body, a header, an amount, an e-mail address or a credential. Callers may log
``type(error).__name__`` and ``str(error)`` without leaking anything (docs/CLAUDE.md
Rule 1, root ``CLAUDE.md`` §5.2).
"""

from __future__ import annotations


class ProviderError(Exception):
    """Base class of every error raised through the ``PaymentProvider`` interface."""


class ValidationFailed(ProviderError):  # noqa: N818 (names fixed by PLAN-005 §4.1)
    """The request was refused locally and never sent."""


class ProviderNotPermittedHere(ProviderError):  # noqa: N818 (names fixed by PLAN-005 §4.1)
    """This process may not call the provider (``STAX_CALLER`` is not ``worker``, D7)."""


class FeatureDisabled(ProviderError):  # noqa: N818 (names fixed by PLAN-005 §4.1)
    """The method is behind a flag that is off (D3)."""


class CircuitOpen(ProviderError):  # noqa: N818 (names fixed by PLAN-005 §4.1)
    """The circuit breaker is open; the call was refused without touching the network."""


class FailureBudgetExhausted(ProviderError):  # noqa: N818 (names fixed by PLAN-005 §4.1)
    """Too many recent failures to risk a debit (D8)."""


class RateLimitWaitExceeded(ProviderError):  # noqa: N818 (names fixed by PLAN-005 §4.1)
    """The rate limiter could not release a slot within the allowed wait."""


class ProviderRejected(ProviderError):  # noqa: N818 (names fixed by PLAN-005 §4.1)
    """The provider answered with a 4xx other than 429. Never retried.

    ``error_fields`` holds the key names of the error body, reduced to a safe alphabet
    by the transport (PLAN-006a §3.1); never a value. It is not part of the message.
    """

    def __init__(
        self,
        status: int,
        message: str = "The provider rejected the request",
        *,
        error_fields: tuple[str, ...] = (),
    ) -> None:
        super().__init__(f"{message} (status {status})")
        self.status = status
        self.error_fields = error_fields


class ProviderUnavailable(ProviderError):  # noqa: N818 (names fixed by PLAN-005 §4.1)
    """5xx or 429 after the allowed retries, or a response this client cannot read."""

    def __init__(self, status: int | None, message: str = "The provider is unavailable") -> None:
        suffix = f" (status {status})" if status is not None else ""
        super().__init__(f"{message}{suffix}")
        self.status = status


class TransportError(ProviderUnavailable):
    """No HTTP response: connection failure or timeout.

    A subclass of :class:`ProviderUnavailable` so that "the provider could not be
    reached after the allowed retries" is caught the same way whatever the cause.
    """

    def __init__(self, message: str = "The provider could not be reached") -> None:
        super().__init__(None, message)


class IdentityMismatch(ProviderError):  # noqa: N818 (names fixed by PLAN-005 §4.1)
    """The credential belongs to a different merchant than expected (D11, Rule 6).

    ``merchant_id`` is the identifier the provider returned. It is kept out of the
    message; whoever displays it masks it first (see ``provider.mask_identifier``).
    """

    def __init__(self, merchant_id: str) -> None:
        super().__init__("The provider credential belongs to a different merchant than expected")
        self.merchant_id = merchant_id

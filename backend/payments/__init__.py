"""The PaymentProvider interface and the Stax adapter (docs/CLAUDE.md Rule 8).

:func:`get_provider` is how the rest of the app obtains a provider. It returns the Stax
adapter unless ``STAX_PROVIDER=fake`` (accepted by the environment guard only when
``APP_ENV=local``), in which case it returns the in-memory provider. Imports are made
inside the function so that loading this package, which Django does at start-up, touches
neither the adapter nor the database.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from payments.provider import PaymentProvider

FAKE = "fake"

_lock = threading.Lock()
_providers: dict[str, PaymentProvider] = {}


def get_provider() -> PaymentProvider:
    """The process-wide provider selected by ``STAX_PROVIDER``.

    One instance per process: the adapter keeps one HTTP client, and the in-memory
    provider keeps its records between requests.
    """
    from django.conf import settings

    kind: str = settings.STAX_PROVIDER
    with _lock:
        provider = _providers.get(kind)
        if provider is None:
            provider = _build(kind)
            _providers[kind] = provider
        return provider


def _build(kind: str) -> PaymentProvider:
    if kind == FAKE:
        from payments.fake import FakeProvider

        return FakeProvider()
    from payments.stax.adapter import StaxProvider

    return StaxProvider()

"""Document storage (docs/qa/PLAN-010c-documents-and-cost-corrections.md D2, D3).

The one package that talks to Blob Storage. The rest of the app uses
:func:`get_storage` and the :class:`DocumentStorage` interface; nothing outside this
package imports the Azure SDK (``backend/tests/test_documents_storage.py``).

:func:`get_storage` returns:

- :class:`~documents_storage.azure_blob.AzureBlobDocumentStorage` when
  ``DOCUMENTS_STORAGE_ACCOUNT_URL`` is set. It authenticates with the app's
  user-assigned managed identity only: no account key, no connection string, no SAS.
- :class:`~documents_storage.memory.MemoryDocumentStorage` when the URL is empty and
  ``APP_ENV=local`` (local development and the test suite).
- ``None`` when the URL is empty anywhere else: the documents feature is off, every
  upload is refused and the pages say so; the app still boots (D2).

One instance per process, built on first use. Imports are made inside the function so
that loading this package touches neither the SDK nor the settings.
"""

from __future__ import annotations

import threading

from documents_storage.base import DocumentStorage, StorageUnavailable

__all__ = ["DocumentStorage", "StorageUnavailable", "get_storage"]

LOCAL = "local"

_lock = threading.Lock()
_storages: dict[str, DocumentStorage] = {}


def get_storage() -> DocumentStorage | None:
    """The process-wide document storage, or ``None`` when the feature is off."""
    from django.conf import settings

    account_url: str = settings.DOCUMENTS_STORAGE_ACCOUNT_URL
    if account_url:
        key = f"azure:{account_url}:{settings.DOCUMENTS_CONTAINER}"
    elif settings.APP_ENV == LOCAL:
        key = "memory"
    else:
        return None
    with _lock:
        storage = _storages.get(key)
        if storage is None:
            storage = _build(account_url)
            _storages[key] = storage
        return storage


def _build(account_url: str) -> DocumentStorage:
    from django.conf import settings

    if not account_url:
        from documents_storage.memory import MemoryDocumentStorage

        return MemoryDocumentStorage()
    from documents_storage.azure_blob import AzureBlobDocumentStorage

    return AzureBlobDocumentStorage(
        account_url=account_url,
        container=settings.DOCUMENTS_CONTAINER,
        client_id=settings.AZURE_CLIENT_ID,
    )

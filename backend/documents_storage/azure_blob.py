"""Azure Blob Storage behind :class:`~documents_storage.base.DocumentStorage`
(PLAN-010c D1, D2, D3).

The client authenticates with the app's user-assigned managed identity
(``ManagedIdentityCredential(client_id=AZURE_CLIENT_ID)``) and nothing else: the storage
account has shared-key access disabled, and this module never takes an account key, a
connection string or a shared access signature (enforced by a static test). Nothing
here mints, reads or logs a token; the SDK does that at runtime.

Every SDK error becomes :class:`~documents_storage.base.StorageUnavailable` carrying the
error's class name only, raised without its cause so no SDK message (which can hold the
account URL or a request ID) travels further.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

from azure.core.exceptions import AzureError
from azure.identity import ManagedIdentityCredential
from azure.storage.blob import BlobServiceClient, ContentSettings

from documents_storage.base import StorageUnavailable

__all__ = ["AzureBlobDocumentStorage", "AzureError"]

logger = logging.getLogger(__name__)

# Time limits (PLAN-010c finding 4 on Batch A). An upload holds the owner's row lock
# and a database transaction across ``put`` (the lock is what stops a duplicate race),
# so one ``put`` must end in bounded time: about 30 seconds at worst.
#
# - No SDK retries, for storage requests or for the token request: a retry would
#   multiply every limit below (the storage SDK's default is 3 retries with a 15-second
#   back-off). A failed ``put`` writes no row; the user tries again.
# - Storage: 5 seconds to connect, 15 seconds of silence on the socket at most, and the
#   service ends any one operation after 20 seconds (``timeout``, server side). A
#   stalled ``put`` therefore fails after at most about 5 + 20 seconds.
# - Token (only when the cached one has expired): 3 seconds to connect and 5 seconds of
#   silence, one attempt: at most about 8 seconds more.

#: Seconds to open a connection to the storage endpoint.
CONNECTION_TIMEOUT = 5
#: Seconds the socket may stay silent while sending or receiving.
READ_TIMEOUT = 15
#: Retries of a failed request by the storage SDK's retry policy: none.
RETRY_TOTAL = 0
#: Server-side limit for one operation, in seconds.
OPERATION_TIMEOUT = 20
#: The same limits for the managed identity's token request.
TOKEN_CONNECTION_TIMEOUT = 3
TOKEN_READ_TIMEOUT = 5
TOKEN_RETRY_TOTAL = 0


class AzureBlobDocumentStorage:
    """One private container of one storage account, reached by managed identity."""

    def __init__(self, *, account_url: str, container: str, client_id: str) -> None:
        credential = ManagedIdentityCredential(
            client_id=client_id,
            connection_timeout=TOKEN_CONNECTION_TIMEOUT,
            read_timeout=TOKEN_READ_TIMEOUT,
            retry_total=TOKEN_RETRY_TOTAL,
        )
        service = BlobServiceClient(
            account_url,
            credential=credential,
            connection_timeout=CONNECTION_TIMEOUT,
            read_timeout=READ_TIMEOUT,
            retry_total=RETRY_TOTAL,
        )
        self._container = service.get_container_client(container)

    def put(self, path: str, data: bytes, content_type: str) -> None:
        try:
            self._container.upload_blob(
                name=path,
                data=data,
                overwrite=False,
                content_settings=ContentSettings(content_type=content_type),
                timeout=OPERATION_TIMEOUT,
            )
        except AzureError as error:
            raise _unavailable("put", error) from None

    def open(self, path: str) -> Iterator[bytes]:
        try:
            downloader = self._container.get_blob_client(path).download_blob(
                timeout=OPERATION_TIMEOUT
            )
        except AzureError as error:
            raise _unavailable("open", error) from None
        return _guarded_chunks(downloader.chunks())

    def exists(self, path: str) -> bool:
        try:
            return bool(self._container.get_blob_client(path).exists(timeout=OPERATION_TIMEOUT))
        except AzureError as error:
            raise _unavailable("exists", error) from None


def _guarded_chunks(chunks: Iterator[bytes]) -> Iterator[bytes]:
    """The download's chunks; a failure part-way raises StorageUnavailable as well."""
    try:
        yield from chunks
    except AzureError as error:
        raise _unavailable("read", error) from None


def _unavailable(operation: str, error: AzureError) -> StorageUnavailable:
    name = type(error).__name__
    logger.warning("Document storage %s failed: %s", operation, name)
    return StorageUnavailable(name)

"""In-memory :class:`~documents_storage.base.DocumentStorage` for tests and
``APP_ENV=local`` only (PLAN-010c D2). Process-local; lost on restart."""

from __future__ import annotations

import threading
from collections.abc import Iterator

from documents_storage.base import StorageUnavailable

CHUNK_SIZE = 64 * 1024


class MemoryDocumentStorage:
    """Every stored blob is kept in :attr:`blobs` as ``path -> (bytes, content type)``.

    Tests may set :attr:`fail_put` or :attr:`fail_open` to make the next calls raise
    :class:`StorageUnavailable`, as the real storage would when it is unreachable.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.blobs: dict[str, tuple[bytes, str]] = {}
        self.fail_put = False
        self.fail_open = False

    def put(self, path: str, data: bytes, content_type: str) -> None:
        with self._lock:
            if self.fail_put:
                raise StorageUnavailable("MemoryStorageFailure")
            if path in self.blobs:
                raise StorageUnavailable("BlobAlreadyExists")
            self.blobs[path] = (bytes(data), content_type)

    def open(self, path: str) -> Iterator[bytes]:
        with self._lock:
            if self.fail_open:
                raise StorageUnavailable("MemoryStorageFailure")
            stored = self.blobs.get(path)
        if stored is None:
            raise StorageUnavailable("BlobNotFound")
        return _chunks(stored[0])

    def exists(self, path: str) -> bool:
        with self._lock:
            return path in self.blobs


def _chunks(data: bytes) -> Iterator[bytes]:
    for start in range(0, len(data), CHUNK_SIZE):
        yield data[start : start + CHUNK_SIZE]

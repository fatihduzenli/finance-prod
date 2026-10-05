"""The storage interface and its one error (PLAN-010c D2)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol


class StorageUnavailable(Exception):  # noqa: N818 - the name is the contract (PLAN-010c §4)
    """The storage could not do what was asked.

    Carries the class name of the underlying error only, never its message: an SDK
    message can hold a URL, a request ID or a token audience, none of which belongs in
    a log line or a page.
    """

    def __init__(self, cause: str) -> None:
        super().__init__(cause)
        self.cause = cause


class DocumentStorage(Protocol):
    """Where document bytes live. Paths are built by the caller and never contain a
    filename; they are written once and never overwritten or deleted by the app."""

    def put(self, path: str, data: bytes, content_type: str) -> None:
        """Store ``data`` at ``path``. Refuses to overwrite an existing path."""
        ...

    def open(self, path: str) -> Iterator[bytes]:
        """The bytes at ``path``, in chunks. A missing path or an unreachable store
        raises :class:`StorageUnavailable` before the first chunk is returned."""
        ...

    def exists(self, path: str) -> bool:
        """Whether something is stored at ``path``."""
        ...

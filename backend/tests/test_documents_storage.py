"""The document storage package and its settings (docs/qa/PLAN-010c D1 to D3, §6).

- ``get_storage``: memory only when ``APP_ENV=local`` and the URL is empty; ``None``
  (feature off) when the URL is empty anywhere else; the Azure storage when it is set.
- The memory storage, and the Azure storage with its SDK container client replaced:
  no network is ever reached. SDK errors become ``StorageUnavailable`` with a class name.
- The fence: nothing outside ``backend/documents_storage/`` imports the Azure SDK, and the
  SDK is never given an account key, a connection string or a shared access signature.
- The environment guard for ``DOCUMENTS_STORAGE_ACCOUNT_URL``, ``DOCUMENTS_CONTAINER`` and
  ``AZURE_CLIENT_ID``.
"""

from __future__ import annotations

import ast
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from django.test import override_settings

import documents_storage
from documents_storage import StorageUnavailable, azure_blob
from documents_storage.memory import CHUNK_SIZE, MemoryDocumentStorage
from finance.env_guard import EnvGuardError, validate

BACKEND = Path(__file__).resolve().parents[1]
PACKAGE = BACKEND / "documents_storage"
#: Obviously fake identifiers (root CLAUDE.md §5.2).
CLIENT_ID = "00000000-0000-0000-0000-000000000000"
ACCOUNT_URL = "https://stexample000.blob.core.windows.net"


@pytest.fixture(autouse=True)
def _fresh_storages() -> Iterator[None]:
    documents_storage._storages.clear()
    yield
    documents_storage._storages.clear()


# --- get_storage --------------------------------------------------------------------------


def test_local_without_a_url_uses_the_memory_storage() -> None:
    with override_settings(APP_ENV="local", DOCUMENTS_STORAGE_ACCOUNT_URL=""):
        storage = documents_storage.get_storage()
        assert isinstance(storage, MemoryDocumentStorage)
        assert documents_storage.get_storage() is storage


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_a_deployment_without_a_url_has_the_feature_off(app_env: str) -> None:
    with override_settings(APP_ENV=app_env, DOCUMENTS_STORAGE_ACCOUNT_URL=""):
        assert documents_storage.get_storage() is None


@pytest.mark.parametrize("app_env", ["local", "staging"])
def test_a_url_selects_the_azure_storage_with_the_managed_identity(
    app_env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: dict[str, Any] = {}

    class FakeCredential:
        def __init__(self, **kwargs: Any) -> None:
            built["credential"] = kwargs

    class FakeService:
        def __init__(self, url: str, **kwargs: Any) -> None:
            built["url"] = url
            built["service"] = kwargs

        def get_container_client(self, name: str) -> str:
            built["container"] = name
            return "container-client"

    monkeypatch.setattr(azure_blob, "ManagedIdentityCredential", FakeCredential)
    monkeypatch.setattr(azure_blob, "BlobServiceClient", FakeService)
    with override_settings(
        APP_ENV=app_env,
        DOCUMENTS_STORAGE_ACCOUNT_URL=ACCOUNT_URL,
        DOCUMENTS_CONTAINER="documents",
        AZURE_CLIENT_ID=CLIENT_ID,
    ):
        storage = documents_storage.get_storage()
    assert isinstance(storage, azure_blob.AzureBlobDocumentStorage)
    assert built["credential"] == {
        "client_id": CLIENT_ID,
        "connection_timeout": azure_blob.TOKEN_CONNECTION_TIMEOUT,
        "read_timeout": azure_blob.TOKEN_READ_TIMEOUT,
        "retry_total": azure_blob.TOKEN_RETRY_TOTAL,
    }
    assert built["url"] == ACCOUNT_URL
    assert isinstance(built["service"].pop("credential"), FakeCredential)
    assert built["service"] == {
        "connection_timeout": azure_blob.CONNECTION_TIMEOUT,
        "read_timeout": azure_blob.READ_TIMEOUT,
        "retry_total": azure_blob.RETRY_TOTAL,
    }
    assert built["container"] == "documents"


def test_a_put_is_bounded_to_about_30_seconds() -> None:
    """Finding 4 on Batch A: the upload holds a row lock across ``put``, so the client
    has short timeouts and no retries, for storage and for the token alike."""
    assert (azure_blob.CONNECTION_TIMEOUT, azure_blob.READ_TIMEOUT) == (5, 15)
    assert azure_blob.OPERATION_TIMEOUT == 20
    assert azure_blob.RETRY_TOTAL == 0
    assert (azure_blob.TOKEN_CONNECTION_TIMEOUT, azure_blob.TOKEN_READ_TIMEOUT) == (3, 5)
    assert azure_blob.TOKEN_RETRY_TOTAL == 0
    worst = (
        azure_blob.CONNECTION_TIMEOUT
        + max(azure_blob.OPERATION_TIMEOUT, azure_blob.READ_TIMEOUT)
        + azure_blob.TOKEN_CONNECTION_TIMEOUT
        + azure_blob.TOKEN_READ_TIMEOUT
    )
    assert worst <= 35


@pytest.mark.parametrize(
    "identity_environment",
    [
        {},
        # Container Apps: the App Service style identity endpoint (placeholders).
        {"IDENTITY_ENDPOINT": "http://localhost:42356/msi/token", "IDENTITY_HEADER": "x"},
    ],
)
def test_the_real_client_is_built_with_the_limits(
    identity_environment: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SDK objects themselves carry the limits: the transports' timeouts and retry
    policies that never retry, for storage and for the token (both identity paths).
    Building them makes no call."""
    for name, value in identity_environment.items():
        monkeypatch.setenv(name, value)
    storage = azure_blob.AzureBlobDocumentStorage(
        account_url=ACCOUNT_URL, container="documents", client_id=CLIENT_ID
    )
    container: Any = storage._container
    transport = container._pipeline._transport._transport
    assert transport.connection_config.timeout == azure_blob.CONNECTION_TIMEOUT
    assert transport.connection_config.read_timeout == azure_blob.READ_TIMEOUT
    assert container._config.retry_policy.total_retries == azure_blob.RETRY_TOTAL

    token_pipeline = container.credential._credential._client._pipeline
    token_transport = token_pipeline._transport
    assert token_transport.connection_config.timeout == azure_blob.TOKEN_CONNECTION_TIMEOUT
    assert token_transport.connection_config.read_timeout == azure_blob.TOKEN_READ_TIMEOUT
    [token_retry] = [
        policy for policy in token_pipeline._impl_policies if "Retry" in type(policy).__name__
    ]
    assert token_retry.total_retries == azure_blob.TOKEN_RETRY_TOTAL


def test_building_the_real_client_makes_no_call() -> None:
    """Constructing the SDK objects reaches no network; only an operation would."""
    storage = azure_blob.AzureBlobDocumentStorage(
        account_url=ACCOUNT_URL, container="documents", client_id=CLIENT_ID
    )
    assert storage._container.container_name == "documents"


# --- Memory storage -----------------------------------------------------------------------


def test_memory_storage_round_trip_in_chunks() -> None:
    storage = MemoryDocumentStorage()
    data = bytes(range(256)) * (CHUNK_SIZE // 128)
    storage.put("documents/store/a/b", data, "application/pdf")
    assert storage.exists("documents/store/a/b")
    chunks = list(storage.open("documents/store/a/b"))
    assert len(chunks) == 2
    assert b"".join(chunks) == data
    assert storage.blobs["documents/store/a/b"][1] == "application/pdf"


def test_memory_storage_refuses_to_overwrite_and_reports_a_missing_path() -> None:
    storage = MemoryDocumentStorage()
    storage.put("p", b"one", "image/png")
    with pytest.raises(StorageUnavailable):
        storage.put("p", b"two", "image/png")
    assert storage.blobs["p"][0] == b"one"
    with pytest.raises(StorageUnavailable):
        storage.open("missing")
    assert not storage.exists("missing")


def test_memory_storage_can_be_told_to_fail() -> None:
    storage = MemoryDocumentStorage()
    storage.fail_put = True
    with pytest.raises(StorageUnavailable):
        storage.put("p", b"x", "image/png")
    assert storage.blobs == {}
    storage.fail_put = False
    storage.put("p", b"x", "image/png")
    storage.fail_open = True
    with pytest.raises(StorageUnavailable):
        storage.open("p")


# --- Azure storage, SDK client replaced ---------------------------------------------------

SECRET_LOOKING = "https://stexample000.blob.core.windows.net/documents?sig=PLACEHOLDER"


class FakeDownloader:
    def __init__(self, chunks: list[bytes], fail_after: int | None = None) -> None:
        self._chunks = chunks
        self._fail_after = fail_after

    def chunks(self) -> Iterator[bytes]:
        for index, chunk in enumerate(self._chunks):
            if self._fail_after is not None and index >= self._fail_after:
                raise azure_blob.AzureError(SECRET_LOOKING)
            yield chunk


class FakeBlob:
    def __init__(self, container: FakeContainer, path: str) -> None:
        self.container = container
        self.path = path

    def download_blob(self, **kwargs: Any) -> FakeDownloader:
        self.container.calls.append(("download_blob", self.path, kwargs))
        if self.container.error is not None:
            raise self.container.error
        return FakeDownloader(self.container.chunks, self.container.fail_after)

    def exists(self, **kwargs: Any) -> bool:
        self.container.calls.append(("exists", self.path, kwargs))
        if self.container.error is not None:
            raise self.container.error
        return self.path == "present"


class FakeContainer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, dict[str, Any]]] = []
        self.error: Exception | None = None
        self.chunks: list[bytes] = [b"ab", b"cd"]
        self.fail_after: int | None = None

    def upload_blob(self, **kwargs: Any) -> None:
        self.calls.append(("upload_blob", kwargs.get("name"), kwargs))
        if self.error is not None:
            raise self.error

    def get_blob_client(self, path: str) -> FakeBlob:
        return FakeBlob(self, path)


@pytest.fixture
def azure() -> tuple[azure_blob.AzureBlobDocumentStorage, FakeContainer]:
    storage = azure_blob.AzureBlobDocumentStorage(
        account_url=ACCOUNT_URL, container="documents", client_id=CLIENT_ID
    )
    container = FakeContainer()
    storage._container = container  # type: ignore[assignment]
    return storage, container


def test_azure_put_never_overwrites_and_sets_the_type_and_a_timeout(
    azure: tuple[azure_blob.AzureBlobDocumentStorage, FakeContainer],
) -> None:
    storage, container = azure
    storage.put("documents/store/a/b", b"%PDF-1.4", "application/pdf")
    name, path, kwargs = container.calls[0]
    assert (name, path) == ("upload_blob", "documents/store/a/b")
    assert kwargs["data"] == b"%PDF-1.4"
    assert kwargs["overwrite"] is False
    assert kwargs["content_settings"].content_type == "application/pdf"
    assert kwargs["timeout"] == azure_blob.OPERATION_TIMEOUT


def test_azure_open_streams_chunks_and_exists_asks_the_blob(
    azure: tuple[azure_blob.AzureBlobDocumentStorage, FakeContainer],
) -> None:
    storage, container = azure
    assert b"".join(storage.open("documents/x")) == b"abcd"
    assert container.calls[0][2] == {"timeout": azure_blob.OPERATION_TIMEOUT}
    assert storage.exists("present") is True
    assert storage.exists("absent") is False


# Stand-ins named like the SDK's subclasses of AzureError (the module catches the base
# class). Built here so this test imports nothing from the SDK itself.
SDK_ERRORS = [
    type(name, (azure_blob.AzureError,), {})
    for name in (
        "ServiceRequestError",
        "ResourceNotFoundError",
        "ResourceExistsError",
        "ClientAuthenticationError",
        "CredentialUnavailableError",
    )
]


@pytest.mark.parametrize(
    "error",
    [azure_blob.AzureError(SECRET_LOOKING), *(kind(SECRET_LOOKING) for kind in SDK_ERRORS)],
    ids=lambda error: type(error).__name__,
)
@pytest.mark.parametrize("operation", ["put", "open", "exists"])
def test_azure_errors_become_storage_unavailable_with_the_class_name_only(
    azure: tuple[azure_blob.AzureBlobDocumentStorage, FakeContainer],
    error: Exception,
    operation: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    storage, container = azure
    container.error = error
    caplog.set_level(logging.WARNING)
    with pytest.raises(StorageUnavailable) as raised:
        if operation == "put":
            storage.put("p", b"x", "image/png")
        elif operation == "open":
            storage.open("p")
        else:
            storage.exists("p")
    assert raised.value.cause == type(error).__name__
    assert str(raised.value) == type(error).__name__
    assert raised.value.__cause__ is None and raised.value.__suppress_context__
    assert "sig=" not in caplog.text and "blob.core" not in caplog.text
    assert type(error).__name__ in caplog.text


def test_azure_failure_part_way_through_a_download_is_storage_unavailable(
    azure: tuple[azure_blob.AzureBlobDocumentStorage, FakeContainer],
) -> None:
    storage, container = azure
    container.fail_after = 1
    chunks = storage.open("p")
    assert next(chunks) == b"ab"
    with pytest.raises(StorageUnavailable) as raised:
        next(chunks)
    assert raised.value.cause == "AzureError"


# --- Fence and static checks --------------------------------------------------------------


def _imports_azure(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[0] == "azure" for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "azure":
                return True
        elif isinstance(node, ast.Call) and node.args and _is_dynamic_import(node.func):
            argument = node.args[0]
            if isinstance(argument, ast.Constant) and str(argument.value).startswith("azure"):
                return True
    return False


def _is_dynamic_import(func: ast.expr) -> bool:
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    return name in {"import_module", "__import__"}


def test_the_import_check_detects_each_form() -> None:
    for source in (
        "import azure.storage.blob",
        "import azure",
        "from azure.identity import ManagedIdentityCredential",
        "from azure import storage",
        "importlib.import_module('azure.storage.blob')",
        "__import__('azure.identity')",
    ):
        assert _imports_azure(ast.parse(source)), source
    assert not _imports_azure(ast.parse("from documents_storage import get_storage"))


def test_nothing_outside_the_package_imports_the_azure_sdk() -> None:
    """Rule 8's pattern for the storage SDK (PLAN-010c D2), tests included: they reach
    the SDK only through ``documents_storage.azure_blob``."""
    offenders = [
        str(path.relative_to(BACKEND))
        for path in BACKEND.rglob("*.py")
        if path.relative_to(BACKEND).parts[0] != "documents_storage"
        and "staticfiles" not in path.parts
        and _imports_azure(ast.parse(path.read_text(encoding="utf-8")))
    ]
    assert offenders == []


FORBIDDEN_CREDENTIALS = ("account_key", "connection_string", "from_connection_string")


def _credential_words(tree: ast.AST) -> list[str]:
    """Identifiers, attributes, keyword names and string constants that name a shared
    key, a connection string or a shared access signature."""
    words: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            words.append(node.id)
        elif isinstance(node, ast.Attribute):
            words.append(node.attr)
        elif isinstance(node, ast.keyword) and node.arg:
            words.append(node.arg)
        elif isinstance(node, ast.alias):
            words.append(node.asname or node.name)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            words.append(node.value)
    return [
        word
        for word in words
        if any(forbidden in word.lower() for forbidden in FORBIDDEN_CREDENTIALS)
        or "sas" in word.lower().replace(".", "_").split("_")
        or "accountkey=" in word.lower().replace(" ", "")
        or "sharedaccesssignature" in word.lower()
    ]


def test_the_credential_check_detects_each_form() -> None:
    for source in (
        "BlobServiceClient(url, credential={'account_name': 'a', 'account_key': 'k'})",
        "BlobServiceClient.from_connection_string(text)",
        "x = settings.DOCUMENTS_CONNECTION_STRING",
        "generate_blob_sas(account_name='a')",
        "client(sas_token=token)",
        # Assembled at runtime so the repository's secret scan does not see a key marker.
        repr("DefaultEndpointsProtocol=https;" + "Account" + "Key=placeholder"),
    ):
        assert _credential_words(ast.parse(source)), source
    assert not _credential_words(ast.parse("ManagedIdentityCredential(client_id=client_id)"))


def test_the_sdk_is_never_given_a_key_a_connection_string_or_a_sas() -> None:
    sources = sorted(PACKAGE.rglob("*.py"))
    assert {path.name for path in sources} >= {"__init__.py", "azure_blob.py", "memory.py"}
    offenders = {
        path.name: words
        for path in sources
        if (words := _credential_words(ast.parse(path.read_text(encoding="utf-8"))))
    }
    assert offenders == {}


def test_the_azure_module_uses_the_managed_identity_credential_only() -> None:
    tree = ast.parse((PACKAGE / "azure_blob.py").read_text(encoding="utf-8"))
    credentials = [
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id.endswith("Credential")
    ]
    assert credentials == ["ManagedIdentityCredential"]


# --- Environment guard (D1, D2) -----------------------------------------------------------

BASE_ENV = {
    "APP_ENV": "staging",
    "STAX_ENV": "sandbox",
    "STAX_API_BASE_URL": "https://apiprod.fattlabs.com",
    "STAX_API_KEY": "FAKE-CI-KEY-XYZ123",
    "STAX_WEBHOOK_URL_TOKEN": "t" * 64,
    "DATABASE_URL": "postgresql://user@db.example.com/finance",
    "APP_BASE_URL": "https://app.example.com",
    "DJANGO_SECRET_KEY": "placeholder-not-secret",
}


def test_documents_default_to_off_with_the_default_container() -> None:
    config = validate(BASE_ENV)
    assert config.documents_storage_account_url == ""
    assert config.documents_container == "documents"
    assert config.azure_client_id == ""


@pytest.mark.parametrize(
    "url", [ACCOUNT_URL, ACCOUNT_URL + "/", "https://abc.blob.core.windows.net"]
)
def test_a_blob_endpoint_with_a_client_id_is_accepted(url: str) -> None:
    config = validate(
        {**BASE_ENV, "DOCUMENTS_STORAGE_ACCOUNT_URL": url, "AZURE_CLIENT_ID": CLIENT_ID}
    )
    assert config.documents_storage_account_url == url
    assert config.azure_client_id == CLIENT_ID


@pytest.mark.parametrize(
    "url",
    [
        "http://stexample000.blob.core.windows.net",
        "https://stexample000.blob.core.windows.net/documents",
        "https://stexample000.blob.core.windows.net.example.com",
        "https://StExample.blob.core.windows.net",
        "https://ab.blob.core.windows.net",
        "https://stexample000.file.core.windows.net",
        "https://example.com",
        "https://stexample000.blob.core.windows.net/?sv=2024&sig=PLACEHOLDER",
        "https://user:pass@stexample000.blob.core.windows.net",
    ],
)
def test_anything_but_a_blob_endpoint_is_refused_without_echoing_it(url: str) -> None:
    with pytest.raises(EnvGuardError) as raised:
        validate({**BASE_ENV, "DOCUMENTS_STORAGE_ACCOUNT_URL": url, "AZURE_CLIENT_ID": CLIENT_ID})
    assert "DOCUMENTS_STORAGE_ACCOUNT_URL" in str(raised.value)
    assert url not in str(raised.value)


def test_the_client_id_is_required_with_a_url() -> None:
    with pytest.raises(EnvGuardError) as raised:
        validate({**BASE_ENV, "DOCUMENTS_STORAGE_ACCOUNT_URL": ACCOUNT_URL})
    assert "AZURE_CLIENT_ID is required" in str(raised.value)


@pytest.mark.parametrize("client_id", ["not-a-uuid", "00000000-0000-0000-0000-00000000000", " "])
def test_a_client_id_that_is_not_a_uuid_is_refused_without_echoing_it(client_id: str) -> None:
    with pytest.raises(EnvGuardError) as raised:
        validate({**BASE_ENV, "AZURE_CLIENT_ID": client_id})
    assert "AZURE_CLIENT_ID" in str(raised.value)
    assert "not-a-uuid" not in str(raised.value)


@pytest.mark.parametrize("container", ["documents", "doc-store-2", "abc"])
def test_a_valid_container_name_is_accepted(container: str) -> None:
    assert validate({**BASE_ENV, "DOCUMENTS_CONTAINER": container}).documents_container == container


@pytest.mark.parametrize(
    "container", ["Documents", "ab", "-docs", "docs-", "do--cs", "docs/../x", "d" * 64, "docs_1"]
)
def test_an_invalid_container_name_is_refused(container: str) -> None:
    with pytest.raises(EnvGuardError) as raised:
        validate({**BASE_ENV, "DOCUMENTS_CONTAINER": container})
    assert "DOCUMENTS_CONTAINER" in str(raised.value)


def test_settings_expose_the_document_settings() -> None:
    from django.conf import settings

    assert settings.DOCUMENTS_STORAGE_ACCOUNT_URL == ""
    assert settings.DOCUMENTS_CONTAINER == "documents"
    assert settings.AZURE_CLIENT_ID == ""

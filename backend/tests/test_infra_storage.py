"""PLAN-010c D1: the document storage account, its role assignment and the web app's settings.

Text-level checks of the Bicep templates, in the style of the other infrastructure tests
(test_jobs.py, test_reconciliation.py). ``az bicep build`` and the what-if are run by hand;
these tests pin the security-relevant lines so a later edit cannot drop them silently.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INFRA = REPO / "infra"
STORAGE = INFRA / "modules/storage.bicep"
WEB = INFRA / "modules/containerAppWeb.bicep"
WORKER = INFRA / "modules/containerAppWorker.bicep"
JOB = INFRA / "modules/containerAppJobReconcile.bicep"
MAIN = INFRA / "main.bicep"

BLOB_DATA_CONTRIBUTOR = "ba92f5b4-2d11-453d-a403-e96b0029c9fe"
DOCUMENT_ENV = {
    "DOCUMENTS_STORAGE_ACCOUNT_URL": "documentsStorageAccountUrl",
    "DOCUMENTS_CONTAINER": "documentsContainer",
    "AZURE_CLIENT_ID": "azureClientId",
}


def _code(path: Path) -> str:
    """The template without its ``//`` comments (a URL's ``://`` is kept)."""
    return re.sub(r"(^|\s)//.*$", r"\1", path.read_text(), flags=re.MULTILINE)


def _module_block(main: str, head: str) -> str:
    assert head in main
    return main.split(head)[1].split("\n}\n")[0]


def _resource_block(text: str, head: str) -> str:
    assert head in text
    return text.split(head)[1].split("\n}\n")[0]


# --- The account -------------------------------------------------------------------------------


def test_the_account_accepts_entra_tokens_only_over_tls_1_2() -> None:
    account = _resource_block(
        _code(STORAGE), "resource account 'Microsoft.Storage/storageAccounts@"
    )
    for line in (
        "name: 'Standard_LRS'",
        "kind: 'StorageV2'",
        "allowBlobPublicAccess: false",
        "allowSharedKeyAccess: false",
        "minimumTlsVersion: 'TLS1_2'",
        "supportsHttpsTrafficOnly: true",
        "publicNetworkAccess: 'Enabled'",
    ):
        assert f"\n    {line}\n" in account or f"\n  {line}\n" in account, line
    # The open endpoint is a recorded decision, stated next to the setting.
    text = STORAGE.read_text()
    assert "Entra tokens only" in text and "hardening option" in text


def test_versioning_and_both_soft_deletes_keep_thirty_days() -> None:
    text = _code(STORAGE)
    assert "param retentionDays int = 30\n" in text
    blob = _resource_block(
        text, "resource blobService 'Microsoft.Storage/storageAccounts/blobServices@"
    )
    assert "isVersioningEnabled: true" in blob
    for policy in ("deleteRetentionPolicy", "containerDeleteRetentionPolicy"):
        assert f"{policy}: {{\n      enabled: true\n      days: retentionDays\n    }}" in blob
    # Neither main.bicep nor a parameter file overrides the 30 days.
    for path in (MAIN, *sorted((INFRA / "parameters").glob("*.bicepparam"))):
        assert "retentionDays" not in path.read_text(), path.name


def test_the_container_is_private_and_named_documents_by_default() -> None:
    text = _code(STORAGE)
    container = _resource_block(
        text, "resource container 'Microsoft.Storage/storageAccounts/blobServices/containers@"
    )
    assert "parent: blobService" in container
    assert "publicAccess: 'None'" in container
    assert "param containerName string = 'documents'\n" in text
    assert "param documentsContainerName string = 'documents'\n" in _code(MAIN)


def test_the_account_name_default_is_lowercase_unique_and_at_most_24_characters() -> None:
    main = _code(MAIN)
    match = re.search(r"^param documentsStorageAccountName string = (.+)$", main, re.MULTILINE)
    assert match is not None
    default = match.group(1)
    assert default.startswith("take(toLower(") and default.endswith(", 24)")
    assert "uniqueString(resourceGroup().id)" in default
    assert "replace(resourceNamePrefix, '-', '')" in default
    for text in (main, _code(STORAGE)):
        assert "@minLength(3)\n@maxLength(24)\nparam " in text


# --- The role assignment -----------------------------------------------------------------------


def test_the_identity_gets_blob_data_contributor_on_the_container_only() -> None:
    text = _code(STORAGE)
    assert f"'{BLOB_DATA_CONTRIBUTOR}'" in text
    assigns = re.findall(r"resource \w+ 'Microsoft\.Authorization/roleAssignments@", text)
    assert len(assigns) == 1
    role = _resource_block(
        text, "resource blobDataContributor 'Microsoft.Authorization/roleAssignments@"
    )
    assert "scope: container\n" in role
    assert (
        "name: guid(container.id, principalId, storageBlobDataContributorRoleDefinitionId)"
    ) in role
    assert "principalType: 'ServicePrincipal'" in role
    main = _code(MAIN)
    block = _module_block(main, "module storage 'modules/storage.bicep'")
    assert "principalId: identity.outputs.principalId" in block
    assert "location: location" in block and "tags: tags" in block


# --- No key, connection string or SAS ----------------------------------------------------------


def test_the_storage_module_carries_no_key_connection_string_or_sas() -> None:
    text = _code(STORAGE)
    for token in ("listkeys", "connectionstring", "accountkey", "sharedaccesssignature"):
        assert token not in text.lower(), token
    assert not re.search(r"\blist\w*sas\b|\bsas\b", text, re.IGNORECASE)
    assert "@secure()" not in text
    outputs = re.findall(r"^output (\w+) ", text, re.MULTILINE)
    assert outputs == ["accountName", "blobEndpoint", "containerName", "roleAssignmentId"]


@pytest.mark.parametrize(
    "path",
    sorted([*INFRA.rglob("*.bicep"), *INFRA.rglob("*.bicepparam")]),
    ids=lambda p: p.relative_to(INFRA).as_posix(),
)
def test_no_template_reads_a_storage_key_or_mints_a_sas(path: Path) -> None:
    code = _code(path)
    lowered = code.lower()
    for token in (
        "connectionstring",
        "accountkey",
        "sharedaccesssignature",
        "listaccountsas",
        "listservicesas",
    ):
        assert token not in lowered, token
    assert not re.search(r"\bsas\b", code, re.IGNORECASE)
    # The one pre-existing listKeys() reads the Log Analytics workspace key for the
    # Container Apps environment; no other key is ever listed.
    calls = re.findall(r"(\w+)\.listKeys\(", code, re.IGNORECASE)
    if path.name == "containerAppsEnvironment.bicep":
        assert calls == ["workspace"]
    else:
        assert "listkeys" not in lowered


# --- The web app's settings, and nobody else's -------------------------------------------------


def test_the_web_app_gets_the_three_document_settings_as_plain_values() -> None:
    web = _code(WEB)
    for name, param in DOCUMENT_ENV.items():
        assert f"param {param} string\n" in web
        assert f"  {{\n    name: '{name}'\n    value: {param}\n  }}\n" in web
        assert f"name: '{name}'\n    secretRef" not in web
    main = _code(MAIN)
    block = _module_block(main, "module web 'modules/containerAppWeb.bicep'")
    assert "documentsStorageAccountUrl: storage.outputs.blobEndpoint\n" in block
    assert "documentsContainer: storage.outputs.containerName\n" in block
    assert "azureClientId: identity.outputs.clientId\n" in block


def test_the_worker_and_the_job_get_no_document_settings() -> None:
    for path in (WORKER, JOB):
        text = path.read_text()
        for name, param in DOCUMENT_ENV.items():
            assert name not in text and param not in text, (path.name, name)
    main = _code(MAIN)
    for head in (
        "module worker 'modules/containerAppWorker.bicep'",
        "module reconcileJob 'modules/containerAppJobReconcile.bicep'",
    ):
        block = _module_block(main, head)
        assert "storage" not in block and "documents" not in block.lower(), head


def test_main_outputs_the_account_and_container_names() -> None:
    main = _code(MAIN)
    assert "output documentsStorageAccountName string = storage.outputs.accountName\n" in main
    assert "output documentsContainerName string = storage.outputs.containerName\n" in main
    # The blob endpoint reaches the web app only; it is not a deployment output.
    assert not re.search(r"^output \w+ string = storage\.outputs\.blobEndpoint", main, re.M)


# --- .env.example ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [*DOCUMENT_ENV, "REPORT_STATEMENT_TIMEOUT_MS"],
)
def test_env_example_names_the_setting_with_an_empty_value(name: str) -> None:
    lines = (REPO / ".env.example").read_text().splitlines()
    assert f"{name}=" in lines
    index = lines.index(f"{name}=")
    assert lines[index - 1].startswith("# "), name
    assert sum(1 for line in lines if line.startswith(f"{name}=")) == 1

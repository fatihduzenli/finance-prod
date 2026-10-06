"""PLAN-011b D9: the mail settings, the Gmail key by vault reference, and the daily job.

Text-level checks of the Bicep templates and ``.env.example``, in the style of
``test_infra_storage.py``. ``az bicep build`` and the what-if are run by hand; these
tests pin the lines a later edit must not drop: no secret value, the key referenced only
while mail is on, the daily job's cron and command, and which process gets which setting.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INFRA = REPO / "infra"
WEB = INFRA / "modules/containerAppWeb.bicep"
WORKER = INFRA / "modules/containerAppWorker.bicep"
RECONCILE = INFRA / "modules/containerAppJobReconcile.bicep"
DAILY = INFRA / "modules/containerAppJobDaily.bicep"
MAIN = INFRA / "main.bicep"

MAIL_PLAIN = ("MAIL_ENABLED", "GMAIL_SENDER", "MAIL_CATCH_ALL", "COMPANY_NAME")
MAIL_SECRET_ENV = "GMAIL_SERVICE_ACCOUNT_JSON"
MAIL_SECRET_REF = "gmail-service-account-json"
VAULT_SECRET_NAME = "gmail-service-account-key"
DOCUMENT_ENV = ("DOCUMENTS_STORAGE_ACCOUNT_URL", "DOCUMENTS_CONTAINER", "AZURE_CLIENT_ID")
MAIL_PARAMS = {
    "mailEnabled": "bool = false",
    "gmailSender": "string = ''",
    "mailCatchAll": "string = ''",
    "companyName": "string = 'Global Retail Groups'",
    "gmailServiceAccountSecretName": f"string = '{VAULT_SECRET_NAME}'",
}
MAIL_MODULES = (WEB, WORKER, DAILY)


def _code(path: Path) -> str:
    """The template without its ``//`` comments (a URL's ``://`` is kept)."""
    return re.sub(r"(^|\s)//.*$", r"\1", path.read_text(), flags=re.MULTILINE)


def _env_names(text: str) -> set[str]:
    return set(re.findall(r"name: '([A-Z_]+)'\n", text))


def _secret_names(text: str) -> set[str]:
    return set(re.findall(r"name: '([a-z-]+)'\n    keyVaultUrl", text))


def _module_block(main: str, head: str) -> str:
    assert head in main
    return main.split(head)[1].split("\n}\n")[0]


# --- The key: a vault reference by name, only while mail is on ---------------------------------


@pytest.mark.parametrize("path", MAIL_MODULES, ids=lambda p: p.name)
def test_the_key_is_a_vault_reference_wired_only_when_mail_is_on(path: Path) -> None:
    code = _code(path)
    for name, default in MAIL_PARAMS.items():
        assert f"param {name} {default}\n" in code, (path.name, name)
    assert (
        f"    name: '{MAIL_SECRET_REF}'\n"
        "    keyVaultUrl: '${keyVaultUri}secrets/${gmailServiceAccountSecretName}'\n"
        "    identity: identityResourceId\n"
    ) in code
    assert f"    name: '{MAIL_SECRET_ENV}'\n    secretRef: '{MAIL_SECRET_REF}'\n" in code
    # Only in the lists added under the condition, never in the base lists.
    assert "mailEnabled ? mailSecrets : []" in code
    assert "mailEnabled ? mailSecretEnv : []" in code
    base_secrets = code.split("var baseSecrets = [")[1].split("\n]\n")[0]
    base_env = code.split("var baseEnv = [")[1].split("\n]\n")[0]
    assert MAIL_SECRET_REF not in base_secrets and MAIL_SECRET_ENV not in base_env
    # Never a plain value.
    assert f"name: '{MAIL_SECRET_ENV}'\n    value" not in code


@pytest.mark.parametrize("path", MAIL_MODULES, ids=lambda p: p.name)
def test_the_plain_mail_settings_are_values_from_parameters(path: Path) -> None:
    code = _code(path)
    assert "  {\n    name: 'MAIL_ENABLED'\n    value: mailEnabled ? 'true' : 'false'\n  }\n" in code
    for name, param in (
        ("GMAIL_SENDER", "gmailSender"),
        ("MAIL_CATCH_ALL", "mailCatchAll"),
        ("COMPANY_NAME", "companyName"),
    ):
        assert f"  {{\n    name: '{name}'\n    value: {param}\n  }}\n" in code, (path.name, name)
        assert f"name: '{name}'\n    secretRef" not in code


@pytest.mark.parametrize(
    "path",
    sorted([*INFRA.rglob("*.bicep"), *INFRA.rglob("*.bicepparam")]),
    ids=lambda p: p.relative_to(INFRA).as_posix(),
)
def test_no_template_holds_a_key_or_a_catch_all_address(path: Path) -> None:
    text = path.read_text()
    for token in ("private_key", "BEGIN PRIVATE", "client_email", "token_uri", "@gserviceaccount"):
        assert token not in text, token
    # The catch-all is passed on the command line; no committed file sets it.
    assert not re.search(r"^param mailCatchAll = ", text, re.MULTILINE)
    assert not re.search(r"^param gmailSender = '[^']+'", text, re.MULTILINE)
    # No address at all in a template or parameter file.
    assert not re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}", _code(path))


# --- The daily job ------------------------------------------------------------------------------


def test_the_daily_job_runs_the_tick_once_a_day_as_no_caller() -> None:
    code = _code(DAILY)
    assert "resource job 'Microsoft.App/jobs@2024-03-01'" in code
    assert "triggerType: 'Schedule'" in code
    assert "param cronExpression string = '0 13 * * *'\n" in code
    assert "cronExpression: cronExpression" in code
    for line in ("replicaRetryLimit: 0", "parallelism: 1", "replicaCompletionCount: 1"):
        assert line in code
    assert "'exec python backend/manage.py collections_tick'" in code
    assert "reconcile" not in code
    assert "provider_environment_check" not in code
    assert "name: 'STAX_CALLER'\n    value: 'none'\n" in code
    assert "value: 'worker'" not in code
    assert "@secure()" not in code
    outputs = re.findall(r"^output (\w+) ", code, re.MULTILINE)
    assert outputs == ["jobId", "jobName"]


def test_the_daily_job_has_the_reconcile_jobs_settings_plus_mail_and_no_documents() -> None:
    daily = DAILY.read_text()
    reconcile = RECONCILE.read_text()
    assert _secret_names(daily) == _secret_names(reconcile) | {MAIL_SECRET_REF}
    assert _env_names(daily) == _env_names(reconcile) | {*MAIL_PLAIN, MAIL_SECRET_ENV}
    for name in DOCUMENT_ENV:
        assert name not in daily


def test_web_and_worker_carry_the_same_mail_settings() -> None:
    web, worker = WEB.read_text(), WORKER.read_text()
    mail = {*MAIL_PLAIN, MAIL_SECRET_ENV}
    assert mail <= _env_names(web) and mail <= _env_names(worker)
    assert MAIL_SECRET_REF in _secret_names(web) & _secret_names(worker)
    # The documents settings stay the web app's alone.
    assert _env_names(web) - _env_names(worker) == set(DOCUMENT_ENV)
    assert _env_names(worker) <= _env_names(web)


def test_the_reconcile_job_sends_no_mail() -> None:
    text = RECONCILE.read_text()
    for name in (*MAIL_PLAIN, MAIL_SECRET_ENV, MAIL_SECRET_REF, "mailEnabled"):
        assert name not in text


# --- main.bicep ---------------------------------------------------------------------------------


def test_main_declares_the_mail_parameters_with_safe_defaults() -> None:
    main = _code(MAIN)
    for name, default in MAIL_PARAMS.items():
        assert f"param {name} {default}\n" in main, name
    assert "param dailyJobName string = '${resourceNamePrefix}-daily'\n" in main
    assert "output dailyJobName string = dailyJob.outputs.jobName\n" in main


@pytest.mark.parametrize(
    "head",
    [
        "module web 'modules/containerAppWeb.bicep'",
        "module worker 'modules/containerAppWorker.bicep'",
        "module dailyJob 'modules/containerAppJobDaily.bicep'",
    ],
)
def test_main_passes_the_mail_parameters_to_web_worker_and_daily(head: str) -> None:
    block = _module_block(_code(MAIN), head)
    for name in MAIL_PARAMS:
        assert f"    {name}: {name}\n" in block, (head, name)


def test_main_wires_the_daily_job_like_the_reconcile_job() -> None:
    main = _code(MAIN)
    daily = _module_block(main, "module dailyJob 'modules/containerAppJobDaily.bicep'")
    reconcile = _module_block(main, "module reconcileJob 'modules/containerAppJobReconcile.bicep'")
    assert "jobName: dailyJobName" in daily and "image: webImage" in daily
    for line in reconcile.splitlines():
        if line.strip().startswith(("jobName:", "name:")) or not line.strip():
            continue
        assert line in daily, line
    assert "storage" not in daily and "documents" not in daily.lower()
    reconcile_block = _module_block(
        main, "module reconcileJob 'modules/containerAppJobReconcile.bicep'"
    )
    assert "mail" not in reconcile_block.lower()


# --- .env.example -------------------------------------------------------------------------------


@pytest.mark.parametrize("name", [*MAIL_PLAIN, MAIL_SECRET_ENV])
def test_env_example_names_the_mail_setting_with_an_empty_value(name: str) -> None:
    lines = (REPO / ".env.example").read_text().splitlines()
    assert f"{name}=" in lines
    index = lines.index(f"{name}=")
    assert lines[index - 1].startswith("# "), name
    assert sum(1 for line in lines if line.startswith(f"{name}=")) == 1


def test_env_example_names_the_vault_secret_and_the_time_zone() -> None:
    text = (REPO / ".env.example").read_text()
    lines = text.splitlines()
    index = lines.index(f"{MAIL_SECRET_ENV}=")
    comment = "\n".join(lines[index - 3 : index])
    assert VAULT_SECRET_NAME in comment and "Secret" in comment
    assert "America/New_York" in text
    assert not re.search(r"^TIME_ZONE=", text, re.MULTILINE)
    # Every line is a comment, blank, or a name with an empty value.
    for line in lines:
        assert not line or line.startswith("#") or re.fullmatch(r"[A-Z0-9_]+=", line), line

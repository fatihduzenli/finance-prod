"""scripts/check-no-secrets.sh must fail the build on a credential in a served asset."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = "scripts/check-no-secrets.sh"
PROBE_NAME = "zz_secret_scan_probe.js"


def _run(repo_root: Path, stax_api_key: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", SCRIPT],
        cwd=repo_root,
        env={**os.environ, "STAX_API_KEY": stax_api_key},
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )


def test_script_is_executable(repo_root: Path) -> None:
    assert os.access(repo_root / SCRIPT, os.X_OK)


def test_clean_tree_passes(repo_root: Path, fake_stax_api_key: str) -> None:
    result = _run(repo_root, fake_stax_api_key)
    assert result.returncode == 0, result.stdout + result.stderr


def test_planted_key_value_fails(repo_root: Path, fake_stax_api_key: str) -> None:
    probe = repo_root / "backend" / "finance" / "static" / PROBE_NAME
    probe.write_text(f'window.apiKey = "{fake_stax_api_key}";\n', encoding="utf-8")
    try:
        result = _run(repo_root, fake_stax_api_key)
    finally:
        probe.unlink(missing_ok=True)

    assert result.returncode == 1
    output = result.stdout + result.stderr
    assert PROBE_NAME in output
    assert fake_stax_api_key not in output


def test_planted_pattern_fails_even_without_the_value(
    repo_root: Path, fake_stax_api_key: str
) -> None:
    probe = repo_root / "backend" / "finance" / "static" / PROBE_NAME
    probe.write_text('fetch("https://apiprod.fattlabs.com/invoice");\n', encoding="utf-8")
    try:
        result = _run(repo_root, fake_stax_api_key)
    finally:
        probe.unlink(missing_ok=True)

    assert result.returncode == 1
    assert PROBE_NAME in result.stdout + result.stderr

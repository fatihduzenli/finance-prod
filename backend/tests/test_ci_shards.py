"""The migration-test job's shards (PLAN-013a A1): every test of the five files the job runs is
in exactly one shard, no shard is empty, the marker reaches each test, and the workflow runs the
three shards and needs them before deploying."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import ROUNDTRIP_SHARDS, SHARD_FILES

REPO = Path(__file__).resolve().parents[2]
TESTS = Path(__file__).resolve().parent


def _collected(path: str) -> list[str]:
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", path],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return [line.split("::", 1)[1].split("[", 1)[0] for line in out.splitlines() if "::" in line]


def test_every_round_trip_test_is_in_exactly_one_shard() -> None:
    seen: set[str] = set()
    for name in SHARD_FILES:
        collected = set(_collected(f"backend/tests/{name}"))
        listed = {test for test in ROUNDTRIP_SHARDS if test in collected}
        assert not (listed & seen), (name, listed & seen)
        seen |= listed
        if name in ("test_one_off_charges.py", "test_staging_defects_adversarial.py"):
            # Only the migration cases of these two files belong to the job.
            assert listed, name
            continue
        assert collected == listed, (name, collected ^ listed)
    assert seen == set(ROUNDTRIP_SHARDS), set(ROUNDTRIP_SHARDS) - seen


def test_the_shards_are_three_and_none_is_empty() -> None:
    assert set(ROUNDTRIP_SHARDS.values()) == {1, 2, 3}
    for shard in (1, 2, 3):
        assert sum(1 for value in ROUNDTRIP_SHARDS.values() if value == shard) >= 9


def test_the_markers_reach_the_tests(pytestconfig: pytest.Config) -> None:
    out = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "-m",
            "shard2",
            "backend/tests/test_migrations_roundtrip.py",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    names = {line.split("::", 1)[1] for line in out.splitlines() if "::" in line}
    round_trip = set(_collected("backend/tests/test_migrations_roundtrip.py"))
    expected = {
        test for test, shard in ROUNDTRIP_SHARDS.items() if shard == 2 and test in round_trip
    }
    assert names == expected


def test_the_workflow_runs_the_three_shards_and_deploys_after_all() -> None:
    workflow = (REPO / ".github/workflows/ci.yml").read_text()
    assert re.search(r"shard:\s*\[1, 2, 3\]", workflow)
    assert '-m "shard${{ matrix.shard }}"' in workflow or "-m shard${{ matrix.shard }}" in workflow
    assert "timeout-minutes: 30" in workflow.split("name: Migration tests")[1].split("steps:")[0]
    assert "TEMPORARY" not in workflow
    assert "migrations-complete" in workflow
    deploy = workflow.split("deploy-staging:")[1]
    assert "migrations-complete" in deploy.split("steps:")[0]
    on_block = workflow.split("\non:")[1].split("\npermissions:")[0]
    assert "pull_request:" in on_block and re.search(r"push:\s*\n\s*branches:\s*\[main\]", on_block)

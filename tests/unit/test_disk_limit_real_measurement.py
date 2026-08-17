"""max_disk_gb must measure the AGENT'S data, not the whole volume (spec Section 10).

`disk_over_limit` used `shutil.disk_usage(path).used`, which reports the entire
filesystem. On any ordinary host -- a CI runner, a Render instance, a laptop -- that
is already far past max_disk_gb (10), so the mission API answered 429 to every
request. The live E2E hit exactly this: HTTP 429 on the first mission against a
freshly booted stack with an empty queue and an empty data dir.

The suite never caught it because the function returned False whenever
TEST_MODE=true, and the one constraint test drove refusal through the TEST_DISK_FULL
flag instead. So the real computation had never executed in any test.

These tests run WITHOUT TEST_MODE and without the flag, so they exercise the
measurement itself.
"""
from __future__ import annotations

import os

import pytest

from agent.orchestrator.engine import disk_over_limit


@pytest.fixture(autouse=True)
def _no_test_seams(monkeypatch):
    """Neither seam may be active: these tests are about the real measurement."""
    monkeypatch.delenv("TEST_DISK_FULL", raising=False)
    monkeypatch.setenv("TEST_MODE", "false")


def test_empty_data_dir_is_not_over_limit(tmp_path):
    """The regression that broke production: a fresh agent must accept missions."""
    assert disk_over_limit(str(tmp_path)) is False


def test_small_data_dir_is_not_over_limit_even_on_a_full_host(tmp_path):
    """The host volume is far past 10 GB; the agent's own footprint is what counts."""
    (tmp_path / "notes.txt").write_bytes(b"x" * 4096)

    assert disk_over_limit(str(tmp_path)) is False, (
        "a 4 KB data dir must not be judged over a 10 GB limit because the host "
        "volume happens to be full"
    )


def test_data_dir_over_the_limit_is_refused(tmp_path, monkeypatch):
    """Shrink the limit rather than write 10 GB, then prove the check still fires."""
    monkeypatch.setattr(
        "agent.orchestrator.engine.OPERATIONAL_CONSTRAINTS",
        {"max_disk_gb": 1 / (1024**3)},  # 1 byte
        raising=False,
    )
    (tmp_path / "big.bin").write_bytes(b"y" * 2048)

    assert disk_over_limit(str(tmp_path)) is True


def test_nested_files_count_towards_the_footprint(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent.orchestrator.engine.OPERATIONAL_CONSTRAINTS",
        {"max_disk_gb": 1024 / (1024**3)},  # 1 KB
        raising=False,
    )
    nested = tmp_path / "logs" / "deep"
    nested.mkdir(parents=True)
    (nested / "a.log").write_bytes(b"z" * 4096)

    assert disk_over_limit(str(tmp_path)) is True


def test_explicit_disk_full_seam_still_wins(tmp_path, monkeypatch):
    """TEST_DISK_FULL remains the declared way to force refusal."""
    monkeypatch.setenv("TEST_DISK_FULL", "1")

    assert disk_over_limit(str(tmp_path)) is True


def test_missing_data_dir_is_not_over_limit(tmp_path):
    """A path that does not exist yet must not wedge the mission API shut."""
    assert disk_over_limit(str(tmp_path / "not-created")) is False

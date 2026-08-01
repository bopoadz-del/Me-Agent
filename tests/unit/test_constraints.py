"""Operational constraint tests (spec Section 10)."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from agent.orchestrator.engine import disk_over_limit
from agent.swarm.spawner import SubAgentSpawner
from common.logging import get_logger
from common.models.schemas import PlanStep

REPO_ROOT = Path(__file__).resolve().parents[2]
PRELOAD = REPO_ROOT / "tests" / "fixtures" / "preload"


def _boot_agent_api(env_extra: dict[str, str], tmp_path: Path) -> tuple[str, str, subprocess.Popen]:
    port = __import__("tests.conftest", fromlist=["free_port"]).free_port()
    env = {
        **os.environ,
        "AGENT_ID": "constraint-test",
        "AGENT_API_TOKEN": "t" * 32,
        "DATA_DIR": str(tmp_path),
        "TEST_MODE": "true",
        "LLM_CLIENT": "mock",
        "AIRGAP": "true",
        "PRELOAD_DIR": str(PRELOAD),
        "SKIP_AIRGAP_VERIFY": "true",
        "AGENT_API_PORT": str(port),
        "PORT": str(port),
        # Do not inherit disk-full seam from a prior test or shell.
        "TEST_DISK_FULL": "",
        **env_extra,
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "agent.bootstrapper"],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            if httpx.get(f"{url}/health", timeout=0.5).status_code == 200:
                return url, env["AGENT_API_TOKEN"], proc
        except Exception:
            pass
        time.sleep(0.05)
    proc.kill()
    raise RuntimeError("agent failed to start for constraint test")


def test_disk_limit_rejects_missions(tmp_path):
    url, token, proc = _boot_agent_api({"TEST_DISK_FULL": "1"}, tmp_path)
    try:
        response = httpx.post(
            f"{url}/mission",
            json={"natural_language": "x"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        )
        assert response.status_code == 429
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_solo_mode_activates(monkeypatch, tmp_path):
    from agent.memory import LocalStore, SyncEngine

    store = LocalStore(str(tmp_path / "agent.db"))

    async def _ws_factory():
        raise RuntimeError("unreachable")

    engine = SyncEngine(store, "agent-1", _ws_factory, "http://127.0.0.1:1")
    frozen = [1_000_000.0]

    def _fake_time() -> float:
        return frozen[0]

    monkeypatch.setattr("agent.memory.time.time", _fake_time)
    engine._last_success = frozen[0]
    engine.note_sync_failure()
    assert engine.solo_mode is False
    frozen[0] = frozen[0] + 8 * 86400.0
    engine.note_sync_failure()
    assert engine.solo_mode is True


def test_spawner_caps_concurrency(registry_with_sleeper, spawner_env, monkeypatch):
    monkeypatch.setattr(
        "agent.swarm.spawner.OPERATIONAL_CONSTRAINTS",
        {"max_subagent_processes": 1, "subagent_default_timeout_sec": 60},
    )
    import agent.swarm.spawner as spawner_mod

    spawner_mod._active_sem = __import__("multiprocessing").get_context("spawn").BoundedSemaphore(1)
    spawner = SubAgentSpawner(registry_with_sleeper)
    step = PlanStep(block_id="sleeper", prompt_scope="sleep", timeout_seconds=1)
    out = spawner.run_block(step, {}, [])
    assert out.validation_status == "failed"


def test_queue_saturation_returns_429(tmp_path):
    url, token, proc = _boot_agent_api({"MISSION_QUEUE_MAX_DEPTH": "0"}, tmp_path)
    try:
        response = httpx.post(
            f"{url}/mission",
            json={"natural_language": "x"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        )
        assert response.status_code == 429
        assert response.json()["detail"] == "queue saturated"
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_logger_rotates(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_ROTATE_MB", "0")
    logger = get_logger("test.rotate")
    handler = logger.handlers[0]
    assert isinstance(handler, logging.handlers.RotatingFileHandler)
    assert handler.maxBytes >= 1024

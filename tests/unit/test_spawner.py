"""Acceptance tests for sub-agent spawner (spec Section 8.5)."""
from __future__ import annotations

import os

import psutil
import pytest

from agent.swarm.spawner import SubAgentSpawner
from agent.swarm.tools import calculator
from common.models.schemas import PlanStep


def test_timeout_kills_process(registry_with_sleeper, spawner_env):
    spawner = SubAgentSpawner(registry_with_sleeper)
    step = PlanStep(
        block_id="sleeper",
        prompt_scope="sleep",
        tool_set=[],
        timeout_seconds=1,
    )
    out = spawner.run_block(step, {}, [])
    assert out.validation_status == "failed"
    assert out.output["reason"] == "timeout"
    assert not psutil.pid_exists(out.output["pid"])


def test_child_env_is_scrubbed(registry_with_envdump, spawner_env):
    os.environ["AGENT_SECRET"] = "s" * 32
    os.environ["AGENT_API_TOKEN"] = "t" * 32
    spawner = SubAgentSpawner(registry_with_envdump)
    step = PlanStep(block_id="envdump", prompt_scope="dump", tool_set=[])
    out = spawner.run_block(step, {}, [])
    child_env = out.output["env"]
    assert not any(k.endswith(("_SECRET", "_TOKEN", "_KEY")) for k in child_env)


def test_airgap_blocks_web_search(registry_with_searcher, spawner_env, monkeypatch):
    monkeypatch.setenv("AIRGAP", "true")
    spawner = SubAgentSpawner(registry_with_searcher)
    step = PlanStep(
        block_id="searcher",
        prompt_scope="search",
        tool_set=["web_search"],
        timeout_seconds=5,
    )
    out = spawner.run_block(step, {}, [])
    assert out.validation_status == "failed"
    assert "airgap" in str(out.output).lower()


def test_file_reader_blocks_escape(spawner_env):
    from agent.swarm.tools import file_reader

    with pytest.raises(PermissionError):
        file_reader("/etc/passwd")
    with pytest.raises(PermissionError):
        file_reader(os.path.join(os.environ["DATA_DIR"], "evidence/../../etc/passwd"))


def test_calculator_rejects_code():
    assert calculator("2 + 3 * 4") == 14
    with pytest.raises(ValueError):
        calculator("__import__('os').system('id')")

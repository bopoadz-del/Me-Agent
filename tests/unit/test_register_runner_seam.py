"""register_runner test seam (amendment A3 / Rule 3)."""
from __future__ import annotations

import os

import pytest

from agent.swarm.registry import BlockRegistry
from agent.swarm.spawner import SubAgentSpawner
from common.models.schemas import BlockDef, PlanStep


def _child_envdump(step, ctx, tools):
    """Module-level so spawn can pickle the child target."""
    return {"ok": True, "env": list(os.environ.keys())}


def _block() -> BlockDef:
    return BlockDef(
        block_id="runner_probe",
        name="runner_probe",
        domain="test",
        input_schema={"type": "object"},
        output_json_schema={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
        },
        system_prompt_template="probe",
    )


def test_register_runner_refuses_outside_test_mode(monkeypatch):
    monkeypatch.setenv("TEST_MODE", "false")
    reg = BlockRegistry()
    with pytest.raises(RuntimeError, match="TEST_MODE"):
        reg.register_runner("runner_probe", lambda *a, **k: {"ok": True})


def test_spawner_refuses_registered_runner_outside_test_mode(monkeypatch, spawner_env):
    monkeypatch.setenv("TEST_MODE", "true")
    reg = BlockRegistry()
    reg.register(_block())

    def _ok_runner(step, ctx, tools):
        return {"ok": True}

    # Use module-level for consistency; inline ok only for register-then-strip guard
    reg.register_runner("runner_probe", _child_envdump)
    monkeypatch.setenv("TEST_MODE", "false")
    spawner = SubAgentSpawner(reg)
    out = spawner.run_block(
        PlanStep(block_id="runner_probe", prompt_scope="x", tool_set=[]),
        {},
        [],
    )
    assert out.validation_status == "failed"
    assert "TEST_MODE" in str(out.output.get("reason", ""))


def test_register_runner_swaps_only_child_target(monkeypatch, spawner_env):
    """Runner runs inside spawn; scrubbed env still applies (no secret leakage)."""
    monkeypatch.setenv("TEST_MODE", "true")
    monkeypatch.setenv("AGENT_SECRET", "s" * 32)
    monkeypatch.setenv("AGENT_API_TOKEN", "t" * 32)

    reg = BlockRegistry()
    reg.register(
        BlockDef(
            block_id="env_via_runner",
            name="env_via_runner",
            domain="test",
            input_schema={"type": "object"},
            output_json_schema={
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "env": {"type": "array"},
                },
                "required": ["ok", "env"],
            },
            system_prompt_template="no harness marker — runner seam only",
        )
    )
    reg.register_runner("env_via_runner", _child_envdump)
    spawner = SubAgentSpawner(reg)
    out = spawner.run_block(
        PlanStep(block_id="env_via_runner", prompt_scope="dump", tool_set=[]),
        {},
        [],
    )
    assert out.validation_status == "passed"
    env_keys = out.output["env"]
    assert not any(k.endswith(("_SECRET", "_TOKEN", "_KEY")) for k in env_keys)
    assert "TEST_MODE" in env_keys

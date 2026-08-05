"""E1–E4 mock-based cold-boot stranger flows under TEST_MODE.

Re-expresses integration guarantees as a stranger cold-boot suite.
Fixtures come from root conftest + tests.e2e.conftest (injection plugin).
"""
from __future__ import annotations

import hashlib
import hmac as hmac_mod
import os
import sqlite3

import httpx
import pytest
import websockets

from common.models.schemas import MissionProfile

pytestmark = pytest.mark.asyncio


# ── E1: bootstrapper ready (cold boot) ───────────────────────────────────────

async def test_e1_bootstrapper_ready_after_cold_boot(
    hive_with_deltas, clean_env, tmp_path, agent_env
):
    """Stranger cold-boot: online agent reaches hive_connected with memory."""
    os.environ.update(agent_env)
    from agent import bootstrapper

    result = bootstrapper.main(serve=False)
    assert result["hive_connected"] is True
    con = sqlite3.connect(os.path.join(str(tmp_path), "agent.db"))
    try:
        assert con.execute("SELECT COUNT(*) FROM memory_deltas").fetchone()[0] >= 1
    finally:
        con.close()


# ── E2: mission happy path ───────────────────────────────────────────────────

async def test_e2_mission_happy_path(orchestrator_with_adder, evidence_file):
    """MockLLM mission: resolve + execute adder with evidence."""
    orch = orchestrator_with_adder
    profile = MissionProfile(
        natural_language="add the two numbers in the evidence",
        domain_hint="math",
        attached_evidence=[{"type": "txt", "path": evidence_file}],
    )
    plan = orch.resolve(profile)
    assert plan.steps and plan.steps[0].block_id == "adder"
    result = orch.execute(plan)
    assert result.status == "success"
    assert result.outputs[0].output["sum"] == 8
    assert result.outputs[0].validation_status == "passed"
    assert result.citations and "a=5" in result.citations[0].excerpt


# ── E3: refusal + injection ──────────────────────────────────────────────────

async def test_e3_refusal_without_evidence(orchestrator_with_port_blocks):
    orch = orchestrator_with_port_blocks
    profile = MissionProfile(
        natural_language="Calculate berth fee",
        domain_hint="port_terminal",
        attached_evidence=[],
    )
    result = orch.execute(orch.resolve(profile))
    assert result.status == "failure"
    assert result.confidence == 0.0
    assert any(
        "evidence" in str(o.output.get("reason", "")).lower() for o in result.outputs
    )


async def test_e3_injection_cannot_steer_confidence(
    orchestrator_with_adder_injection, poisoned_evidence
):
    """Thin re-expression of Rule 9 injection canary under e2e naming."""
    orch = orchestrator_with_adder_injection
    profile = MissionProfile(
        natural_language="add the two numbers in the evidence",
        domain_hint="math",
        attached_evidence=[{"type": "txt", "path": poisoned_evidence}],
    )
    result = orch.execute(orch.resolve(profile))
    assert result.status == "success"
    assert result.outputs[0].output["sum"] == 8
    assert result.confidence == 1.0
    assert result.citations[0].confidence == 1.0


# ── E4: auth ─────────────────────────────────────────────────────────────────

async def test_e4_auth_challenge_and_bearer_gate(hive, enrolled_agent, agent_api):
    """Stranger auth: proof issues JWT; mission API requires bearer."""
    agent_id, secret = enrolled_agent
    async with httpx.AsyncClient(base_url=hive.url) as c:
        nonce = (
            await c.get("/auth/challenge", params={"agent_id": agent_id})
        ).json()["nonce"]
        proof = hmac_mod.new(
            secret.encode(), nonce.encode(), hashlib.sha256
        ).hexdigest()
        r = await c.post(
            "/auth/prove",
            json={"agent_id": agent_id, "nonce": nonce, "proof": proof},
        )
        assert r.status_code == 200
        assert r.json()["session_token"]

    async with httpx.AsyncClient(base_url=agent_api.url) as c:
        denied = await c.post("/mission", json={"natural_language": "x"})
        assert denied.status_code == 401
        health = await c.get("/health")
        assert health.status_code == 200

    async with websockets.connect(hive.ws_url) as ws:
        await ws.send('{"auth": "not-a-jwt"}')
        with pytest.raises(websockets.ConnectionClosed) as exc:
            await ws.recv()
        assert exc.value.rcvd.code == 4401

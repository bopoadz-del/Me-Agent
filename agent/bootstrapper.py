"""Agent bootstrapper — env validation, auth, API server."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import sqlite3
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Any, Optional

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from agent.comms import CommsLink
from agent.memory import LocalStore, Retriever, SyncEngine
from agent.orchestrator.engine import HeadlessOrchestrator, disk_over_limit
from agent.swarm.registry import BlockRegistry
from agent.swarm.spawner import SubAgentSpawner
from common.logging import OPERATIONAL_CONSTRAINTS, get_logger
from common.models.llm_client import MockLLMClient, OllamaClient
from common.models.schemas import BlockDef, MemoryDelta, MissionProfile, SessionGrant

_logger = get_logger("agent.bootstrapper")
_RUNTIME: dict[str, Any] = {}


class MissionCreateBody(BaseModel):
    natural_language: str
    domain_hint: Optional[str] = None
    attached_evidence: list[dict[str, str]] = Field(default_factory=list)
    required_confidence: float = 0.9
    max_cost_usd: float = 1.0
    priority: int = 5


def _mission_queue_max_depth() -> int:
    raw = os.environ.get("MISSION_QUEUE_MAX_DEPTH")
    if raw is not None and str(raw).strip() != "":
        return max(0, int(raw))
    return int(OPERATIONAL_CONSTRAINTS["mission_queue_max_depth"])


def _build_llm():
    """Prefer MockLLM under TEST_MODE; OLLAMA_MODEL overrides host model name."""
    if os.environ.get("LLM_CLIENT", "").lower() == "mock":
        return MockLLMClient()
    model = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b-instruct")
    return OllamaClient(model=model)


class MissionQueue:
    """In-process mission queue with configurable max depth."""

    def __init__(self, max_depth: Optional[int] = None) -> None:
        depth = max_depth if max_depth is not None else _mission_queue_max_depth()
        self._max_depth = max(0, int(depth))
        self._items: deque[Any] = deque()

    def enqueue(self, item: Any) -> bool:
        if len(self._items) >= self._max_depth:
            return False
        self._items.append(item)
        return True

    def depth(self) -> int:
        return len(self._items)


def _fail(message: str) -> None:
    sys.stderr.write(message + "\n")
    raise SystemExit(1)


def _require(name: str) -> str:
    val = os.environ.get(name)
    if val is None or not str(val).strip():
        _fail(f"missing required environment variable: {name}")
    return str(val).strip()


def _validate_env() -> None:
    _require("AGENT_ID")
    _require("AGENT_API_TOKEN")
    airgap = os.environ.get("AIRGAP", "false").lower() == "true"
    if not airgap:
        _require("HIVE_URL")
        _require("AGENT_SECRET")
    test_mode = os.environ.get("TEST_MODE", "").lower() == "true"
    if os.environ.get("LLM_CLIENT", "").lower() == "mock" and not test_mode:
        _fail("LLM_CLIENT=mock requires TEST_MODE=true")
    if os.environ.get("INPROCESS_BROKER", "").lower() == "true" and not test_mode:
        _fail("INPROCESS_BROKER requires TEST_MODE=true")


def _constant_time_eq(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode(), b.encode())


def _data_dir() -> str:
    return os.environ.get("DATA_DIR", "/data")


def _db_path() -> str:
    return str(Path(_data_dir()) / "agent.db")


def _preload_dir() -> Path:
    return Path(os.environ.get("PRELOAD_DIR", str(Path(_data_dir()) / "preload")))


def _load_preload(store: LocalStore) -> None:
    preload = _preload_dir()
    mem_file = preload / "memory.sqlite"
    blocks_file = preload / "blocks.json"
    if mem_file.is_file():
        src = sqlite3.connect(str(mem_file))
        src.row_factory = sqlite3.Row
        rows = src.execute(
            "SELECT delta_id, key, value, origin, vector_clock, operation, timestamp FROM memory_deltas"
        ).fetchall()
        deltas = []
        for row in rows:
            deltas.append(
                MemoryDelta(
                    delta_id=row["delta_id"],
                    key=row["key"],
                    value=json.loads(row["value"]) if row["value"] else None,
                    origin=row["origin"],
                    vector_clock=json.loads(row["vector_clock"]),
                    operation=row["operation"],
                )
            )
        store.apply_memory_dump(deltas)
        src.close()
    if blocks_file.is_file():
        raw = json.loads(blocks_file.read_text(encoding="utf-8"))
        blocks = [BlockDef.model_validate(item) for item in raw]
        store.apply_blocks(blocks)


def _authenticate_online() -> SessionGrant:
    hive_url = _require("HIVE_URL").rstrip("/")
    agent_id = _require("AGENT_ID")
    secret = _require("AGENT_SECRET")
    with httpx.Client(base_url=hive_url, timeout=30.0) as client:
        nonce = client.get("/auth/challenge", params={"agent_id": agent_id}).json()["nonce"]
        proof = hmac.new(secret.encode(), nonce.encode(), hashlib.sha256).hexdigest()
        response = client.post(
            "/auth/prove",
            json={"agent_id": agent_id, "nonce": nonce, "proof": proof},
        )
        if response.status_code != 200:
            _fail(f"hive auth failed: {response.status_code} {response.text}")
        return SessionGrant.model_validate(response.json())


def _build_runtime(store: LocalStore, grant: Optional[SessionGrant] = None) -> dict[str, Any]:
    registry = BlockRegistry()
    for block in store.load_blocks():
        registry.register(block)
    if grant is not None:
        for block in grant.blocks:
            registry.register(block)
    llm = _build_llm()
    spawner = SubAgentSpawner(registry, llm_client=llm)
    retriever = Retriever(store)
    orchestrator = HeadlessOrchestrator(registry, retriever, spawner, store)
    comms = CommsLink(orchestrator)
    sync_engine: Optional[SyncEngine] = None
    if grant is not None:
        sync_engine = SyncEngine(
            store,
            _require("AGENT_ID"),
            _require("HIVE_URL"),
            grant.session_token,
            comms=comms,
        )

        def _publish_to_hive(message: dict[str, Any]) -> None:
            agent_id = _require("AGENT_ID")
            clock = dict(sync_engine.vector_clock)
            clock[agent_id] = clock.get(agent_id, 0) + 1
            sync_engine.vector_clock = clock
            frame = {
                "msg_type": message.get("msg_type", "block_update"),
                "session_id": agent_id,
                "payload": message.get("payload", message),
                "vector_clock": clock,
            }
            sync_engine.send_outbound(frame)

        comms.publish_fn = _publish_to_hive
    return {
        "store": store,
        "registry": registry,
        "orchestrator": orchestrator,
        "comms": comms,
        "sync_engine": sync_engine,
        "grant": grant,
    }


def _create_api(runtime: dict[str, Any]) -> FastAPI:
    api_token = _require("AGENT_API_TOKEN")
    app = FastAPI(title="self-agent")

    def _auth(authorization: Optional[str] = Header(default=None)) -> None:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="unauthorized")
        token = authorization.split(" ", 1)[1].strip()
        if not _constant_time_eq(token, api_token):
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/mission", dependencies=[Depends(_auth)])
    async def post_mission(payload: MissionCreateBody) -> dict[str, Any]:
        # Re-read constraints at request time (subprocess env / seams).
        if disk_over_limit(_data_dir()):
            raise HTTPException(status_code=429, detail="disk limit exceeded")
        comms: CommsLink = runtime["comms"]
        max_depth = _mission_queue_max_depth()
        if hasattr(comms, "_max_depth"):
            comms._max_depth = max_depth
        if comms.queue_depth() >= max_depth:
            raise HTTPException(status_code=429, detail="queue saturated")
        profile = MissionProfile(
            natural_language=payload.natural_language,
            domain_hint=payload.domain_hint,
            attached_evidence=list(payload.attached_evidence or []),
            required_confidence=payload.required_confidence,
            max_cost_usd=payload.max_cost_usd,
            priority=payload.priority,
        )
        runtime["store"].last_profile = profile
        if not comms.enqueue(profile):
            raise HTTPException(status_code=429, detail="queue saturated")
        return {"mission_id": profile.mission_id, "status": "queued"}

    @app.get("/mission/{mission_id}", dependencies=[Depends(_auth)])
    async def get_mission(mission_id: str) -> dict[str, Any]:
        row = runtime["store"].conn.execute(
            "SELECT status, checkpoint FROM missions WHERE mission_id = ?",
            (mission_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="not found")
        return {
            "mission_id": mission_id,
            "status": row["status"],
            "checkpoint": json.loads(row["checkpoint"] or "{}"),
        }

    return app


def _resume_running_missions(runtime: dict[str, Any], online: bool) -> None:
    store: LocalStore = runtime["store"]
    for state in store.running_missions():
        state.resumed_from = state.mission_id
        store.save_mission_state(state)
        if online and runtime.get("sync_engine") is not None:
            runtime["comms"].handle_command({"action": "resume", "mission_id": state.mission_id})


def boot(*, serve: bool = True) -> dict[str, Any]:
    _validate_env()
    data_dir = _data_dir()
    os.makedirs(data_dir, exist_ok=True)
    if disk_over_limit(data_dir):
        _logger.warning("disk usage over limit — read-only inspection mode")
    airgap = os.environ.get("AIRGAP", "false").lower() == "true"
    store = LocalStore(_db_path())
    grant: Optional[SessionGrant] = None
    hive_connected = False
    if airgap:
        _load_preload(store)
        skip = os.environ.get("SKIP_AIRGAP_VERIFY", "false").lower() == "true"
        if not skip:
            from agent.security.airgap import AirGapVerifier

            report = AirGapVerifier().verify()
            if not report.verified:
                _fail("airgap verification failed")
    else:
        grant = _authenticate_online()
        hive_connected = True
        store.apply_memory_dump(grant.memory_dump)
        store.apply_blocks(grant.blocks)
    runtime = _build_runtime(store, grant)
    _RUNTIME.clear()
    _RUNTIME.update(runtime)
    if runtime["sync_engine"] is not None:
        runtime["sync_engine"].start()
    runtime["comms"].start()
    _resume_running_missions(runtime, online=not airgap)
    result = {"airgap": airgap, "hive_connected": hive_connected}
    if not serve:
        return result
    port = int(os.environ.get("AGENT_API_PORT") or os.environ.get("PORT", "8000"))
    app = _create_api(runtime)
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(config)
    server.run()
    return result


def main(serve: bool = True) -> dict[str, Any]:
    return boot(serve=serve)


if __name__ == "__main__":
    main()

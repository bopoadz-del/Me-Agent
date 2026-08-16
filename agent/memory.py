"""Local memory store, retriever, and sync engine."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from common.logging import OPERATIONAL_CONSTRAINTS, get_logger
from common.models.schemas import BlockDef, MemoryDelta, MissionState, StepOutput, SyncConflict
from common.storage.db import connect, migrate

_logger = get_logger("agent.memory")


def _agent_db_path(data_dir: str) -> str:
    return str(Path(data_dir) / "agent.db")


def _ensure_agent_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS missions (
            mission_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            checkpoint TEXT NOT NULL DEFAULT '{}',
            resumed_from TEXT,
            profile_json TEXT,
            result_json TEXT
        );
        CREATE TABLE IF NOT EXISTS agent_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    # Agent DBs created before result persistence existed lack result_json.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(missions)")}
    if "result_json" not in columns:
        conn.execute("ALTER TABLE missions ADD COLUMN result_json TEXT")
    conn.commit()


class LocalStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn = connect(db_path)
        migrate(self.conn)
        _ensure_agent_tables(self.conn)
        self.last_profile: Any = None

    def apply_memory_dump(self, deltas: list[MemoryDelta]) -> None:
        for delta in deltas:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO memory_deltas
                    (delta_id, key, value, origin, vector_clock, operation, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    delta.delta_id,
                    delta.key,
                    json.dumps(delta.value) if delta.value is not None else None,
                    delta.origin,
                    json.dumps(delta.vector_clock),
                    delta.operation,
                    delta.timestamp.isoformat(),
                ),
            )
        self.conn.commit()

    def apply_blocks(self, blocks: list[BlockDef]) -> None:
        for block in blocks:
            payload = block.model_dump(mode="json")
            self.conn.execute(
                """
                INSERT OR REPLACE INTO block_registry(block_id, def_json, version_clock)
                VALUES (?, ?, ?)
                """,
                (block.block_id, json.dumps(payload), block.version_clock),
            )
        self.conn.commit()

    def load_blocks(self) -> list[BlockDef]:
        rows = self.conn.execute("SELECT def_json FROM block_registry").fetchall()
        return [BlockDef.model_validate(json.loads(r["def_json"])) for r in rows]

    def save_mission_state(self, state: MissionState) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO missions(mission_id, status, checkpoint, resumed_from, profile_json)
            VALUES (?, ?, ?, ?, COALESCE((SELECT profile_json FROM missions WHERE mission_id = ?), '{}'))
            """,
            (
                state.mission_id,
                state.status,
                json.dumps(state.checkpoint),
                state.resumed_from,
                state.mission_id,
            ),
        )
        self.conn.commit()

    def save_mission_result(self, result: Any) -> None:
        """Persist the terminal ExecutionResult so the agent API can report it.

        The worker previously published the result to the Hive and discarded it, which
        left `GET /mission/{id}` unable to expose citations or confidence -- the two
        things spec Section 11.4 step 7 asserts.
        """
        payload = result.model_dump(mode="json")
        self.conn.execute(
            """
            INSERT INTO missions(mission_id, status, checkpoint, resumed_from, profile_json, result_json)
            VALUES (?, ?, '{}', NULL, '{}', ?)
            ON CONFLICT(mission_id) DO UPDATE SET
                status = excluded.status,
                result_json = excluded.result_json
            """,
            (payload["mission_id"], payload["status"], json.dumps(payload)),
        )
        self.conn.commit()

    def get_mission_result(self, mission_id: str) -> Optional[dict[str, Any]]:
        """Return the stored ExecutionResult dump, or None if the mission has not finished."""
        row = self.conn.execute(
            "SELECT result_json FROM missions WHERE mission_id = ?",
            (mission_id,),
        ).fetchone()
        if row is None or not row["result_json"]:
            return None
        return json.loads(row["result_json"])

    def update_mission_status(self, mission_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE missions SET status = ? WHERE mission_id = ?",
            (status, mission_id),
        )
        self.conn.commit()

    def checkpoint(self, mission_id: str, step_output: StepOutput) -> None:
        row = self.conn.execute(
            "SELECT checkpoint FROM missions WHERE mission_id = ?",
            (mission_id,),
        ).fetchone()
        data: dict[str, Any] = {}
        if row is not None and row["checkpoint"]:
            data = json.loads(row["checkpoint"])
        data[step_output.step_id] = step_output.model_dump(mode="json")
        self.conn.execute(
            "UPDATE missions SET checkpoint = ?, status = 'running' WHERE mission_id = ?",
            (json.dumps(data), mission_id),
        )
        self.conn.commit()

    def running_missions(self) -> list[MissionState]:
        rows = self.conn.execute(
            "SELECT mission_id, status, checkpoint, resumed_from FROM missions WHERE status = 'running'"
        ).fetchall()
        out: list[MissionState] = []
        for row in rows:
            out.append(
                MissionState(
                    mission_id=row["mission_id"],
                    status=row["status"],
                    checkpoint=json.loads(row["checkpoint"] or "{}"),
                    resumed_from=row["resumed_from"],
                )
            )
        return out

    def pause_running_missions(self) -> None:
        self.conn.execute("UPDATE missions SET status = 'paused' WHERE status = 'running'")
        self.conn.commit()

    def record_conflict(self, conflict: SyncConflict) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO sync_conflicts
                (conflict_id, key, hive_value, agent_value, hive_clock, agent_clock,
                 winner, resolved_at, replayed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conflict.conflict_id,
                conflict.key,
                json.dumps(conflict.hive_value) if conflict.hive_value is not None else None,
                json.dumps(conflict.agent_value) if conflict.agent_value is not None else None,
                json.dumps(conflict.hive_clock),
                json.dumps(conflict.agent_clock),
                conflict.winner,
                conflict.resolved_at.isoformat(),
                1 if conflict.replayed else 0,
            ),
        )
        self.conn.commit()

    def list_conflicts(self) -> list[SyncConflict]:
        rows = self.conn.execute("SELECT * FROM sync_conflicts").fetchall()
        out: list[SyncConflict] = []
        for row in rows:
            out.append(
                SyncConflict(
                    conflict_id=row["conflict_id"],
                    key=row["key"],
                    hive_value=json.loads(row["hive_value"]) if row["hive_value"] else None,
                    agent_value=json.loads(row["agent_value"]) if row["agent_value"] else None,
                    hive_clock=json.loads(row["hive_clock"]),
                    agent_clock=json.loads(row["agent_clock"]),
                    winner=row["winner"],
                    resolved_at=datetime.fromisoformat(row["resolved_at"]),
                    replayed=bool(row["replayed"]),
                )
            )
        return out


class Retriever:
    def __init__(self, store: Optional[LocalStore] = None) -> None:
        self.store = store

    def search_blocks(self, query: str) -> list[BlockDef]:
        if self.store is None:
            return []
        blocks = self.store.load_blocks()
        q = query.lower()
        return [b for b in blocks if q in b.domain.lower() or q in b.name.lower()]


class SyncEngine:
    """Background sync with hive; solo mode after threshold days unreachable."""

    def __init__(
        self,
        store: LocalStore,
        agent_id: str,
        hive_url_or_factory: Any,
        session_token_or_url: Optional[str] = None,
        comms: Any = None,
    ) -> None:
        self.store = store
        self.agent_id = agent_id
        self.comms = comms
        if callable(hive_url_or_factory):
            self._ws_factory = hive_url_or_factory
            self.hive_url = str(session_token_or_url or "").rstrip("/")
            self.session_token = ""
        else:
            self._ws_factory = None
            self.hive_url = str(hive_url_or_factory).rstrip("/")
            self.session_token = str(session_token_or_url or "")
        self.vector_clock: dict[str, int] = {agent_id: 0}
        self._solo_mode = False
        self._last_success = time.time()
        self._last_warning = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._outbound: deque[dict[str, Any]] = deque()
        self._out_lock = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_loop, name="sync-engine", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)

    def mark_success(self) -> None:
        self._last_success = time.time()
        self._solo_mode = False

    def mark_failure(self) -> None:
        threshold = OPERATIONAL_CONSTRAINTS["solo_mode_threshold_days"] * 86400
        if time.time() - self._last_success > threshold:
            self._solo_mode = True
            if time.time() - self._last_warning > 86400:
                _logger.warning("solo mode active — hive unreachable")
                self._last_warning = time.time()

    def note_sync_failure(self) -> None:
        """Alias used by constraint acceptance tests (Section 10)."""
        self.mark_failure()

    @property
    def solo_mode(self) -> bool:
        return self._solo_mode

    def send_outbound(self, message: dict[str, Any]) -> None:
        with self._out_lock:
            self._outbound.append(message)

    def _drain_outbound(self) -> list[dict[str, Any]]:
        with self._out_lock:
            items = list(self._outbound)
            self._outbound.clear()
        return items

    def _run_loop(self) -> None:
        import asyncio

        asyncio.run(self._async_loop())

    async def _async_loop(self) -> None:
        import websockets

        ws_url = self.hive_url.replace("http://", "ws://").replace("https://", "wss://") + "/sync"
        while not self._stop.is_set():
            try:
                if self._ws_factory is not None:
                    ws = await self._ws_factory()
                    await self._session(ws)
                else:
                    async with websockets.connect(ws_url) as ws:
                        await self._session(ws)
            except Exception:
                self.mark_failure()
                await _async_sleep(2.0)

    async def _session(self, ws: Any) -> None:
        await ws.send(json.dumps({"auth": self.session_token}))
        self.mark_success()
        while not self._stop.is_set():
            for outbound in self._drain_outbound():
                frame = {
                    "msg_type": outbound.get("msg_type", "block_update"),
                    "session_id": self.agent_id,
                    "payload": outbound.get("payload", outbound),
                    "vector_clock": outbound.get("vector_clock") or dict(self.vector_clock),
                }
                await ws.send(json.dumps(frame))
            await ws.send(
                json.dumps(
                    {
                        "msg_type": "heartbeat",
                        "session_id": self.agent_id,
                        "payload": {},
                        "vector_clock": dict(self.vector_clock),
                    }
                )
            )
            # Heartbeat may return multiple frames (deltas, commands, ack).
            while not self._stop.is_set():
                raw = await ws.recv()
                msg = json.loads(raw)
                self._handle_message(msg)
                if msg.get("msg_type") == "ack":
                    break
            await _async_sleep(0.2)

    def _handle_message(self, msg: dict[str, Any]) -> None:
        msg_type = msg.get("msg_type")
        payload = msg.get("payload") or {}
        clock = msg.get("vector_clock") or {}
        if clock:
            for node, val in clock.items():
                self.vector_clock[node] = max(self.vector_clock.get(node, 0), int(val))
        if msg_type == "memory_delta":
            delta = MemoryDelta(
                key=payload["key"],
                value=payload.get("value"),
                origin=payload.get("origin", "hive"),
                vector_clock=clock or {self.agent_id: self.vector_clock.get(self.agent_id, 0)},
                operation=payload.get("operation", "upsert"),
            )
            self.store.apply_memory_dump([delta])
        elif msg_type == "conflict":
            conflict = SyncConflict(
                conflict_id=payload.get("conflict_id", ""),
                key=payload.get("key", ""),
                hive_value=payload.get("hive_value"),
                agent_value=payload.get("agent_value"),
                hive_clock=clock,
                agent_clock=dict(self.vector_clock),
                winner="hive",
            )
            self.store.record_conflict(conflict)
        elif msg_type == "command" and self.comms is not None:
            self.comms.handle_command(payload)
        elif msg_type == "block_update":
            blocks_raw = payload.get("blocks") or []
            if blocks_raw:
                blocks = [BlockDef.model_validate(b) for b in blocks_raw]
                self.store.apply_blocks(blocks)
                if self.comms is not None and hasattr(self.comms, "orchestrator"):
                    orch = self.comms.orchestrator
                    if hasattr(orch, "registry"):
                        for block in blocks:
                            orch.registry.register(block)


async def _async_sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)

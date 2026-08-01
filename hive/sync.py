"""Vector-clock comparison and hive-wins conflict resolution."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from common.models.schemas import HiveMessage, MemoryDelta, SyncConflict
from common.storage.db import embedding_dim


def clock_dominates(a: dict[str, int], b: dict[str, int]) -> bool:
    nodes = set(a) | set(b)
    ge_all = all(a.get(n, 0) >= b.get(n, 0) for n in nodes)
    gt_any = any(a.get(n, 0) > b.get(n, 0) for n in nodes)
    return ge_all and gt_any


def clocks_concurrent(a: dict[str, int], b: dict[str, int]) -> bool:
    return (not clock_dominates(a, b)) and (not clock_dominates(b, a))


def merge_clocks(*clocks: dict[str, int]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for clock in clocks:
        for node, value in clock.items():
            merged[node] = max(merged.get(node, 0), value)
    return merged


def bump_hive(clock: dict[str, int]) -> dict[str, int]:
    out = dict(clock)
    out["hive"] = out.get("hive", 0) + 1
    return out


def _parse_json(raw: Optional[str]) -> Any:
    if raw is None:
        return None
    return json.loads(raw)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def hive_vector_clock(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT vector_clock FROM memory_deltas").fetchall()
    clock: dict[str, int] = {"hive": 0}
    for row in rows:
        clock = merge_clocks(clock, json.loads(row["vector_clock"]))
    if "hive" not in clock:
        clock["hive"] = 0
    return clock


def delta_unseen_by(delta_clock: dict[str, int], agent_clock: dict[str, int]) -> bool:
    nodes = set(delta_clock) | set(agent_clock)
    return any(delta_clock.get(n, 0) > agent_clock.get(n, 0) for n in nodes)


def load_deltas(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT delta_id, key, value, origin, vector_clock, operation, timestamp "
        "FROM memory_deltas ORDER BY timestamp ASC"
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "delta_id": row["delta_id"],
                "key": row["key"],
                "value": _parse_json(row["value"]),
                "origin": row["origin"],
                "vector_clock": json.loads(row["vector_clock"]),
                "operation": row["operation"],
                "timestamp": row["timestamp"],
            }
        )
    return out


def latest_state_for_key(
    conn: sqlite3.Connection, key: str
) -> Optional[dict[str, Any]]:
    rows = conn.execute(
        "SELECT delta_id, key, value, origin, vector_clock, operation, timestamp "
        "FROM memory_deltas WHERE key = ? ORDER BY timestamp ASC",
        (key,),
    ).fetchall()
    if not rows:
        return None
    best = rows[0]
    best_clock = json.loads(best["vector_clock"])
    for row in rows[1:]:
        clock = json.loads(row["vector_clock"])
        if clock_dominates(clock, best_clock) or not clock_dominates(best_clock, clock):
            # Prefer dominating; if concurrent/equal keep later row (hive-seeded wins later resolve)
            if clock_dominates(clock, best_clock) or (
                not clock_dominates(best_clock, clock) and row["timestamp"] >= best["timestamp"]
            ):
                best = row
                best_clock = clock
    return {
        "delta_id": best["delta_id"],
        "key": best["key"],
        "value": _parse_json(best["value"]),
        "origin": best["origin"],
        "vector_clock": best_clock,
        "operation": best["operation"],
        "timestamp": best["timestamp"],
    }


def insert_delta(
    conn: sqlite3.Connection,
    *,
    key: str,
    value: Optional[dict[str, Any]],
    origin: str,
    vector_clock: dict[str, int],
    operation: str = "upsert",
    embedding: Optional[list[float]] = None,
    delta_id: Optional[str] = None,
) -> dict[str, Any]:
    did = delta_id or str(uuid.uuid4())
    ts = _iso_now()
    conn.execute(
        """
        INSERT INTO memory_deltas
            (delta_id, key, value, origin, vector_clock, operation, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            did,
            key,
            json.dumps(value) if value is not None else None,
            origin,
            json.dumps(vector_clock),
            operation,
            ts,
        ),
    )
    if embedding is not None:
        dim = embedding_dim()
        if len(embedding) != dim:
            raise ValueError(
                f"embedding dimension mismatch: got {len(embedding)}, expected {dim}"
            )
        # sqlite-vec: insert into virtual table; join by rowid of memory_deltas
        row = conn.execute(
            "SELECT rowid FROM memory_deltas WHERE delta_id = ?", (did,)
        ).fetchone()
        if row is not None:
            conn.execute(
                "INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)",
                (int(row["rowid"]), json.dumps(embedding)),
            )
    conn.commit()
    return {
        "delta_id": did,
        "key": key,
        "value": value,
        "origin": origin,
        "vector_clock": vector_clock,
        "operation": operation,
        "timestamp": ts,
    }


def archive_conflict(
    conn: sqlite3.Connection,
    *,
    key: str,
    hive_value: Optional[dict[str, Any]],
    agent_value: Optional[dict[str, Any]],
    hive_clock: dict[str, int],
    agent_clock: dict[str, int],
) -> SyncConflict:
    conflict = SyncConflict(
        key=key,
        hive_value=hive_value,
        agent_value=agent_value,
        hive_clock=hive_clock,
        agent_clock=agent_clock,
        winner="hive",
    )
    conn.execute(
        """
        INSERT INTO sync_conflicts
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
    conn.commit()
    return conflict


def heartbeat_response(
    conn: sqlite3.Connection,
    session_id: str,
    agent_clock: dict[str, int],
) -> list[HiveMessage]:
    messages: list[HiveMessage] = []
    for delta in load_deltas(conn):
        if delta_unseen_by(delta["vector_clock"], agent_clock):
            messages.append(
                HiveMessage(
                    msg_type="memory_delta",
                    session_id=session_id,
                    payload={
                        "delta_id": delta["delta_id"],
                        "key": delta["key"],
                        "value": delta["value"],
                        "origin": delta["origin"],
                        "operation": delta["operation"],
                        "timestamp": delta["timestamp"],
                    },
                    vector_clock=delta["vector_clock"],
                )
            )
    # Deliver pending messages as commands
    pending = conn.execute(
        "SELECT id, payload FROM messages WHERE target_agent_id = ? AND delivered = 0",
        (session_id,),
    ).fetchall()
    for row in pending:
        messages.append(
            HiveMessage(
                msg_type="command",
                session_id=session_id,
                payload=json.loads(row["payload"]),
                vector_clock=hive_vector_clock(conn),
            )
        )
        conn.execute("UPDATE messages SET delivered = 1 WHERE id = ?", (row["id"],))
    conn.commit()
    messages.append(
        HiveMessage(
            msg_type="ack",
            session_id=session_id,
            payload={},
            vector_clock=hive_vector_clock(conn),
        )
    )
    return messages


def handle_agent_delta(
    conn: sqlite3.Connection,
    session_id: str,
    payload: dict[str, Any],
    agent_clock: dict[str, int],
) -> list[HiveMessage]:
    key = payload["key"]
    operation = payload.get("operation", "upsert")
    value = payload.get("value")
    embedding = payload.get("embedding")
    hive_state = latest_state_for_key(conn, key)

    if hive_state is None:
        merged = bump_hive(merge_clocks(agent_clock, {"hive": 0}))
        stored = insert_delta(
            conn,
            key=key,
            value=value,
            origin=session_id,
            vector_clock=merged,
            operation=operation,
            embedding=embedding,
        )
        return [
            HiveMessage(
                msg_type="memory_delta",
                session_id=session_id,
                payload={
                    "delta_id": stored["delta_id"],
                    "key": key,
                    "value": value,
                    "origin": session_id,
                    "operation": operation,
                },
                vector_clock=merged,
            ),
            HiveMessage(
                msg_type="ack",
                session_id=session_id,
                payload={},
                vector_clock=hive_vector_clock(conn),
            ),
        ]

    hive_clock = hive_state["vector_clock"]
    if clock_dominates(agent_clock, hive_clock):
        merged = bump_hive(merge_clocks(hive_clock, agent_clock))
        stored = insert_delta(
            conn,
            key=key,
            value=value,
            origin=session_id,
            vector_clock=merged,
            operation=operation,
            embedding=embedding,
        )
        return [
            HiveMessage(
                msg_type="memory_delta",
                session_id=session_id,
                payload={
                    "delta_id": stored["delta_id"],
                    "key": key,
                    "value": value,
                    "origin": session_id,
                    "operation": operation,
                },
                vector_clock=merged,
            ),
            HiveMessage(
                msg_type="ack",
                session_id=session_id,
                payload={},
                vector_clock=hive_vector_clock(conn),
            ),
        ]

    if clocks_concurrent(agent_clock, hive_clock) or clock_dominates(hive_clock, agent_clock):
        # Hive wins on concurrent; also ignore dominated agent writes with conflict archive
        # only when concurrent (losing agent value preserved)
        if clocks_concurrent(agent_clock, hive_clock):
            conflict = archive_conflict(
                conn,
                key=key,
                hive_value=hive_state["value"],
                agent_value=value,
                hive_clock=hive_clock,
                agent_clock=agent_clock,
            )
            return [
                HiveMessage(
                    msg_type="conflict",
                    session_id=session_id,
                    payload={
                        "conflict_id": conflict.conflict_id,
                        "key": key,
                        "winner": "hive",
                        "resolved_value": hive_state["value"],
                        "hive_value": hive_state["value"],
                        "agent_value": value,
                    },
                    vector_clock=hive_clock,
                )
            ]
        return [
            HiveMessage(
                msg_type="ack",
                session_id=session_id,
                payload={"ignored": True},
                vector_clock=hive_vector_clock(conn),
            )
        ]

    return [
        HiveMessage(
            msg_type="ack",
            session_id=session_id,
            payload={},
            vector_clock=hive_vector_clock(conn),
        )
    ]


def memory_delta_from_row(row: dict[str, Any]) -> MemoryDelta:
    ts_raw = row["timestamp"]
    if isinstance(ts_raw, str):
        ts = datetime.fromisoformat(ts_raw)
    else:
        ts = datetime.now(timezone.utc)
    return MemoryDelta(
        delta_id=row["delta_id"],
        key=row["key"],
        value=row["value"],
        origin=row["origin"],
        vector_clock=row["vector_clock"],
        operation=row["operation"],
        timestamp=ts,
    )

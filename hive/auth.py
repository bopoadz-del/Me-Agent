"""Challenge-response authentication and JWT session issuance."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import jwt

from common.models.schemas import AuthChallenge, AuthProof, BlockDef, MemoryDelta, SessionGrant

AUTH_FAIL_DETAIL = "unauthorized"
NONCE_TTL_SECONDS = 60
SESSION_TTL_SECONDS = 24 * 60 * 60
CHALLENGE_RATE_LIMIT = 10
CHALLENGE_RATE_WINDOW = 60.0


class ChallengeRateLimiter:
    """Sliding-window limiter: 10 challenges per minute per agent_id."""

    def __init__(self, limit: int = CHALLENGE_RATE_LIMIT, window: float = CHALLENGE_RATE_WINDOW) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, agent_id: str) -> bool:
        now = time.monotonic()
        q = self._hits[agent_id]
        while q and (now - q[0]) > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True


def require_jwt_key(jwt_key: Optional[str]) -> str:
    if jwt_key is None or not str(jwt_key).strip() or len(str(jwt_key)) < 32:
        raise RuntimeError(
            "HIVE_JWT_KEY must be set, non-empty, and at least 32 characters"
        )
    return str(jwt_key)


def issue_challenge(conn: sqlite3.Connection, agent_id: str) -> AuthChallenge:
    nonce = secrets.token_hex(32)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=NONCE_TTL_SECONDS)
    conn.execute(
        "INSERT INTO nonces(nonce, agent_id, expires_at, used) VALUES (?, ?, ?, 0)",
        (nonce, agent_id, expires_at.isoformat()),
    )
    conn.commit()
    return AuthChallenge(agent_id=agent_id, nonce=nonce, expires_at=expires_at)


def _expected_proof(secret: str, nonce: str) -> str:
    return hmac.new(secret.encode(), nonce.encode(), hashlib.sha256).hexdigest()


def _load_memory_dump(conn: sqlite3.Connection) -> list[MemoryDelta]:
    rows = conn.execute(
        "SELECT delta_id, key, value, origin, vector_clock, operation, timestamp "
        "FROM memory_deltas ORDER BY timestamp ASC"
    ).fetchall()
    dump: list[MemoryDelta] = []
    for row in rows:
        value = json.loads(row["value"]) if row["value"] is not None else None
        clock = json.loads(row["vector_clock"])
        ts_raw = row["timestamp"]
        if isinstance(ts_raw, str):
            ts = datetime.fromisoformat(ts_raw)
        else:
            ts = datetime.now(timezone.utc)
        dump.append(
            MemoryDelta(
                delta_id=row["delta_id"],
                key=row["key"],
                value=value,
                origin=row["origin"],
                vector_clock=clock,
                operation=row["operation"],
                timestamp=ts,
            )
        )
    return dump


def _load_blocks(conn: sqlite3.Connection) -> list[BlockDef]:
    rows = conn.execute(
        "SELECT def_json FROM block_registry ORDER BY block_id ASC"
    ).fetchall()
    return [BlockDef.model_validate(json.loads(r["def_json"])) for r in rows]


def mint_session_token(agent_id: str, jwt_key: str, *, role: Optional[str] = None) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": agent_id,
        "iat": now,
        "exp": now + SESSION_TTL_SECONDS,
    }
    if role is not None:
        payload["role"] = role
    return jwt.encode(payload, jwt_key, algorithm="HS256")


def mint_admin_token(jwt_key: str, ttl_seconds: int = 3600) -> str:
    key = require_jwt_key(jwt_key)
    now = int(time.time())
    return jwt.encode(
        {"sub": "admin", "role": "admin", "iat": now, "exp": now + ttl_seconds},
        key,
        algorithm="HS256",
    )


def decode_token(token: str, jwt_key: str) -> dict[str, Any]:
    return jwt.decode(token, jwt_key, algorithms=["HS256"])


def verify_and_grant(
    conn: sqlite3.Connection,
    proof: AuthProof,
    jwt_key: str,
) -> tuple[Optional[SessionGrant], int, str]:
    """Return (grant, status_code, detail). status 200 on success."""
    now = datetime.now(timezone.utc)
    row = conn.execute(
        "SELECT agent_id, expires_at, used FROM nonces WHERE nonce = ?",
        (proof.nonce,),
    ).fetchone()
    if row is None:
        return None, 401, AUTH_FAIL_DETAIL
    if int(row["used"]) == 1:
        return None, 401, AUTH_FAIL_DETAIL
    expires_at = datetime.fromisoformat(row["expires_at"])
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < now:
        return None, 401, AUTH_FAIL_DETAIL
    if row["agent_id"] != proof.agent_id:
        return None, 401, AUTH_FAIL_DETAIL

    conn.execute("UPDATE nonces SET used = 1 WHERE nonce = ?", (proof.nonce,))
    conn.commit()

    agent = conn.execute(
        "SELECT agent_id, secret, active FROM agents WHERE agent_id = ?",
        (proof.agent_id,),
    ).fetchone()
    if agent is None:
        return None, 401, AUTH_FAIL_DETAIL

    expected = _expected_proof(agent["secret"], proof.nonce)
    if not hmac.compare_digest(expected, proof.proof):
        return None, 401, AUTH_FAIL_DETAIL

    other = conn.execute(
        "SELECT agent_id FROM agents WHERE active = 1 AND agent_id != ?",
        (proof.agent_id,),
    ).fetchone()
    if other is not None:
        return None, 409, "single-agent limit: another agent is active"

    conn.execute(
        "UPDATE agents SET active = 1 WHERE agent_id = ?",
        (proof.agent_id,),
    )
    conn.commit()

    token = mint_session_token(proof.agent_id, jwt_key)
    grant = SessionGrant(
        session_token=token,
        memory_dump=_load_memory_dump(conn),
        blocks=_load_blocks(conn),
    )
    return grant, 200, "ok"

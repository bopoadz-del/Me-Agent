"""FastAPI Hive app: health, auth, message, admin blocks, WebSocket /sync."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from common.models.schemas import AuthProof, BlockDef, HiveMessage
from common.storage.db import open_db
from hive import auth as hive_auth
from hive import sync as hive_sync

REPO_ROOT = Path(__file__).resolve().parents[1]
WS_AUTH_TIMEOUT_SECONDS = 5.0


def _read_version() -> str:
    version_path = REPO_ROOT / "VERSION"
    if version_path.is_file():
        return version_path.read_text(encoding="utf-8").strip() or "1.0.0"
    return "1.0.0"


def _resolve_db_path(db_path: Optional[str], data_dir: Optional[str]) -> str:
    if db_path:
        return db_path
    env_db = os.environ.get("HIVE_DB_PATH")
    if env_db:
        return env_db
    base = data_dir or os.environ.get("DATA_DIR", "/data")
    return str(Path(base) / "hive.db")


class MessageBody(BaseModel):
    target_agent_id: str
    payload: dict[str, Any]


def create_app(
    db_path: Optional[str] = None,
    jwt_key: Optional[str] = None,
    data_dir: Optional[str] = None,
) -> FastAPI:
    key = hive_auth.require_jwt_key(jwt_key or os.environ.get("HIVE_JWT_KEY"))
    resolved_db = _resolve_db_path(db_path, data_dir)
    if data_dir:
        os.environ["DATA_DIR"] = str(data_dir)
    os.environ["HIVE_DB_PATH"] = resolved_db
    os.environ["HIVE_JWT_KEY"] = key

    conn = open_db(resolved_db)
    limiter = hive_auth.ChallengeRateLimiter()
    version = _read_version()

    app = FastAPI(title="self-agent-hive", version=version)
    app.state.db = conn
    app.state.jwt_key = key
    app.state.db_path = resolved_db
    app.state.rate_limiter = limiter
    app.state.version = version
    app.state.test_broker = None

    def get_conn() -> sqlite3.Connection:
        return app.state.db

    def bearer_claims(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail=hive_auth.AUTH_FAIL_DETAIL)
        token = authorization.split(" ", 1)[1].strip()
        try:
            return hive_auth.decode_token(token, app.state.jwt_key)
        except Exception as exc:
            raise HTTPException(status_code=401, detail=hive_auth.AUTH_FAIL_DETAIL) from exc

    async def _publish(message: dict[str, Any]) -> None:
        broker = getattr(app.state, "test_broker", None)
        if broker is not None and hasattr(broker, "publish"):
            await broker.publish(message)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": app.state.version}

    @app.get("/auth/challenge")
    async def auth_challenge(agent_id: str, db: sqlite3.Connection = Depends(get_conn)):
        if not app.state.rate_limiter.allow(agent_id):
            raise HTTPException(status_code=429, detail="rate limit exceeded")
        challenge = hive_auth.issue_challenge(db, agent_id)
        return challenge.model_dump(mode="json")

    @app.post("/auth/prove")
    async def auth_prove(body: AuthProof, db: sqlite3.Connection = Depends(get_conn)):
        grant, status, detail = hive_auth.verify_and_grant(db, body, app.state.jwt_key)
        if grant is None:
            return JSONResponse(status_code=status, content={"detail": detail})
        return grant.model_dump(mode="json")

    @app.post("/message")
    async def post_message(
        body: MessageBody,
        claims: dict[str, Any] = Depends(bearer_claims),
        db: sqlite3.Connection = Depends(get_conn),
    ):
        _ = claims
        msg_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO messages(id, target_agent_id, payload, delivered) VALUES (?, ?, ?, 0)",
            (msg_id, body.target_agent_id, json.dumps(body.payload)),
        )
        db.commit()
        return {"id": msg_id, "status": "stored"}

    @app.post("/admin/blocks")
    async def admin_blocks(
        blocks: list[BlockDef],
        claims: dict[str, Any] = Depends(bearer_claims),
        db: sqlite3.Connection = Depends(get_conn),
    ):
        if claims.get("role") != "admin":
            raise HTTPException(status_code=403, detail="admin role required")
        changed = False
        for block in blocks:
            existing = db.execute(
                "SELECT def_json, version_clock FROM block_registry WHERE block_id = ?",
                (block.block_id,),
            ).fetchone()
            if existing is None:
                payload = block.model_dump(mode="json")
                db.execute(
                    "INSERT INTO block_registry(block_id, def_json, version_clock) VALUES (?, ?, ?)",
                    (block.block_id, json.dumps(payload), block.version_clock),
                )
                changed = True
                continue
            prev = json.loads(existing["def_json"])
            new_payload = block.model_dump(mode="json")
            if prev != new_payload:
                new_clock = int(existing["version_clock"]) + 1
                new_payload["version_clock"] = new_clock
                db.execute(
                    "UPDATE block_registry SET def_json = ?, version_clock = ? WHERE block_id = ?",
                    (json.dumps(new_payload), new_clock, block.block_id),
                )
                changed = True
        db.commit()
        if changed:
            msg = {
                "msg_type": "block_update",
                "session_id": "admin",
                "payload": {"blocks": [b.model_dump(mode="json") for b in blocks]},
                "vector_clock": hive_sync.hive_vector_clock(db),
            }
            await _publish(msg)
        return {"status": "ok", "changed": changed}

    async def _v2_not_implemented() -> JSONResponse:
        return JSONResponse(status_code=501, content={"detail": "V2"})

    @app.post("/lend")
    async def lend() -> JSONResponse:
        return await _v2_not_implemented()

    @app.post("/lend/redeem")
    async def lend_redeem() -> JSONResponse:
        return await _v2_not_implemented()

    @app.post("/billing/{path:path}")
    async def billing_any(path: str) -> JSONResponse:
        _ = path
        return await _v2_not_implemented()

    @app.websocket("/sync")
    async def sync_ws(websocket: WebSocket) -> None:
        await websocket.accept()
        db: sqlite3.Connection = app.state.db
        agent_id: Optional[str] = None
        try:
            raw = await asyncio.wait_for(
                websocket.receive_text(), timeout=WS_AUTH_TIMEOUT_SECONDS
            )
        except (asyncio.TimeoutError, WebSocketDisconnect):
            await websocket.close(code=4401)
            return
        try:
            frame = json.loads(raw)
            token = frame.get("auth")
            if not token:
                await websocket.close(code=4401)
                return
            claims = hive_auth.decode_token(token, app.state.jwt_key)
            agent_id = str(claims["sub"])
        except Exception:
            await websocket.close(code=4401)
            return

        try:
            while True:
                text = await websocket.receive_text()
                data = json.loads(text)
                msg = HiveMessage.model_validate(data)
                if msg.session_id != agent_id:
                    await websocket.close(code=4403)
                    return
                outbound: list[HiveMessage] = []
                if msg.msg_type == "heartbeat":
                    outbound = hive_sync.heartbeat_response(
                        db, agent_id, msg.vector_clock or {}
                    )
                elif msg.msg_type == "memory_delta":
                    outbound = hive_sync.handle_agent_delta(
                        db, agent_id, msg.payload, msg.vector_clock or {}
                    )
                elif msg.msg_type == "block_update":
                    outbound = [
                        HiveMessage(
                            msg_type="ack",
                            session_id=agent_id,
                            payload={},
                            vector_clock=hive_sync.hive_vector_clock(db),
                        )
                    ]
                    payload = msg.model_dump(mode="json")
                    await _publish(payload)
                else:
                    outbound = [
                        HiveMessage(
                            msg_type="ack",
                            session_id=agent_id,
                            payload={},
                            vector_clock=hive_sync.hive_vector_clock(db),
                        )
                    ]
                for out in outbound:
                    payload = out.model_dump(mode="json")
                    await websocket.send_text(json.dumps(payload))
                    await _publish(payload)
        except WebSocketDisconnect:
            return
        except Exception:
            await websocket.close(code=4401)
            return

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        try:
            app.state.db.close()
        except Exception:
            pass

    return app


def _default_app() -> FastAPI:
    return create_app()


try:
    app = _default_app()
except Exception:
    # Allow import without env during tooling; create_app used by harness.
    app = FastAPI(title="self-agent-hive-unconfigured")

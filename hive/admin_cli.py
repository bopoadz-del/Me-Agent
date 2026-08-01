"""Hive admin CLI — agent enrollment and admin JWT minting."""
from __future__ import annotations

import argparse
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

from common.storage.db import open_db
from hive.auth import mint_admin_token, require_jwt_key


def _db_path_from_env(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    env_db = os.environ.get("HIVE_DB_PATH")
    if env_db:
        return env_db
    data_dir = os.environ.get("DATA_DIR", "/data")
    return str(Path(data_dir) / "hive.db")


def enroll_agent(
    agent_id: str,
    db_path: str | None = None,
    jwt_key: str | None = None,
) -> str:
    """Enroll an agent; return per-agent secret (hex string)."""
    _ = jwt_key  # enrollment does not require JWT; accepted for harness signature
    path = _db_path_from_env(db_path)
    conn = open_db(path)
    secret = secrets.token_hex(32)
    now = datetime.now(timezone.utc).isoformat()
    existing = conn.execute(
        "SELECT agent_id FROM agents WHERE agent_id = ?", (agent_id,)
    ).fetchone()
    if existing is not None:
        conn.execute(
            "UPDATE agents SET secret = ?, enrolled_at = ?, active = 0 WHERE agent_id = ?",
            (secret, now, agent_id),
        )
    else:
        conn.execute(
            "INSERT INTO agents(agent_id, secret, enrolled_at, active) VALUES (?, ?, ?, 0)",
            (agent_id, secret, now),
        )
    conn.commit()
    conn.close()
    return secret


def add_agent(agent_id: str, db_path: str | None = None, jwt_key: str | None = None) -> str:
    return enroll_agent(agent_id, db_path=db_path, jwt_key=jwt_key)


def agent_add(agent_id: str, db_path: str | None = None, jwt_key: str | None = None) -> str:
    return enroll_agent(agent_id, db_path=db_path, jwt_key=jwt_key)


def mint_admin(jwt_key: str | None = None) -> str:
    key = require_jwt_key(jwt_key or os.environ.get("HIVE_JWT_KEY"))
    return mint_admin_token(key)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hive-admin")
    sub = parser.add_subparsers(dest="command", required=True)

    agent_p = sub.add_parser("agent", help="agent enrollment")
    agent_sub = agent_p.add_subparsers(dest="agent_cmd", required=True)
    add_p = agent_sub.add_parser("add", help="enroll a new agent")
    add_p.add_argument("agent_id")
    add_p.add_argument("--db", dest="db_path", default=None)

    admin_p = sub.add_parser("token", help="mint an admin JWT")
    admin_p.add_argument("--jwt-key", dest="jwt_key", default=None)

    args = parser.parse_args(argv)
    if args.command == "agent" and args.agent_cmd == "add":
        secret = enroll_agent(args.agent_id, db_path=args.db_path)
        sys.stdout.write(secret + "\n")
        return 0
    if args.command == "token":
        token = mint_admin(args.jwt_key)
        sys.stdout.write(token + "\n")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

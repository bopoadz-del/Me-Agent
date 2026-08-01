"""Agent CLI — shutdown and conflict management."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from agent.memory import LocalStore
from common.storage.db import open_db


def _db_path() -> str:
    data_dir = os.environ.get("DATA_DIR", "/data")
    return str(Path(data_dir) / "agent.db")


def cmd_shutdown(grace_period: int) -> int:
    store = LocalStore(_db_path())
    store.pause_running_missions()
    deadline = time.time() + grace_period
    while time.time() < deadline:
        running = store.running_missions()
        if not running:
            break
        time.sleep(0.5)
    return 0


def cmd_conflicts_list() -> int:
    store = LocalStore(_db_path())
    rows = store.list_conflicts()
    sys.stdout.write(json.dumps([c.model_dump(mode="json") for c in rows]) + "\n")
    return 0


def cmd_conflicts_replay(conflict_id: str) -> int:
    store = LocalStore(_db_path())
    row = store.conn.execute(
        "SELECT * FROM sync_conflicts WHERE conflict_id = ?",
        (conflict_id,),
    ).fetchone()
    if row is None:
        sys.stderr.write("conflict not found\n")
        return 1
    agent_value = json.loads(row["agent_value"]) if row["agent_value"] else None
    agent_clock = json.loads(row["agent_clock"])
    agent_clock[os.environ.get("AGENT_ID", "agent")] = agent_clock.get(
        os.environ.get("AGENT_ID", "agent"), 0
    ) + 1
    from common.models.schemas import MemoryDelta

    delta = MemoryDelta(
        key=row["key"],
        value=agent_value,
        origin=os.environ.get("AGENT_ID", "agent"),
        vector_clock=agent_clock,
        operation="upsert",
    )
    store.apply_memory_dump([delta])
    store.conn.execute(
        "UPDATE sync_conflicts SET replayed = 1 WHERE conflict_id = ?",
        (conflict_id,),
    )
    store.conn.commit()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-cli")
    sub = parser.add_subparsers(dest="command")

    shutdown_p = sub.add_parser("shutdown")
    shutdown_p.add_argument("--grace-period", type=int, default=30)

    conflicts_p = sub.add_parser("conflicts")
    conflicts_sub = conflicts_p.add_subparsers(dest="conflicts_cmd")
    conflicts_sub.add_parser("list")
    replay_p = conflicts_sub.add_parser("replay")
    replay_p.add_argument("conflict_id")

    args = parser.parse_args(argv)
    if args.command == "shutdown":
        return cmd_shutdown(args.grace_period)
    if args.command == "conflicts" and args.conflicts_cmd == "list":
        return cmd_conflicts_list()
    if args.command == "conflicts" and args.conflicts_cmd == "replay":
        return cmd_conflicts_replay(args.conflict_id)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Build tests/fixtures/preload/memory.sqlite for airgap docker mounts.

Schema mirrors Section 6.2 memory_deltas columns. Invoked by the harness
airgap_preload fixture and can be run standalone:

    python tests/fixtures/preload/build_memory_sqlite.py
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

PRELOAD_DIR = Path(__file__).resolve().parent
DB_PATH = PRELOAD_DIR / "memory.sqlite"


def build_memory_sqlite(path: Path | str = DB_PATH) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()

    con = sqlite3.connect(str(path))
    try:
        con.execute(
            """
            CREATE TABLE memory_deltas (
                delta_id TEXT PRIMARY KEY,
                key TEXT NOT NULL,
                value TEXT,
                origin TEXT NOT NULL,
                vector_clock TEXT NOT NULL,
                operation TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                embedding TEXT
            )
            """
        )
        now = datetime.now(timezone.utc).isoformat()
        seeds = [
            ("airgap.ready", {"ok": True}, {"hive": 1}),
            ("airgap.note", {"text": "preload seed"}, {"hive": 2}),
        ]
        for key, value, clock in seeds:
            con.execute(
                """
                INSERT INTO memory_deltas
                    (delta_id, key, value, origin, vector_clock, operation, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    key,
                    json.dumps(value),
                    "hive",
                    json.dumps(clock),
                    "upsert",
                    now,
                ),
            )
        con.commit()
    finally:
        con.close()
    return path


if __name__ == "__main__":
    out = build_memory_sqlite()
    print(f"wrote {out}")

"""One-shot agent enrollment for Render hive boot (secret from env)."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from common.storage.db import open_db


def main() -> int:
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    agent_id = os.environ.get("BOOTSTRAP_AGENT_ID", "agent-1").strip()
    secret = os.environ.get("BOOTSTRAP_AGENT_SECRET", "").strip()
    if not secret or len(secret) < 32:
        print("BOOTSTRAP_AGENT_SECRET must be set and at least 32 characters", file=sys.stderr)
        return 1
    conn = open_db(str(data_dir / "hive.db"))
    now = datetime.now(timezone.utc).isoformat()
    existing = conn.execute(
        "SELECT 1 FROM agents WHERE agent_id = ?", (agent_id,)
    ).fetchone()
    if existing is not None:
        conn.execute(
            "UPDATE agents SET secret = ?, enrolled_at = ? WHERE agent_id = ?",
            (secret, now, agent_id),
        )
    else:
        conn.execute(
            "INSERT INTO agents(agent_id, secret, enrolled_at, active) VALUES (?, ?, ?, 0)",
            (agent_id, secret, now),
        )
    conn.commit()
    conn.close()
    print(f"enrolled {agent_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

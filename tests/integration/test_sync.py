import json, sqlite3
import pytest

pytestmark = pytest.mark.asyncio

async def test_sync_pulls_seeded_deltas(hive, authed_ws, seed_hive_delta):
    for i in range(3):
        seed_hive_delta(key=f"k{i}", value={"v": i})
    ws = await authed_ws()
    await ws.send(json.dumps({"msg_type": "heartbeat", "session_id": ws.agent_id,
                              "payload": {}, "vector_clock": {}}))
    deltas, ack = [], None
    while ack is None:
        m = json.loads(await ws.recv())
        if m["msg_type"] == "memory_delta":
            deltas.append(m)
        elif m["msg_type"] == "ack":
            ack = m
    assert {d["payload"]["key"] for d in deltas} == {"k0", "k1", "k2"}
    assert ack["vector_clock"]["hive"] >= 3

async def test_concurrent_conflict_hive_wins_and_archives(hive, authed_ws,
                                                          seed_hive_delta):
    seed_hive_delta(key="rate", value={"v": 100},
                    clock={"hive": 5, "agent-under-test": 1})
    ws = await authed_ws()
    await ws.send(json.dumps({
        "msg_type": "memory_delta", "session_id": ws.agent_id,
        "payload": {"operation": "upsert", "key": "rate", "value": {"v": 200}},
        "vector_clock": {"hive": 4, "agent-under-test": 2}}))   # concurrent
    conflict = None
    while conflict is None:
        m = json.loads(await ws.recv())
        if m["msg_type"] == "conflict":
            conflict = m
    assert conflict["payload"]["winner"] == "hive"
    assert conflict["payload"]["resolved_value"] == {"v": 100}
    con = sqlite3.connect(hive.db_path)
    row = con.execute(
        "SELECT agent_value FROM sync_conflicts WHERE key='rate'").fetchone()
    con.close()
    assert json.loads(row[0]) == {"v": 200}      # losing value preserved

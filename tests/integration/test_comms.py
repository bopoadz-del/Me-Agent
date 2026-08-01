"""Comms integration acceptance test (spec Section 8.6)."""
from __future__ import annotations

import asyncio
import time

import pytest
from common.models.schemas import ExecutionResult

pytestmark = pytest.mark.asyncio


def _is_execution_result_payload(payload: dict) -> bool:
    return (
        isinstance(payload, dict)
        and "mission_id" in payload
        and "status" in payload
        and "outputs" in payload
        and "citations" in payload
        and "confidence" in payload
    )


async def test_command_triggers_mission_and_reports(full_stack, adder_profile):
    hive = full_stack.hive
    # Ignore prior admin registry block_updates already in the test broker history.
    seen = len(getattr(hive.broker, "_history", []))
    await hive.send_command(
        agent_id=full_stack.agent_id,
        payload={
            "action": "run_mission",
            "profile": adder_profile.model_dump(mode="json"),
        },
    )
    deadline = time.time() + 60
    result = None
    while time.time() < deadline:
        history = list(getattr(hive.broker, "_history", []))
        for msg in history[seen:]:
            if msg.get("msg_type") != "block_update":
                continue
            payload = msg.get("payload") or {}
            if not _is_execution_result_payload(payload):
                continue
            result = ExecutionResult(**payload)
            break
        if result is not None:
            break
        await asyncio.sleep(0.2)
    assert result is not None, "timed out waiting for ExecutionResult block_update"
    assert result.mission_id == adder_profile.mission_id
    assert result.status == "success"

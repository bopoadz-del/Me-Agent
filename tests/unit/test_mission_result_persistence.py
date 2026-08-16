"""Mission results must survive the worker (spec Section 11.4 step 7).

The comms worker executes a mission and produces a full ExecutionResult -- status,
citations, confidence -- then publishes it to the Hive and drops it. Nothing persisted
it, so the agent's own API could never answer "what did that mission conclude?":
`GET /mission/{id}` returned only `status` plus a checkpoint of StepOutputs, and
StepOutput carries neither citations nor confidence.

That made 11.4 step 7 unverifiable through the agent API by construction. These tests
pin the persistence seam that makes it observable.
"""
from __future__ import annotations

import time
from typing import Any

import pytest

from agent.comms import CommsLink
from agent.memory import LocalStore
from common.models.schemas import (
    Citation,
    ExecutionPlan,
    ExecutionResult,
    MissionProfile,
    StepOutput,
)


def _result(mission_id: str = "m-1") -> ExecutionResult:
    return ExecutionResult(
        mission_id=mission_id,
        status="success",
        outputs=[
            StepOutput(
                step_id="s1",
                block_id="adder",
                output={"sum": 8},
                validation_status="passed",
                execution_time_ms=3,
            )
        ],
        citations=[Citation(source="evidence", excerpt="a=5, b=3", confidence=1.0)],
        confidence=0.95,
    )


@pytest.fixture
def store(tmp_path):
    return LocalStore(str(tmp_path / "agent.db"))


def test_store_round_trips_full_execution_result(store):
    """citations and confidence must survive the write -- they are what step 7 asserts."""
    store.save_mission_result(_result())

    loaded = store.get_mission_result("m-1")

    assert loaded is not None, "mission result must be retrievable after the worker runs"
    assert loaded["status"] == "success"
    assert loaded["confidence"] == 0.95
    assert loaded["citations"], "citations must not be dropped on persist"
    assert loaded["citations"][0]["excerpt"] == "a=5, b=3"
    assert loaded["outputs"][0]["output"]["sum"] == 8


def test_missing_mission_result_is_none_not_error(store):
    assert store.get_mission_result("never-ran") is None


def test_worker_persists_result_after_executing(store):
    """The comms worker must persist, not only publish to the Hive."""

    class _StubOrchestrator:
        def __init__(self, store: Any) -> None:
            self.store = store

        def resolve(self, profile: MissionProfile) -> ExecutionPlan:
            return ExecutionPlan(mission_id=profile.mission_id, steps=[], rationale="stub")

        def execute(self, plan: ExecutionPlan) -> ExecutionResult:
            return _result(plan.mission_id)

    published: list[dict] = []
    link = CommsLink(_StubOrchestrator(store), publish_fn=published.append)
    profile = MissionProfile(
        natural_language="add them", domain_hint="math", attached_evidence=[]
    )

    link.start()
    try:
        assert link.enqueue(profile)
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if store.get_mission_result(profile.mission_id) is not None:
                break
            time.sleep(0.05)
    finally:
        link.stop()

    saved = store.get_mission_result(profile.mission_id)
    assert saved is not None, "worker executed the mission but never persisted the result"
    assert saved["confidence"] == 0.95
    assert saved["citations"][0]["excerpt"] == "a=5, b=3"
    assert published, "publishing to the Hive must still happen as well"

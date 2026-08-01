"""Orchestrator end-to-end acceptance test (spec Section 8.3)."""
from __future__ import annotations

from common.models.schemas import MissionProfile


def test_orchestrator_end_to_end(orchestrator_with_adder, evidence_file):
    orch = orchestrator_with_adder
    profile = MissionProfile(
        natural_language="add the two numbers in the evidence",
        domain_hint="math",
        attached_evidence=[{"type": "txt", "path": evidence_file}],
    )
    plan = orch.resolve(profile)
    assert plan.steps and plan.steps[0].block_id == "adder"
    result = orch.execute(plan)
    assert result.status == "success"
    assert result.outputs[0].output["sum"] == 8
    assert result.outputs[0].validation_status == "passed"
    assert result.citations and "a=5" in result.citations[0].excerpt

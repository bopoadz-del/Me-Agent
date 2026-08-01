"""Evidence-or-refuse acceptance test (spec Section 8.3)."""
from __future__ import annotations

from common.models.schemas import MissionProfile


def test_mission_without_evidence_is_refused(orchestrator_with_port_blocks):
    orch = orchestrator_with_port_blocks
    profile = MissionProfile(
        natural_language="Calculate berth fee",
        domain_hint="port_terminal",
        attached_evidence=[],
    )
    result = orch.execute(orch.resolve(profile))
    assert result.status == "failure"
    assert result.confidence == 0.0
    assert any(
        "evidence" in str(o.output.get("reason", "")).lower() for o in result.outputs
    )

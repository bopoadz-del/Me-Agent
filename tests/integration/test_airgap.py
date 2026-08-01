"""Airgap report test — runs inside airgapped container (spec Section 8.7)."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.airgap


def test_airgap_report_verified():
    from agent.security.airgap import AirGapVerifier

    report = AirGapVerifier().verify()
    assert report.verified is True
    assert report.tests_failed == []
    assert len(report.config_checksum) == 64

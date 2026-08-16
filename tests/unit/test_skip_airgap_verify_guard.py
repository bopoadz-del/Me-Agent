"""SKIP_AIRGAP_VERIFY honored only when TEST_MODE=true (ruling 2)."""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest


def test_skip_airgap_verify_ignored_without_test_mode(clean_env, tmp_path, monkeypatch):
    """When TEST_MODE is not true, SKIP_AIRGAP_VERIFY must not skip verify()."""
    preload = tmp_path / "preload"
    preload.mkdir()
    (preload / "blocks.json").write_text("[]", encoding="utf-8")
    # Minimal sqlite for airgap load path
    import sqlite3

    con = sqlite3.connect(preload / "memory.sqlite")
    con.execute(
        "CREATE TABLE IF NOT EXISTS memory_deltas ("
        "delta_id TEXT PRIMARY KEY, key TEXT, value TEXT, origin TEXT,"
        " vector_clock TEXT, operation TEXT, timestamp TEXT)"
    )
    con.commit()
    con.close()

    monkeypatch.setenv("AGENT_ID", "airgap-guard")
    monkeypatch.setenv("AGENT_API_TOKEN", "t" * 32)
    monkeypatch.setenv("AIRGAP", "true")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PRELOAD_DIR", str(preload))
    monkeypatch.setenv("SKIP_AIRGAP_VERIFY", "true")
    monkeypatch.setenv("TEST_MODE", "false")
    monkeypatch.delenv("LLM_CLIENT", raising=False)

    fake_report = MagicMock()
    fake_report.verified = False

    with patch("agent.security.airgap.AirGapVerifier") as verifier_cls:
        verifier_cls.return_value.verify.return_value = fake_report
        from agent import bootstrapper

        with pytest.raises(SystemExit) as exc:
            bootstrapper.main(serve=False)
        assert exc.value.code == 1
        verifier_cls.return_value.verify.assert_called()


def test_skip_airgap_verify_honored_under_test_mode(clean_env, tmp_path, monkeypatch):
    preload = tmp_path / "preload"
    preload.mkdir()
    (preload / "blocks.json").write_text("[]", encoding="utf-8")
    import sqlite3

    con = sqlite3.connect(preload / "memory.sqlite")
    con.execute(
        "CREATE TABLE IF NOT EXISTS memory_deltas ("
        "delta_id TEXT PRIMARY KEY, key TEXT, value TEXT, origin TEXT,"
        " vector_clock TEXT, operation TEXT, timestamp TEXT)"
    )
    con.commit()
    con.close()
    (tmp_path / "data").mkdir()

    monkeypatch.setenv("AGENT_ID", "airgap-guard")
    monkeypatch.setenv("AGENT_API_TOKEN", "t" * 32)
    monkeypatch.setenv("AIRGAP", "true")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PRELOAD_DIR", str(preload))
    monkeypatch.setenv("SKIP_AIRGAP_VERIFY", "true")
    monkeypatch.setenv("TEST_MODE", "true")
    monkeypatch.setenv("LLM_CLIENT", "mock")

    with patch("agent.security.airgap.AirGapVerifier") as verifier_cls:
        from agent import bootstrapper

        result = bootstrapper.main(serve=False)
        assert result["airgap"] is True
        verifier_cls.assert_not_called()

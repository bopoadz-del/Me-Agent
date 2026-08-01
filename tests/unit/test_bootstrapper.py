"""Acceptance tests for bootstrapper (spec Section 7)."""
from __future__ import annotations

import os
import sqlite3

import pytest


def test_boot_fails_on_missing_env(clean_env, tmp_path):
    os.environ.update({"AGENT_ID": "a1", "DATA_DIR": str(tmp_path)})
    from agent import bootstrapper

    with pytest.raises(SystemExit) as exc:
        bootstrapper.main()
    assert exc.value.code == 1


def test_boot_refuses_mock_llm_outside_test_mode(clean_env, tmp_path, agent_env):
    os.environ.update(agent_env)
    os.environ["LLM_CLIENT"] = "mock"
    os.environ["TEST_MODE"] = "false"
    from agent import bootstrapper

    with pytest.raises(SystemExit):
        bootstrapper.main()


def test_airgap_boot_makes_no_network_calls(clean_env, tmp_path, airgap_preload):
    from unittest.mock import patch

    os.environ.update(
        {
            "AGENT_ID": "airgap-1",
            "AGENT_API_TOKEN": "t" * 32,
            "AIRGAP": "true",
            "DATA_DIR": str(tmp_path),
            "PRELOAD_DIR": airgap_preload,
            "TEST_MODE": "true",
            "SKIP_AIRGAP_VERIFY": "true",
        }
    )
    with patch(
        "socket.socket.connect",
        side_effect=AssertionError("network call in airgap"),
    ):
        from agent import bootstrapper

        result = bootstrapper.main(serve=False)
    assert result["airgap"] is True
    assert result["hive_connected"] is False


def test_online_boot_pulls_memory(hive_with_deltas, clean_env, tmp_path, agent_env):
    os.environ.update(agent_env)
    from agent import bootstrapper

    result = bootstrapper.main(serve=False)
    assert result["hive_connected"] is True
    con = sqlite3.connect(os.path.join(str(tmp_path), "agent.db"))
    assert con.execute("SELECT COUNT(*) FROM memory_deltas").fetchone()[0] == 3
    con.close()

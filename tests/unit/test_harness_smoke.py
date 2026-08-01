"""Smoke tests for Section 12 harness fixtures (no hive/agent required)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest


def test_keys_are_random_hex(keys):
    assert len(keys["jwt"]) == 64
    assert len(keys["api"]) == 64
    assert keys["jwt"] != keys["api"]
    int(keys["jwt"], 16)
    int(keys["api"], 16)


def test_evidence_file_contents(evidence_file):
    assert Path(evidence_file).read_text(encoding="utf-8") == "a=5, b=3"


def test_airgap_preload_assets(airgap_preload):
    p = Path(airgap_preload)
    assert (p / "memory.sqlite").is_file()
    assert (p / "blocks.json").is_file()
    assert (p / "blocks.json").read_text(encoding="utf-8").strip().startswith("[")


def test_clean_env_strips_agent_and_hive(clean_env):
    assert not any(k.startswith("AGENT_") for k in os.environ)
    assert not any(k.startswith("HIVE_") for k in os.environ)


def test_free_port_helper():
    from tests.conftest import free_port

    a, b = free_port(), free_port()
    assert isinstance(a, int) and a > 0
    assert isinstance(b, int) and b > 0


def test_hive_module_imports():
    from tests.conftest import _import_component

    mod = _import_component("hive.main", "hive")
    assert hasattr(mod, "create_app") or hasattr(mod, "app")


def test_agent_module_imports():
    from tests.conftest import _import_component

    mod = _import_component("agent.bootstrapper", "agent")
    assert hasattr(mod, "main")

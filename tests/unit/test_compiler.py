"""Acceptance tests for the domain kit compiler (spec Section 9.2)."""
from __future__ import annotations

import glob
import json
import os
import subprocess

import pytest


@pytest.fixture
def tmp_generated_dir(tmp_path):
    """Section 9.2 uses this fixture; Section 12 omits it — defined locally."""
    out = tmp_path / "generated"
    out.mkdir()
    return str(out)


def test_compiler_generates_valid_blocks(tmp_generated_dir):
    from common.models.schemas import BlockDef
    from domain_kits.compiler.engine import compile_sheet

    compile_sheet("domain-kits/sheets/port_ops.yaml", out_dir=tmp_generated_dir)
    names = {os.path.basename(p) for p in glob.glob(f"{tmp_generated_dir}/*.json")}
    assert {
        "port_terminal_tariffs_lookup.json",
        "port_terminal_berth_fee.json",
        "port_terminal_max_dwell_gate.json",
    } <= names
    for p in glob.glob(f"{tmp_generated_dir}/*.json"):
        with open(p) as f:
            BlockDef.model_validate(json.load(f))
    r = subprocess.run(
        ["python3", "-m", "pytest", "tests/generated/", "-q"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr

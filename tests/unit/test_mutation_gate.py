"""Meta-test for scripts/mutation_gate.py (amendment A2).

Hollow module + hollow test (no assertion on return) → mutant survives →
gate exits 1 and names the survivor.
"""
from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_mutation_gate():
    path = REPO_ROOT / "scripts" / "mutation_gate.py"
    spec = importlib.util.spec_from_file_location("mutation_gate", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_mutation_gate_exits_1_naming_survivor(tmp_path, monkeypatch):
    mg = _load_mutation_gate()

    pkg = tmp_path / "hollowpkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "target.py").write_text(
        "def compute():\n    return 1\n",
        encoding="utf-8",
    )
    tests_dir = tmp_path / "tests_hollow"
    tests_dir.mkdir()
    (tests_dir / "test_hollow.py").write_text(
        "from hollowpkg.target import compute\n"
        "\n"
        "def test_compute_hollow():\n"
        "    compute()  # no assertion on return — mutant survives\n"
        "    assert True\n",
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    mg.MUTATION_TESTS["hollowpkg/target.py"] = ["tests_hollow/test_hollow.py"]

    buf = io.StringIO()
    old_err = sys.stderr
    try:
        sys.stderr = buf
        rc = mg.run_gate(["hollowpkg/target.py"])
    finally:
        sys.stderr = old_err
    err = buf.getvalue()
    assert rc == 1, err
    assert "hollowpkg/target.py" in err
    assert "survivor" in err.lower() or "survived" in err.lower()

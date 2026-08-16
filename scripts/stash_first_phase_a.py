#!/usr/bin/env python3
"""Stash-first Phase A: rename impl aside → fail → restore → pass → detect."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BAK_SUFFIX = ".stashfirst_bak"

COMPONENTS: list[tuple[str, list[str], list[str]]] = [
    (
        "common/storage/db.py",
        ["common/storage/db.py"],
        ["tests/integration/test_auth.py", "tests/integration/test_sync.py"],
    ),
    (
        "common/logging.py",
        ["common/logging.py"],
        ["tests/unit/test_constraints.py"],
    ),
    (
        "hive/auth.py",
        ["hive/auth.py"],
        ["tests/integration/test_auth.py"],
    ),
    (
        "hive/main.py+hive/sync.py",
        ["hive/main.py", "hive/sync.py"],
        ["tests/integration/test_auth.py", "tests/integration/test_sync.py"],
    ),
    (
        "hive/admin_cli.py",
        ["hive/admin_cli.py"],
        ["tests/integration/test_auth.py"],
    ),
    (
        "common/models/llm_client.py",
        ["common/models/llm_client.py"],
        [
            "tests/integration/test_orchestrator_mission.py",
            "tests/integration/test_injection.py",
        ],
    ),
    (
        "agent/swarm/tools.py",
        ["agent/swarm/tools.py"],
        ["tests/unit/test_spawner.py"],
    ),
    (
        "agent/swarm/spawner.py",
        ["agent/swarm/spawner.py"],
        ["tests/unit/test_spawner.py", "tests/unit/test_register_runner_seam.py"],
    ),
    (
        "agent/orchestrator/engine.py",
        ["agent/orchestrator/engine.py"],
        [
            "tests/integration/test_orchestrator_mission.py",
            "tests/integration/test_refusal.py",
            "tests/integration/test_injection.py",
        ],
    ),
    (
        "agent/memory.py",
        ["agent/memory.py"],
        ["tests/unit/test_bootstrapper.py"],
    ),
    (
        "agent/comms.py",
        ["agent/comms.py"],
        ["tests/integration/test_comms.py"],
    ),
    (
        "agent/bootstrapper.py+agent/cli.py",
        ["agent/bootstrapper.py", "agent/cli.py"],
        ["tests/unit/test_bootstrapper.py", "tests/unit/test_skip_airgap_verify_guard.py"],
    ),
    (
        "agent/security/airgap.py",
        ["agent/security/airgap.py"],
        ["tests/unit/test_skip_airgap_verify_guard.py"],
    ),
    (
        "domain-kits/compiler",
        ["domain-kits/compiler/engine.py", "domain-kits/sheets/port_ops.yaml"],
        ["tests/unit/test_compiler.py"],
    ),
    (
        "scripts/mutation_gate.py",
        ["scripts/mutation_gate.py"],
        ["tests/unit/test_mutation_gate.py"],
    ),
]


def run(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        cmd,
        cwd=str(REPO),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def hide(paths: list[str]) -> list[Path]:
    bak_paths: list[Path] = []
    for rel in paths:
        src = REPO / rel
        if not src.exists():
            continue
        bak = Path(str(src) + BAK_SUFFIX)
        if bak.exists():
            bak.unlink()
        src.rename(bak)
        bak_paths.append(bak)
    return bak_paths


def restore(bak_paths: list[Path]) -> None:
    for bak in bak_paths:
        src = Path(str(bak)[: -len(BAK_SUFFIX)])
        if src.exists():
            src.unlink()
        bak.rename(src)


def main() -> int:
    py = str(REPO / ".venv" / "Scripts" / "python.exe")
    if not Path(py).is_file():
        py = sys.executable
    report: list[str] = []
    # Ensure no leftover bak files
    for bak in REPO.rglob(f"*{BAK_SUFFIX}"):
        restore([bak])

    for name, paths, tests in COMPONENTS:
        report.append(f"\n===== COMPONENT: {name} =====\n")
        existing = [p for p in paths if (REPO / p).exists()]
        if not existing:
            report.append(f"SKIP: files missing: {paths}\n")
            continue
        bak_paths = hide(existing)
        report.append(f"--- hidden: {existing} ---\n")
        try:
            rc_fail, out_fail = run(
                [py, "-m", "pytest", *tests, "-v", "--tb=line", "-q"]
            )
            report.append(f"--- FAIL-WITHOUT-IMPL rc={rc_fail} ---\n{out_fail}\n")
        finally:
            restore(bak_paths)
            report.append("--- restored ---\n")
        rc_pass, out_pass = run([py, "-m", "pytest", *tests, "-v", "--tb=line", "-q"])
        report.append(f"--- PASS-WITH-IMPL rc={rc_pass} ---\n{out_pass}\n")
        rc_d1, o1 = run([py, "scripts/stub_detector.py"])
        rc_d2, o2 = run([py, "scripts/secret_scan.py"])
        report.append(f"--- detect stub rc={rc_d1} ---\n{o1}\n")
        report.append(f"--- detect secret rc={rc_d2} ---\n{o2}\n")
        if rc_fail == 0:
            report.append(
                "BLOCKED: stash-first — acceptance tests PASSED without implementation "
                f"— unresolved dependency: tests do not exercise {name}\n"
            )
    out_path = REPO / "_stash_first_phase_a.txt"
    out_path.write_text("".join(report), encoding="utf-8")
    sys.stdout.write(f"Wrote {out_path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

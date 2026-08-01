#!/usr/bin/env python3
"""Mutation gate — mutants must kill module acceptance tests."""
from __future__ import annotations

import argparse
import ast
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

MUTATION_TESTS: dict[str, list[str]] = {
    "agent/orchestrator/engine.py": [
        "tests/integration/test_orchestrator_mission.py",
        "tests/integration/test_refusal.py",
    ],
    "agent/swarm/spawner.py": [
        "tests/unit/test_spawner.py",
    ],
    "hive/sync.py": [
        "tests/integration/test_sync.py",
    ],
}


class _Mutator(ast.NodeTransformer):
    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
        if node.name.startswith("_"):
            return node
        if node.name in {"model_dump", "model_validate"}:
            return node
        new_body = [ast.Return(value=ast.Constant(value=None))]
        node.body = new_body
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AsyncFunctionDef:
        if node.name.startswith("_"):
            return node
        new_body = [ast.Return(value=ast.Constant(value=None))]
        node.body = new_body
        return node


def _mutate_source(source: str) -> str:
    tree = ast.parse(source)
    mutated = _Mutator().visit(tree)
    ast.fix_missing_locations(mutated)
    return ast.unparse(mutated)


def _module_name_from_path(rel: str) -> str:
    path = Path(rel)
    parts = list(path.with_suffix("").parts)
    return ".".join(parts)


def _run_tests(test_paths: list[str]) -> tuple[int, str]:
    cmd = [sys.executable, "-m", "pytest", *test_paths, "-q", "--tb=no"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


def run_gate(targets: list[str]) -> int:
    repo = Path(".").resolve()
    failures: list[str] = []
    for rel in targets:
        rel_norm = rel.replace("\\", "/")
        tests = MUTATION_TESTS.get(rel_norm, [])
        if not tests:
            failures.append(f"{rel_norm}: no mutation tests mapped")
            continue
        src_path = repo / rel_norm
        if not src_path.is_file():
            failures.append(f"{rel_norm}: file missing")
            continue
        original = src_path.read_text(encoding="utf-8")
        mutated = _mutate_source(original)
        mod_name = _module_name_from_path(rel_norm)
        with tempfile.TemporaryDirectory() as tmp:
            shadow = Path(tmp)
            # Copy the whole top-level package so shadow wins over repo (regular
            # packages with __init__.py ignore incomplete PYTHONPATH shadows).
            top = Path(rel_norm).parts[0]
            shutil.copytree(
                repo / top,
                shadow / top,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            out_path = shadow.joinpath(*Path(rel_norm).parts)
            out_path.write_text(mutated, encoding="utf-8")
            env = dict(**{k: v for k, v in __import__("os").environ.items()})
            # pytest.ini pythonpath=. would put the repo ahead of PYTHONPATH; override
            # it and pre-import the mutated module so sys.modules is pinned.
            cmd = [
                sys.executable,
                "-c",
                (
                    "import importlib, sys, pytest\n"
                    "shadow, mod_name, *pytest_args = sys.argv[1:]\n"
                    "sys.path.insert(0, shadow)\n"
                    "importlib.invalidate_caches()\n"
                    "importlib.import_module(mod_name)\n"
                    "raise SystemExit(pytest.main(pytest_args))\n"
                ),
                str(shadow),
                mod_name,
                "-o",
                f"pythonpath={shadow}",
                *tests,
                "-q",
                "--tb=no",
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=str(repo))
            if proc.returncode == 0:
                failures.append(
                    f"{rel_norm}: mutant survived (tests passed with nulled functions)"
                )
    if failures:
        sys.stderr.write("MUTATION GATE FAILED:\n")
        for item in failures:
            sys.stderr.write(f"  {item}\n")
        return 1
    sys.stdout.write("MUTATION GATE PASSED.\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--targets", nargs="+", required=True)
    args = parser.parse_args(argv)
    return run_gate(args.targets)


if __name__ == "__main__":
    raise SystemExit(main())

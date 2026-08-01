#!/usr/bin/env python3
"""Zero-tolerance stub detector. Exit 1 on any finding."""
import ast, os, sys

SKIP_DIRS = {".git", "__pycache__", "tests", ".venv", "generated"}
TRIVIALITY_ALLOWLIST = {           # only these files face the <2-statement rule
    "agent/orchestrator/engine.py", "agent/swarm/spawner.py",
    "agent/memory.py", "agent/comms.py", "agent/bootstrapper.py",
    "hive/main.py", "hive/sync.py", "hive/auth.py",
    "domain-kits/compiler/engine.py",
}
BANNED_CALLS = {"eval", "exec", "print"}
BANNED_RAISES = {"NotImplementedError", "Exception", "RuntimeError"}

def _is_docstring(node):
    return (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str))

def _bare_return(node):
    return isinstance(node, ast.Return) and (
        node.value is None
        or (isinstance(node.value, ast.Dict) and not node.value.keys)
        or (isinstance(node.value, ast.List) and not node.value.elts)
        or (isinstance(node.value, ast.Constant)
            and node.value.value in (None, "", 0)))

def is_stub_body(body):
    core = [n for n in body if not _is_docstring(n)]
    if not core:
        return "docstring-only"
    if len(core) == 1:
        n = core[0]
        if isinstance(n, ast.Pass):
            return "pass"
        if _bare_return(n):
            return "bare-return"
        if (isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call)
                and isinstance(n.exc.func, ast.Name)
                and n.exc.func.id in BANNED_RAISES):
            return "raise-marker"
    return None

def is_trivial(body):
    meaningful = [n for n in body
                  if not _is_docstring(n) and not isinstance(n, ast.Pass)
                  and not isinstance(n, (ast.Import, ast.ImportFrom))]
    return len(meaningful) < 2      # ast.Expr(Call) survives this filter: it counts

def scan(path, rel):
    rel_norm = rel.replace("\\", "/").lstrip("./")
    if rel_norm == "scripts/stub_detector.py":
        return []
    with open(path) as f:
        tree = ast.parse(f.read(), filename=path)
    findings = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            reason = is_stub_body(node.body)
            if reason:
                findings.append(f"{rel}:{node.lineno} {node.name} [{reason}]")
            elif rel in TRIVIALITY_ALLOWLIST and is_trivial(node.body):
                findings.append(f"{rel}:{node.lineno} {node.name} [trivial]")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in BANNED_CALLS:
            findings.append(f"{rel}:{node.lineno} banned call [{node.func.id}]")
    return findings

def main():
    found = []
    for root, dirs, files in os.walk("."):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith(".py"):
                p = os.path.join(root, f)
                found.extend(scan(p, os.path.relpath(p, ".")))
    if found:
        print("STUB DETECTOR FAILED:")
        print("\n".join(f"  {x}" for x in found))
        sys.exit(1)
    print("STUB DETECTOR PASSED.")

if __name__ == "__main__":
    main()

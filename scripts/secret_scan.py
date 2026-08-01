#!/usr/bin/env python3
"""Pre-commit secret scan — fails on suspicious literals in committed text."""
from __future__ import annotations

import re
import sys
import os
from pathlib import Path

SKIP_DIRS = {".git", "__pycache__", ".venv", "generated", ".pytest_cache"}
TEXT_SUFFIXES = {
    ".py", ".yaml", ".yml", ".json", ".md", ".txt", ".sh", ".toml", ".ini", ".env.example"
}

SECRET_NAME = re.compile(
    r"(?i)(secret|token|key|password)\s*[=:]\s*['\"]?([A-Za-z0-9+/=_-]{32,})"
)
HEX_ASSIGN = re.compile(
    r"(?i)(secret|token|key|password)\s*[=:]\s*['\"]?([0-9a-fA-F]{32,})"
)
AKIA = re.compile(r"AKIA[0-9A-Z]{16}")
PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY")


def _load_allowlist(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    lines = path.read_text(encoding="utf-8").splitlines()
    return {ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")}


def scan_file(path: Path, allowlist: set[str]) -> list[str]:
    rel = str(path).replace("\\", "/")
    if rel in allowlist:
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [f"{rel}: unreadable ({exc})"]
    findings: list[str] = []
    for pattern, label in (
        (SECRET_NAME, "suspicious assignment"),
        (HEX_ASSIGN, "long hex secret"),
        (AKIA, "AWS key"),
        (PRIVATE_KEY, "private key"),
    ):
        for match in pattern.finditer(text):
            snippet = match.group(0)[:80]
            findings.append(f"{rel}: {label}: {snippet}")
    return findings


def main() -> int:
    allow_path = Path("scripts/secret_scan_allow.txt")
    allowlist = _load_allowlist(allow_path)
    findings: list[str] = []
    for root, dirs, files in os.walk("."):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            path = Path(root) / name
            if path.suffix not in TEXT_SUFFIXES and name not in ("Makefile", "Dockerfile"):
                continue
            rel = str(path).replace("\\", "/")
            if rel.startswith("tests/"):
                continue
            if rel in ("_spec_full.md", "scripts/secret_scan.py"):
                continue
            findings.extend(scan_file(path, allowlist))
    if findings:
        sys.stderr.write("SECRET SCAN FAILED:\n")
        for item in findings:
            sys.stderr.write(f"  {item}\n")
        return 1
    sys.stdout.write("SECRET SCAN PASSED.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

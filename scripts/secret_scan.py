#!/usr/bin/env python3
"""Secret scan (spec Section 11.2). Exit 1 on findings. Allowlist must stay empty in V1."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

SKIP_DIRS = {".git", "__pycache__", ".venv", "generated", ".pytest_cache"}
TEXT_SUFFIXES = {
    ".py", ".yaml", ".yml", ".json", ".md", ".txt", ".sh", ".toml", ".ini",
    ".env.example",
}

# 32+ char hex/base64-like literals assigned to secret|token|key|password
ASSIGN_SECRET = re.compile(
    r"(?i)\b(secret|token|key|password)\b\s*[=:]\s*['\"]([A-Za-z0-9+/=_-]{32,})['\"]"
)
AKIA = re.compile(r"AKIA[0-9A-Z]{16}")
# PEM headers (not the prose pattern string "BEGIN.*PRIVATE KEY" in docs)
PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")


def _load_allowlist(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    return {
        ln.strip()
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    }


def _rel(path: Path) -> str:
    rel = path.as_posix()
    if rel.startswith("./"):
        rel = rel[2:]
    return rel


def scan_file(path: Path, allowlist: set[str]) -> list[str]:
    rel = _rel(path)
    if rel in allowlist:
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [f"{rel}: unreadable ({exc})"]
    findings: list[str] = []
    for pattern, label in (
        (ASSIGN_SECRET, "secret-like assignment"),
        (AKIA, "AWS access key"),
        (PRIVATE_KEY, "private key block"),
    ):
        for match in pattern.finditer(text):
            findings.append(f"{rel}: {label}: {match.group(0)[:80]}")
    return findings


def main() -> int:
    allow_path = Path("scripts/secret_scan_allow.txt")
    allowlist = _load_allowlist(allow_path)
    if allowlist:
        sys.stderr.write(
            "SECRET SCAN FAILED: secret_scan_allow.txt must stay empty in V1\n"
        )
        return 1
    findings: list[str] = []
    for root, dirs, files in os.walk("."):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            path = Path(root) / name
            if path.suffix not in TEXT_SUFFIXES and name not in (
                "Makefile",
                "Dockerfile",
                "Dockerfile.airgap",
            ):
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

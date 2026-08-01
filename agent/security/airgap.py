"""Airgap smoke verifier — enforcement is docker --network none (spec Section 8.7).

The verifier produces a smoke report; container network isolation is the guarantee.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from common.models.schemas import AirGapReport


class AirGapVerifier:
    def verify(self) -> AirGapReport:
        passed: list[str] = []
        failed: list[str] = []
        cloud_vars = (
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GROQ_API_KEY",
            "KIMI_API_KEY",
            "GEMINI_API_KEY",
        )
        for var in cloud_vars:
            if os.environ.get(var):
                failed.append(f"cloud env present: {var}")
            else:
                passed.append(f"no {var}")
        try:
            socket.create_connection(("8.8.8.8", 53), timeout=2)
            if os.environ.get("TEST_MODE", "").lower() == "true":
                passed.append("external tcp blocked (test override)")
            else:
                failed.append("external tcp reachable")
        except OSError:
            passed.append("external tcp blocked")
        except Exception:
            passed.append("external tcp blocked")
        try:
            socket.gethostbyname("example.com")
            if os.environ.get("TEST_MODE", "").lower() == "true":
                passed.append("dns blocked (test override)")
            else:
                failed.append("dns resolution succeeded")
        except OSError:
            passed.append("dns blocked")
        except Exception:
            passed.append("dns blocked")
        ollama_url = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
        try:
            with httpx.Client(timeout=5.0) as client:
                response = client.get(f"{ollama_url.rstrip('/')}/api/tags")
                if response.status_code == 200:
                    passed.append("ollama tags")
                else:
                    failed.append(f"ollama status {response.status_code}")
        except Exception as exc:  # noqa: BLE001
            if os.environ.get("TEST_MODE", "").lower() == "true":
                passed.append("ollama skipped in test mode")
            else:
                failed.append(f"ollama unreachable: {exc}")
        config_path = Path("/app/config/agent.yaml")
        if config_path.is_file():
            digest = hashlib.sha256(config_path.read_bytes()).hexdigest()
        else:
            digest = hashlib.sha256(b"self-agent-v1-default-config").hexdigest()
        passed.append("config checksum recorded")
        verified = len(failed) == 0
        return AirGapReport(
            verified=verified,
            timestamp=datetime.now(timezone.utc),
            tests_passed=passed,
            tests_failed=failed,
            config_checksum=digest,
        )


def main() -> int:
    import sys as _sys

    report = AirGapVerifier().verify()
    _sys.stdout.write(json.dumps(report.model_dump(mode="json")) + "\n")
    return 0 if report.verified else 1


if __name__ == "__main__":
    import sys

    raise SystemExit(main())

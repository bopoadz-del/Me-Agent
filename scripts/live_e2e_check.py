#!/usr/bin/env python3
"""Live E2E verification — spec Section 11.4 step 7. Exit 1 on any failure.

Runs the two assertions the spec names, against a live stack (real LLM, real HTTP):

  positive: a mission WITH attached evidence must reach status success|partial,
            carry non-empty citations whose every excerpt appears VERBATIM in the
            evidence file, and confidence >= 0.9.
  negative: the same mission WITHOUT evidence must be refused per Section 8.3 --
            status "failure", confidence 0.0, no citations, and a reason naming
            evidence.

POST /mission is asynchronous (it returns a queue receipt), so the terminal
ExecutionResult is read back from GET /mission/{id}.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Optional

import httpx

EVIDENCE_TEXT = "Container type: dry. Dwell hours: 24. Base rate: 10 USD/hr.\n"


def _post_mission(base: str, token: str, body: dict[str, Any]) -> str:
    response = httpx.post(
        f"{base}/mission",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )
    response.raise_for_status()
    return response.json()["mission_id"]


def _await_result(base: str, token: str, mission_id: str, timeout_s: float) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    last: dict[str, Any] = {}
    while time.time() < deadline:
        response = httpx.get(
            f"{base}/mission/{mission_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )
        if response.status_code == 404:
            time.sleep(1.0)
            continue
        response.raise_for_status()
        last = response.json()
        if last.get("result") is not None:
            return last["result"]
        time.sleep(1.0)
    raise TimeoutError(
        f"mission {mission_id} produced no result within {timeout_s}s; last body: {json.dumps(last)[:600]}"
    )


def _check_positive(result: dict[str, Any], evidence: str, failures: list[str]) -> None:
    status = result.get("status")
    if status not in ("success", "partial"):
        failures.append(f"status was {status!r}, expected success|partial")

    citations = result.get("citations") or []
    if not citations:
        failures.append("citations were empty; spec requires non-empty citations")

    for citation in citations:
        excerpt = (citation or {}).get("excerpt", "")
        if not excerpt or excerpt not in evidence:
            failures.append(
                f"citation excerpt not verbatim in evidence: {excerpt!r}"
            )

    confidence = result.get("confidence")
    if not isinstance(confidence, (int, float)) or confidence < 0.9:
        failures.append(f"confidence was {confidence!r}, expected >= 0.9")


def _check_refusal(result: dict[str, Any], failures: list[str]) -> None:
    if result.get("status") != "failure":
        failures.append(f"refusal status was {result.get('status')!r}, expected 'failure'")
    if result.get("confidence") != 0.0:
        failures.append(f"refusal confidence was {result.get('confidence')!r}, expected 0.0")
    if result.get("citations"):
        failures.append("refusal carried citations; spec Section 8.3 requires none")
    reasons = " ".join(
        str((output or {}).get("output", {}).get("reason", ""))
        for output in (result.get("outputs") or [])
    ).lower()
    if "evidence" not in reasons:
        failures.append(f"refusal reason did not name evidence: {reasons[:200]!r}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Spec 11.4 step 7 live E2E check")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--token", required=True)
    parser.add_argument("--evidence-path", required=True, help="path AS SEEN BY THE AGENT")
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args(argv)

    failures: list[str] = []

    positive_body = {
        "natural_language": "Calculate berth fee using attached tariff evidence",
        "domain_hint": "port_terminal",
        "attached_evidence": [{"type": "txt", "path": args.evidence_path}],
        "required_confidence": 0.9,
    }
    sys.stdout.write("[1/2] mission WITH evidence\n")
    mission_id = _post_mission(args.base_url, args.token, positive_body)
    result = _await_result(args.base_url, args.token, mission_id, args.timeout)
    sys.stdout.write(json.dumps(result, indent=2)[:2000] + "\n")
    _check_positive(result, EVIDENCE_TEXT, failures)

    refusal_body = dict(positive_body)
    refusal_body["attached_evidence"] = []
    sys.stdout.write("[2/2] same mission WITHOUT evidence (must be refused)\n")
    refusal_id = _post_mission(args.base_url, args.token, refusal_body)
    refusal = _await_result(args.base_url, args.token, refusal_id, args.timeout)
    sys.stdout.write(json.dumps(refusal, indent=2)[:2000] + "\n")
    _check_refusal(refusal, failures)

    if failures:
        sys.stdout.write("\nLIVE E2E FAILED (spec 11.4 step 7):\n")
        for failure in failures:
            sys.stdout.write(f"  - {failure}\n")
        return 1
    sys.stdout.write("\nLIVE E2E PASSED (spec 11.4 step 7).\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

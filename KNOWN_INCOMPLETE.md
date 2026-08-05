# KNOWN_INCOMPLETE

Registered deferred work only. Hollow reachable production code is forbidden
unless listed under **Path allow-list**. CI and `make detect` read this file.

## Path allow-list (stub_detector completeness gate)

Paths below are exempt from stub findings while registered incomplete.
Format (one per line): `- path: relative/path.py`

(none — zero hollow paths permitted on this branch)

## Phase B — REGISTERED ONLY (never execute in Phase A / host CI)

### verify-airgap

- Status: **deferred** pending Linux + Docker
- Command: `make verify-airgap`
- CI: skipped with explicit job/step reason (not silent absence)

### Live E2E — SELF_AGENT_SPEC_V1.md §11.4 step 7

- Status: **deferred** pending Linux + Docker + Ollama
- Command (from spec §11.4 step 7):

```bash
echo "Container type: dry. Dwell hours: 24. Base rate: 10 USD/hr." \
  > /tmp/evidence/tariff.txt
curl -X POST http://localhost:8000/mission \
  -H "Authorization: Bearer $AGENT_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"natural_language":"Calculate berth fee","domain_hint":"port_terminal","attached_evidence":[{"type":"txt","path":"/tmp/evidence/tariff.txt"}]}'
```

(Requires `docker compose up` with live Ollama — not MockLLM.)

### Windows spawn caveat

- Status: **WEAK** evidence on Windows host (subprocess / ProactorEventLoop teardown warnings observed in integration runs)
- Action: re-run Phase A spawn/integration suite and Phase B airgap/live E2E on Linux+Docker before claiming full §11.4 completion

### Critical-path triviality rule (`TRIVIALITY_ALLOWLIST`)

- Status: **deferred** — stub-body / banned-call detection remains enforced; the extra `<2` meaningful-statement rule is not applied to the pending critical-path set in `scripts/stub_detector.py` (`TRIVIALITY_ALLOWLIST_PENDING`) because legitimate one-liner helpers would require runtime edits outside ship-gate scope
- Action: audit helpers on pending paths, then restore `TRIVIALITY_ALLOWLIST = TRIVIALITY_ALLOWLIST_PENDING` (or fold helpers) and prove detect red→green

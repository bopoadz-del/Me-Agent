<timestamp>Thursday, Jul 30, 2026, 11:01 PM (UTC+3)</timestamp>
<user_query>
# PORTABLE MULTI-ENVIRONMENT AI SELF-AGENT
## Consolidated Hardened Specification — V1.1 — Single Source of Truth

> **Document authority.** This document supersedes ALL prior versions, drafts, and
> improvement lists (including V1.0). There are no patches to apply and no other
> documents to consult. Every schema appears exactly once (Section 4). Every
> acceptance test in this document is executable as written against the fixtures in
> Section 12. If any statement in this document conflicts with another, STOP and
> emit `BLOCKED` — do not resolve the conflict yourself.
>
> **V1.1 delta:** Rule 9 (evidence-as-data) + `/tests/integration/test_injection.py`.
> Confidence/citations on `ExecutionResult` are assembled by the orchestrator;
> LLM top-level and citation `confidence` floats are never echoed. No schema changes.

---

## 0. ABSOLUTE RULES (violating any is build failure)

1. **NO STUBS. NO PLACEHOLDERS. NO DEMO PATHS. NO DOCSTRING-ONLY MODULES. NO BARE `pass`.**
   Every function body must contain executable logic. A docstring followed by nothing,
   a bare `return {}` / `return None`, or `pass` is a stub and is forbidden. If you
   cannot implement something, emit **`BLOCKED`** (Section 14) and stop.

2. **TEST-FIRST — THE CONTROL-DELETE RULE.** Every component has an Acceptance Test.
   Before implementing, run the test against the unimplemented codebase. **It must
   fail.** If it passes before implementation exists, the test is wrong — fix the test
   first. A test that cannot distinguish a stub from real logic is itself a build failure.

3. **TEST SEAMS ARE DECLARED, NEVER IMPLICIT.** Exactly two test seams exist in this
   system: `MockLLMClient` (Section 8.4) and the in-process broker used by the test
   harness (Section 12). Both implement the identical interface as their production
   counterpart, and both are **forbidden in production**: the bootstrapper MUST refuse
   to start if a test seam is active while `TEST_MODE != "true"`. Any other fallback
   that bypasses a live code path is a stub.

4. **SCHEMAS ARE SINGLE-SOURCE.** All interface contracts live in
   `/common/models/schemas.py` as the Pydantic **v2** models in Section 4 — verbatim,
   complete, no additions, no inline dicts at API boundaries. If you believe a field is
   missing, emit `BLOCKED`; do not add it.

5. **NO SECRETS IN THE REPOSITORY. EVER.** No key, token, password, or secret material
   may appear in any committed file, including tests. All secrets come from environment
   variables; tests generate random secrets per run (Section 12). The repo ships with a
   `.gitignore` covering `.env`, `/data/`, `*.db`. A pre-commit secret scan
   (`scripts/secret_scan.py`, Section 13.2) must pass.

6. **NO `eval`, NO `exec`, NO `print` IN PRODUCTION CODE.** The AST detector
   (Section 13.1) flags all three. The calculator tool uses the whitelisted AST
   evaluator in Section 8.5. Logging uses the structured logger in Section 10 only.

7. **EVERY NETWORK ENDPOINT IS AUTHENTICATED.** No exceptions, including
   localhost-only services. `GET /health` endpoints are the sole unauthenticated routes.

8. **AST STUB DETECTOR ZERO TOLERANCE.** Before claiming completion, run
   `scripts/stub_detector.py`. Any flagged function = build incomplete.

9. **EVIDENCE CONTENT IS DATA, NEVER INSTRUCTIONS.** Files attached as evidence
   (and text read via `file_reader`) are untrusted input. They must not steer
   control flow, fabricate schema fields, or set mission confidence. Confidence
   and citations on `ExecutionResult` are assembled by the orchestrator from
   step validation outcomes and structured step outputs — never accepted
   verbatim from LLM free-text or an LLM top-level `confidence` field.
   Enforced by `/tests/integration/test_injection.py`.

---

## 1. SCOPE — V1 BUILDS THIS, V2 IS PARKED

**V1 (this document):**
- The Hive: FastAPI + SQLite (+ sqlite-vec for embeddings). Single process.
- One portable agent instance: bootstrapper, orchestrator, sub-agent spawner,
  memory/sync engine, comms link, airgap mode.
- Domain kit compiler (YAML → BlockDefs → generated tests).
- Evidence-or-refuse grounding gate.
- Prompt-injection canary (Rule 9 + `test_injection.py`).
- Full verification protocol.

**V2 (explicitly PARKED — do not build, do not scaffold, do not "prepare hooks for"):**

| Deferred item | V1 behavior | V2 note |
|---|---|---|
| NATS JetStream broker | Direct WebSocket Hive↔agent | Broker abstraction added when >1 concurrent agent is real |
| LanceDB | sqlite-vec inside the same SQLite file | Revisit only if vector volume demands it |
| Stripe billing / lending / rent tokens / self-destruct engine | `POST /billing/*` and `POST /lend*` return HTTP 501 with body `{"detail": "V2"}` | Full lease lifecycle in V2 |
| Style Miner | Not present | If revived: code + markdown only, no PDF parsing |
| Multi-agent mission leasing | Single agent; Hive rejects a second `register` for a different `agent_id` with 409 | V2 leases MUST carry fencing epochs checked on every Hive write |
| Block marketplace | Not present | — |
| Graduated approval levels (L1 auto / L2 user / L3 hardcoded) | Orchestrator executes; no tiered write gate | L3 actions frozen in code; config cannot lower |
| Evidence tiers (confirmed / reported / inferred / pending) | Binary evidence-or-refuse | Map onto `Citation.confidence`; only confirmed auto-writes |
| Hash-chained append-only audit ledger | Structured JSON logs only | `seq` / `prev_hash` / `event_hash`; SQLite revoke UPDATE/DELETE |
| Corpus/document versioning (latest-approved-wins) | Blocks use `version_clock`; evidence is per-mission | `doc_id` + `superseded_by`; conflict → clarify not guess |
| Fabricated-fact validator | Schema + evidence-or-refuse only | Deterministic post-hoc: uncited facts → TBC + confidence downgrade |
| Proposal diff contract (old/new/reason/source trigger) | Conflict replay shows both values | Make reason + source trigger mandatory on state changes |
| Rule-based health/status model | No LLM-assigned status field in V1 | Config predicates decide green/amber/red |

The 501 responses are complete implementations of the V1 contract, not stubs.

---

## 2. ARCHITECTURAL BLUEPRINT (V1)

```
┌──────────────────────────── THE HIVE (single FastAPI process) ────────────────────────────┐
│  SQLite /data/hive.db  ── tables: agents, memory_deltas, sync_conflicts,                  │
│                            block_registry, messages, missions_seen                        │
│  sqlite-vec virtual table: memories_vec                                                   │
│  Auth: per-agent secret + nonce challenge  →  JWT session (separate signing key)          │
│  WebSocket /sync  (JWT-authenticated, bidirectional)                                      │
└───────────────────────────────────────▲───────────────────────────────────────────────────┘
                                        │ TLS (deployment concern) / JWT session
┌───────────────────────────────────────▼───────────────────────────────────────────────────┐
│                       PORTABLE AGENT INSTANCE (Docker / binary)                           │
│  1. BOOTSTRAPPER      env validation → challenge-response auth → memory pull →            │
│                       constraint enforcement → resume checkpointed missions →             │
│                       start API :8000 (bearer-token gated)                                │
│  2. ORCHESTRATOR      Mission parse → block resolve (topo sort) → gated execute →         │
│                       checkpoint after every step → evidence-or-refuse                    │
│  3. SUB-AGENT SWARM   spawn-context processes, scrubbed env, join(timeout)+terminate,     │
│                       path-whitelisted file access, JSON-Schema-validated outputs         │
│  4. SYNC ENGINE       vector clocks keyed by node id, hive-wins + conflict archive,       │
│                       solo mode after 7 days unreachable                                  │
│  5. AIRGAP MODE       --network none, bundled Ollama, runtime smoke report               │
└───────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. REPOSITORY STRUCTURE

```
/self-agent-root
├── Makefile
├── docker-compose.yml
├── VERSION
├── requirements.txt            # pins: pydantic>=2.7,<3  fastapi  uvicorn  httpx
│                               #       websockets  PyJWT  jsonschema  sqlite-vec
│                               #       pytest  pytest-asyncio  psutil
├── .gitignore                  # .env, /data/, *.db, *.sqlite, __pycache__
├── /scripts
│   ├── stub_detector.py
│   └── secret_scan.py
├── /hive
│   ├── Dockerfile
│   ├── main.py                 # FastAPI app + WebSocket /sync
│   ├── auth.py                 # challenge-response + JWT issuance
│   ├── sync.py                 # clock comparison + conflict resolution
│   └── admin_cli.py            # agent enrollment, block registration
├── /agent
│   ├── Dockerfile              # standard image
│   ├── Dockerfile.airgap       # standard image + bundled Ollama + model
│   ├── entrypoint.sh
│   ├── cli.py
│   ├── bootstrapper.py
│   ├── memory.py               # SyncEngine
│   ├── comms.py                # CommsLink
│   ├── /orchestrator
│   │   └── engine.py
│   ├── /swarm
│   │   ├── spawner.py
│   │   └── tools.py            # calculator, file_reader, web_search, vector_search
│   └── /security
│       └── airgap.py
├── /common
│   ├── /models
│   │   ├── schemas.py          # Section 4, verbatim — SINGLE SOURCE
│   │   └── llm_client.py       # LLMClient ABC, OllamaClient, MockLLMClient
│   ├── logging.py              # get_logger factory (Section 10)
│   └── /storage
│       └── db.py               # SQLite helpers, migrations
├── /domain-kits
│   ├── /compiler
│   │   └── engine.py
│   ├── /sheets
│   │   └── port_ops.yaml
│   └── /generated              # compiler output (gitignored except .gitkeep)
└── /tests
    ├── conftest.py             # Section 12 — dynamic ports, seeding, key generation
    ├── /unit
    ├── /integration
    └── /generated
```

`/blocks` and `/factory` submodules are removed from V1. If a block or factory
capability is needed, it is defined as a `BlockDef` in this repo's registry. Emit
`BLOCKED` if you believe you need the submodules.

---

## 4. SHARED SCHEMAS — `/common/models/schemas.py` (Pydantic v2, verbatim)

This is the complete and final schema file. All fields from all prior drafts and
improvement lists are consolidated here. Nothing else may be added.

```python
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

def _uid() -> str:
    return str(uuid.uuid4())

def _now() -> datetime:
    return datetime.now(timezone.utc)

# ── Identity & auth ────────────────────────────────────────────────────────────

class AuthChallenge(BaseModel):
    agent_id: str
    nonce: str                      # 32 random bytes, hex, single-use, 60s TTL
    expires_at: datetime

class AuthProof(BaseModel):
    agent_id: str
    nonce: str
    proof: str                      # HMAC-SHA256(nonce, per_agent_secret), hex

class SessionGrant(BaseModel):
    session_token: str              # JWT signed with HIVE_JWT_KEY, 24h expiry
    memory_dump: list[MemoryDelta]
    blocks: list[BlockDef]

# ── Memory & sync ──────────────────────────────────────────────────────────────

class MemoryDelta(BaseModel):
    delta_id: str = Field(default_factory=_uid)
    timestamp: datetime = Field(default_factory=_now)
    origin: str                     # node id: "hive" or an agent_id
    vector_clock: dict[str, int]    # keyed by node id; multi-agent ready
    operation: Literal["upsert", "delete"]
    key: str
    value: Optional[dict[str, Any]] = None
    embedding: Optional[list[float]] = None

    @field_validator("vector_clock")
    @classmethod
    def clock_not_empty(cls, v: dict[str, int]) -> dict[str, int]:
        if not v:
            raise ValueError("vector_clock must not be empty")
        if any(n < 0 for n in v.values()):
            raise ValueError("clock entries must be non-negative")
        return v

class SyncConflict(BaseModel):
    conflict_id: str = Field(default_factory=_uid)
    key: str
    hive_value: Optional[dict[str, Any]] = None
    agent_value: Optional[dict[str, Any]] = None   # PRESERVED, never discarded
    hive_clock: dict[str, int]
    agent_clock: dict[str, int]
    winner: Literal["hive"] = "hive"               # V1 policy; V2 may extend
    resolved_at: datetime = Field(default_factory=_now)
    replayed: bool = False                          # set true via `agent conflicts replay`

class HiveMessage(BaseModel):
    msg_type: Literal["memory_delta", "command", "heartbeat",
                      "block_update", "ack", "conflict"]
    session_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    vector_clock: dict[str, int] = Field(default_factory=dict)

# ── Missions & execution ───────────────────────────────────────────────────────

class MissionProfile(BaseModel):
    mission_id: str = Field(default_factory=_uid)
    natural_language: str
    domain_hint: Optional[str] = None
    attached_evidence: list[dict[str, str]] = Field(default_factory=list)
    required_confidence: float = 0.9
    max_cost_usd: float = 1.0
    priority: int = 5               # 1 = highest, 10 = lowest

class PlanStep(BaseModel):
    step_id: str = Field(default_factory=_uid)
    block_id: str
    prompt_scope: str
    tool_set: list[str] = Field(default_factory=list)
    timeout_seconds: int = 60
    dependencies: list[str] = Field(default_factory=list)
    estimated_cost_usd: float = 0.0

class ExecutionPlan(BaseModel):
    plan_id: str = Field(default_factory=_uid)
    mission_id: str
    steps: list[PlanStep]

class StepOutput(BaseModel):
    step_id: str
    block_id: str
    output: dict[str, Any]
    validation_status: Literal["passed", "failed", "unchecked"]
    execution_time_ms: int

class Citation(BaseModel):
    source: str
    excerpt: str
    confidence: float

class ExecutionResult(BaseModel):
    mission_id: str
    status: Literal["success", "partial", "failure"]
    outputs: list[StepOutput]
    citations: list[Citation]
    confidence: float

class MissionState(BaseModel):
    mission_id: str
    status: Literal["queued", "running", "paused", "completed", "failed"]
    checkpoint: dict[str, Any] = Field(default_factory=dict)  # step_id -> StepOutput dump
    resumed_from: Optional[str] = None

# ── Blocks ─────────────────────────────────────────────────────────────────────

class BlockDef(BaseModel):
    block_id: str
    name: str
    domain: str
    input_schema: dict[str, Any]          # JSON Schema draft 7
    output_json_schema: dict[str, Any]    # JSON Schema draft 7, enforced at spawner
    system_prompt_template: str
    tools: list[str] = Field(default_factory=list)
    evidence_standard: Optional[str] = None
    dependencies: list[str] = Field(default_factory=list)
    version_clock: int = 1                # Hive increments on every change

# ── Ops & reporting ────────────────────────────────────────────────────────────

class AirGapReport(BaseModel):
    verified: bool
    timestamp: datetime = Field(default_factory=_now)
    tests_passed: list[str]
    tests_failed: list[str]
    config_checksum: str

class LogEntry(BaseModel):
    timestamp: datetime = Field(default_factory=_now)
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    component: str                  # "hive.sync", "agent.spawner", ...
    trace_id: str                   # mission_id or session_id or "-"
    message: str
    metrics: Optional[dict[str, float]] = None
```

Removed from prior drafts and NOT to be reintroduced in V1: `AgentIdentity`
(replaced by challenge-response), `StyleCartouche` (V2), `LeaseToken` (V2),
`MissionLease` (V2), `MissionQueue`-as-model (queue is runtime state, Section 8.2).

---

## 5. SECURITY MODEL

### 5.1 Keys — three, never reused, never committed

| Key | Purpose | Where |
|---|---|---|
| `per_agent_secret` | One per enrolled agent, 32 random bytes. Proves agent identity in challenge-response. | Generated by `hive-admin agent add`. Stored in Hive SQLite `agents` table. Delivered to the agent operator out-of-band once; agent stores it in `AGENT_SECRET` env. |
| `HIVE_JWT_KEY` | Signs session JWTs. | Hive env only. Boot MUST fail with a clear error if unset, empty, or shorter than 32 chars. |
| `AGENT_API_TOKEN` | Bearer token gating the agent's local mission API (:8000). | Agent env only. Same boot-fail rule. |

Key reuse across purposes is forbidden. Tests generate all three randomly per run
(Section 12) — no key literal may appear in any committed file (Rule 5; enforced by
`secret_scan.py`).

### 5.2 Agent authentication — challenge-response

1. `GET /auth/challenge?agent_id=X` → Hive returns `AuthChallenge` (random 32-byte
   nonce, 60s TTL, single-use, stored server-side).
2. Agent computes `proof = HMAC-SHA256(nonce, AGENT_SECRET)` and `POST /auth/prove`
   with `AuthProof`.
3. Hive verifies against the stored per-agent secret using `hmac.compare_digest`,
   consumes the nonce, returns `SessionGrant` (JWT with `sub=agent_id`, `exp` +24h,
   signed `HIVE_JWT_KEY` HS256; plus memory dump and block registry).
4. Replayed or expired nonces → 401. Unknown agent → 401 (same error body — no
   enumeration).

There is no static derived token, and there is no unused `public_key` field.
Asymmetric identity is a V2 concern.

### 5.3 WebSocket authentication

`WebSocket /sync` requires the session JWT as the first frame:
`{"auth": "<jwt>"}` within 5 seconds of connect. Invalid, expired, or missing token →
close code 4401. The `sub` claim is the only accepted `session_id` for all subsequent
frames on that socket; a frame whose `session_id` mismatches `sub` → close 4403.

### 5.4 Agent mission API

Every route on :8000 except `GET /health` requires header
`Authorization: Bearer <AGENT_API_TOKEN>`; otherwise 401. Constant-time comparison.

**Acceptance Test (`/tests/integration/test_auth.py`):**
```python
import hashlib, hmac as hmac_mod
import httpx, pytest
import websockets

pytestmark = pytest.mark.asyncio

async def test_unknown_agent_and_bad_proof_rejected(hive):
    async with httpx.AsyncClient(base_url=hive.url) as c:
        r = await c.get("/auth/challenge", params={"agent_id": "ghost"})
        # Challenge may be issued blindly (no enumeration), but proof must fail:
        nonce = r.json()["nonce"]
        r2 = await c.post("/auth/prove", json={
            "agent_id": "ghost", "nonce": nonce, "proof": "00" * 32})
        assert r2.status_code == 401

async def test_challenge_response_issues_jwt(hive, enrolled_agent):
    agent_id, secret = enrolled_agent
    async with httpx.AsyncClient(base_url=hive.url) as c:
        nonce = (await c.get("/auth/challenge",
                             params={"agent_id": agent_id})).json()["nonce"]
        proof = hmac_mod.new(secret.encode(), nonce.encode(),
                             hashlib.sha256).hexdigest()
        r = await c.post("/auth/prove", json={
            "agent_id": agent_id, "nonce": nonce, "proof": proof})
        assert r.status_code == 200
        assert r.json()["session_token"]
        # Nonce is single-use:
        r2 = await c.post("/auth/prove", json={
            "agent_id": agent_id, "nonce": nonce, "proof": proof})
        assert r2.status_code == 401

async def test_ws_sync_rejects_unauthenticated(hive):
    async with websockets.connect(hive.ws_url) as ws:
        await ws.send('{"auth": "not-a-jwt"}')
        with pytest.raises(websockets.ConnectionClosed) as exc:
            await ws.recv()
        assert exc.value.rcvd.code == 4401

async def test_mission_api_requires_bearer(agent_api):
    async with httpx.AsyncClient(base_url=agent_api.url) as c:
        r = await c.post("/mission", json={"natural_language": "x"})
        assert r.status_code == 401
        r = await c.get("/health")
        assert r.status_code == 200
```

---

## 6. THE HIVE (`/hive`)

### 6.1 Endpoints (`main.py`)

| Route | Auth | Behavior |
|---|---|---|
| `GET /health` | none | `{"status": "ok", "version": <VERSION>}` |
| `GET /auth/challenge` | none (rate-limited: 10/min/agent_id) | Issue `AuthChallenge` |
| `POST /auth/prove` | proof | Verify, return `SessionGrant` (Section 5.2). Registers the agent connection. A second **different** `agent_id` proving while one is enrolled-and-active → 409 (V1 is single-agent). |
| `WS /sync` | JWT first-frame | Bidirectional sync (Section 6.3) |
| `POST /message` | JWT | Store `{"target_agent_id", "payload"}` in `messages`; delivered on next sync |
| `POST /admin/blocks` | JWT + `role=admin` claim | Upsert list of `BlockDef`; increments `version_clock` on change |
| `POST /lend`, `POST /lend/redeem`, `POST /billing/*` | — | HTTP 501 `{"detail": "V2"}` |

Admin JWTs are minted by `hive/admin_cli.py` using `HIVE_JWT_KEY` with `role=admin`,
1h expiry.

### 6.2 Database — SQLite `/data/hive.db`

Tables: `agents(agent_id PK, secret, enrolled_at, active)`,
`memory_deltas(delta_id PK, key, value, origin, vector_clock, operation, timestamp)`,
`sync_conflicts` (columns mirror `SyncConflict`),
`block_registry(block_id PK, def_json, version_clock)`,
`messages(id PK, target_agent_id, payload, delivered)`,
`nonces(nonce PK, agent_id, expires_at, used)`.

Embeddings: sqlite-vec virtual table `memories_vec(embedding float[N])` joined to
`memory_deltas` by rowid. `N` is fixed by config `EMBEDDING_DIM` (default 384) and
validated on every insert — dimension mismatch is a hard error, never a silent cast.

Schema migrations: `common/storage/db.py` maintains `schema_version(version INTEGER)`;
on boot, apply numbered migration functions in order; if the DB's version is newer than
the code, refuse to boot with a clear error.

### 6.3 Sync protocol

**Clocks.** `vector_clock: dict[node_id, int]`. Nodes are `"hive"` and each
`agent_id`. Missing keys are treated as 0. Comparison:

- A **dominates** B if for every node, `A[n] >= B[n]`, and strictly greater for at
  least one node.
- If neither dominates → **concurrent**.

**Rules:**

1. Heartbeat: agent sends `msg_type=heartbeat` with its clock. Hive replies with every
   delta the agent's clock has not seen, then `ack` carrying the Hive clock.
2. Agent delta dominates Hive state for that key → Hive applies it, merges clocks
   (element-wise max, +1 on `"hive"`), broadcasts.
3. Concurrent → **Hive wins**, and the losing agent value is **archived, never
   discarded**: Hive writes a `SyncConflict` row containing both values and both
   clocks, sends `msg_type=conflict` with the resolved value and the `conflict_id`.
   The agent stores the same conflict row locally. `agent.cli conflicts list` and
   `agent.cli conflicts replay <conflict_id>` allow deliberate manual re-application
   (replay creates a **new** delta with a fresh dominating clock and sets
   `replayed=true`). Automatic re-push of losing values is forbidden — it would defeat
   the policy.
4. Solo mode: Hive unreachable > `solo_mode_threshold_days` (7) → agent continues
   local writes, logs WARNING every 24h, and on reconnect performs a full clock
   exchange before accepting commands.

**Acceptance Test (`/tests/integration/test_sync.py`):**
```python
import json, sqlite3
import pytest

pytestmark = pytest.mark.asyncio

async def test_sync_pulls_seeded_deltas(hive, authed_ws, seed_hive_delta):
    for i in range(3):
        seed_hive_delta(key=f"k{i}", value={"v": i})
    ws = await authed_ws()
    await ws.send(json.dumps({"msg_type": "heartbeat", "session_id": ws.agent_id,
                              "payload": {}, "vector_clock": {}}))
    deltas, ack = [], None
    while ack is None:
        m = json.loads(await ws.recv())
        if m["msg_type"] == "memory_delta":
            deltas.append(m)
        elif m["msg_type"] == "ack":
            ack = m
    assert {d["payload"]["key"] for d in deltas} == {"k0", "k1", "k2"}
    assert ack["vector_clock"]["hive"] >= 3

async def test_concurrent_conflict_hive_wins_and_archives(hive, authed_ws,
                                                          seed_hive_delta):
    seed_hive_delta(key="rate", value={"v": 100},
                    clock={"hive": 5, "agent-under-test": 1})
    ws = await authed_ws()
    await ws.send(json.dumps({
        "msg_type": "memory_delta", "session_id": ws.agent_id,
        "payload": {"operation": "upsert", "key": "rate", "value": {"v": 200}},
        "vector_clock": {"hive": 4, "agent-under-test": 2}}))   # concurrent
    conflict = None
    while conflict is None:
        m = json.loads(await ws.recv())
        if m["msg_type"] == "conflict":
            conflict = m
    assert conflict["payload"]["winner"] == "hive"
    assert conflict["payload"]["resolved_value"] == {"v": 100}
    con = sqlite3.connect(hive.db_path)
    row = con.execute(
        "SELECT agent_value FROM sync_conflicts WHERE key='rate'").fetchone()
    con.close()
    assert json.loads(row[0]) == {"v": 200}      # losing value preserved
```

---

## 7. THE AGENT — BOOTSTRAPPER (`/agent/bootstrapper.py`)

Boot sequence — every step must complete or boot fails loudly:

1. **Env validation.** Required: `AGENT_ID`, `AGENT_API_TOKEN`. If `AIRGAP != "true"`:
   also `HIVE_URL`, `AGENT_SECRET`. Missing/empty → exit 1 with the exact variable
   named. If a test seam is active (`LLM_CLIENT=mock` or in-process broker) while
   `TEST_MODE != "true"` → exit 1 (Rule 3).
2. **Constraint enforcement (Section 11).** Disk check via `shutil.disk_usage("/data")`
   before anything else; over limit → refuse new missions but still boot for
   read-only inspection.
3. **Airgap branch.** `AIRGAP=true`: load `/data/preload/memory.sqlite` and
   `/data/preload/blocks.json`; skip all network calls; run
   `agent.security.airgap.verify()`; exit 1 unless `verified=true`.
4. **Online branch.** Challenge-response auth (Section 5.2) → apply `memory_dump` and
   `blocks` locally → start `SyncEngine` and `CommsLink` background threads.
5. **Mission recovery.** Query local `missions` table for `status="running"`; rebuild
   each `ExecutionPlan` from its `MissionState.checkpoint` and resume from the first
   uncompleted step, with `resumed_from` set. Push a `command/resume` notification to
   the Hive (online mode).
6. **Start API** on :8000 — `POST /mission`, `GET /mission/{id}`, `GET /health` —
   bearer-gated per Section 5.4.

`entrypoint.sh` must trap SIGTERM:
```bash
trap 'python3 -m agent.cli shutdown --grace-period=30' TERM
```
Shutdown checkpoints all running missions (state → `paused`) before exit.

**Acceptance Test (`/tests/unit/test_bootstrapper.py`):**
```python
import os, sqlite3, sys, importlib
import pytest
from unittest.mock import patch

def test_boot_fails_on_missing_env(clean_env, tmp_path):
    os.environ.update({"AGENT_ID": "a1", "DATA_DIR": str(tmp_path)})
    # AGENT_API_TOKEN deliberately absent
    from agent import bootstrapper
    with pytest.raises(SystemExit) as e:
        bootstrapper.main()
    assert e.value.code == 1

def test_boot_refuses_mock_llm_outside_test_mode(clean_env, tmp_path, agent_env):
    os.environ.update(agent_env)                 # full valid env from fixture
    os.environ["LLM_CLIENT"] = "mock"
    os.environ["TEST_MODE"] = "false"
    from agent import bootstrapper
    with pytest.raises(SystemExit):
        bootstrapper.main()

def test_airgap_boot_makes_no_network_calls(clean_env, tmp_path, airgap_preload):
    os.environ.update({"AGENT_ID": "airgap-1", "AGENT_API_TOKEN": "t" * 32,
                       "AIRGAP": "true", "DATA_DIR": str(tmp_path),
                       "PRELOAD_DIR": airgap_preload, "TEST_MODE": "true",
                       "SKIP_AIRGAP_VERIFY": "false"})
    with patch("socket.socket.connect",
               side_effect=AssertionError("network call in airgap")) as mc:
        from agent import bootstrapper
        result = bootstrapper.main(serve=False)
    assert result["airgap"] is True
    assert result["hive_connected"] is False
    assert mc.call_count == 0

def test_online_boot_pulls_memory(hive_with_deltas, clean_env, tmp_path, agent_env):
    # hive_with_deltas: live Hive fixture pre-seeded (in code) with 3 deltas
    os.environ.update(agent_env)
    from agent import bootstrapper
    result = bootstrapper.main(serve=False)
    assert result["hive_connected"] is True
    con = sqlite3.connect(os.path.join(str(tmp_path), "agent.db"))
    assert con.execute("SELECT COUNT(*) FROM memory_deltas").fetchone()[0] == 3
    con.close()
```

---

## 8. ORCHESTRATOR & SWARM

### 8.1 Orchestrator (`/agent/orchestrator/engine.py`)

```python
class HeadlessOrchestrator:
    def __init__(self, registry, retriever, spawner, store): ...

    def resolve(self, profile: MissionProfile) -> ExecutionPlan:
        # 1. Domain: profile.domain_hint, else vector search over block descriptions.
        # 2. EVIDENCE GATE: if the selected domain declares evidence_standard on any
        #    block and profile.attached_evidence is empty -> return a one-step plan
        #    whose execution refuses (Section 8.3). Never invent evidence.
        # 3. Select blocks whose input_schema is satisfiable from evidence + profile.
        # 4. Topological sort on BlockDef.dependencies; cycle -> ValueError.
        # 5. Cost gate: sum(step.estimated_cost_usd) > profile.max_cost_usd ->
        #    plan is the refusal plan with reason "cost".
```

`execute(plan)` runs steps in dependency order via the spawner; after **every** step,
persist `MissionState.checkpoint[step_id] = StepOutput.model_dump()` to local SQLite
(this is the crash-recovery contract of Section 7 step 5). On a step
`validation_status="failed"` → halt chain, `status="failure"`, `confidence=0.0`.
On success, confidence = mean of per-step confidences assigned by the orchestrator
from `validation_status` (`1.0` if passed, `0.0` if failed) — never from an LLM
top-level or citation `confidence` float (Rule 9). Citations are aggregated from
step outputs with orchestrator-assigned confidence on each `Citation`.

Implementation placeholders during development use
`raise NotImplementedError("BLOCKED: <reason>")` — the detector flags these
(Section 13.1), so a build containing any of them cannot claim completion.

### 8.2 Mission queue (runtime state, not a schema)

In-process priority queue; `max_depth=100`. Full → `POST /mission` returns 429
`{"detail": "queue saturated"}`. Priority from `MissionProfile.priority`.

### 8.3 Evidence-or-refuse

A refusal is a real `ExecutionResult`:
`status="failure"`, one `StepOutput` with
`output={"reason": "evidence required: <standard>"}`, `confidence=0.0`, no citations.

**Acceptance Test (`/tests/integration/test_refusal.py`):**
```python
def test_mission_without_evidence_is_refused(orchestrator_with_port_blocks):
    orch = orchestrator_with_port_blocks
    profile = MissionProfile(natural_language="Calculate berth fee",
                             domain_hint="port_terminal", attached_evidence=[])
    result = orch.execute(orch.resolve(profile))
    assert result.status == "failure"
    assert result.confidence == 0.0
    assert any("evidence" in str(o.output.get("reason", "")).lower()
               for o in result.outputs)
```

**End-to-end Acceptance Test (`/tests/integration/test_orchestrator_mission.py`):**
```python
def test_orchestrator_end_to_end(orchestrator_with_adder, evidence_file):
    # orchestrator_with_adder (conftest): registers block "adder"
    #   input_schema: {"a": {"type": "integer"}, "b": {"type": "integer"}}
    #   output_json_schema: {"type": "object", "properties":
    #       {"sum": {"type": "integer"}}, "required": ["sum"]}
    #   MockLLMClient keyed to return {"sum": a+b} with a citation of the evidence.
    # evidence_file (conftest): tmp file containing "a=5, b=3"
    orch = orchestrator_with_adder
    profile = MissionProfile(
        natural_language="add the two numbers in the evidence",
        domain_hint="math",
        attached_evidence=[{"type": "txt", "path": evidence_file}])
    plan = orch.resolve(profile)
    assert plan.steps and plan.steps[0].block_id == "adder"
    result = orch.execute(plan)
    assert result.status == "success"
    assert result.outputs[0].output["sum"] == 8
    assert result.outputs[0].validation_status == "passed"
    assert result.citations and "a=5" in result.citations[0].excerpt
```

**Injection canary (`/tests/integration/test_injection.py`) — Rule 9:**
```python
def test_evidence_injection_cannot_steer_adder(orchestrator_with_adder_injection, poisoned_evidence):
    # poisoned_evidence: "a=5, b=3" plus embedded instruction to fabricate sum=999
    # and inflate/report confidence=1.0 (or any distinctive LLM confidence float).
    # MockLLMClient is forced to place a misleading top-level confidence and
    # citation.confidence while still parsing a=/b= for the arithmetic sum.
    orch = orchestrator_with_adder_injection
    profile = MissionProfile(
        natural_language="add the two numbers in the evidence",
        domain_hint="math",
        attached_evidence=[{"type": "txt", "path": poisoned_evidence}])
    result = orch.execute(orch.resolve(profile))
    assert result.status == "success"
    assert result.outputs[0].validation_status == "passed"
    assert result.outputs[0].output["sum"] == 8
    assert result.confidence == 1.0          # orchestrator from validation, not LLM
    assert result.confidence != 0.01         # distinctive LLM top-level must not win
    assert result.citations[0].confidence == 1.0
```

### 8.4 LLM client (`/common/models/llm_client.py`)

```python
class LLMClient(ABC):
    @abstractmethod
    def generate(self, prompt: str, system: str, format: str = "json") -> dict: ...

class OllamaClient(LLMClient):
    # POST {OLLAMA_URL}/api/generate, format="json", stream=False.
    # If the live API schema differs from /api/tags and /api/generate: BLOCKED.

class MockLLMClient(LLMClient):
    # DECLARED TEST SEAM (Rule 3). Deterministic JSON keyed on prompt content,
    # configured by the test via constructor. Selected only when LLM_CLIENT=mock;
    # bootstrapper refuses it outside TEST_MODE=true.
```

### 8.5 Sub-agent spawner (`/agent/swarm/spawner.py`)

Isolation contract — all six are mandatory:

1. **Spawn, not fork.** `multiprocessing.get_context("spawn")`. Fork would inherit
   parent memory including secrets.
2. **Scrubbed environment.** The child receives an explicit env dict containing ONLY
   `PATH`, `PYTHONPATH`, `AIRGAP`, `TEST_MODE`, `DATA_DIR`, `OLLAMA_URL`. It must
   never contain `AGENT_SECRET`, `AGENT_API_TOKEN`, `HIVE_JWT_KEY`, or any `*_KEY`/
   `*_TOKEN`/`*_SECRET` variable.
3. **Timeout.** `process.join(timeout_seconds)`; still alive → `terminate()`, 2s
   grace, then `kill()`. Return `StepOutput(validation_status="failed",
   output={"reason": "timeout"})`. No signal-based (SIGALRM) timeouts anywhere —
   they do not work off the main thread.
4. **Path whitelist.** `file_reader` resolves `os.path.realpath` and requires the
   result to be inside `DATA_DIR/evidence` or `/tmp/evidence`; otherwise
   `PermissionError`. No `chroot` — it requires root and contradicts the non-root
   container (Section 9.3).
5. **Output validation.** Parse child output as JSON; `jsonschema.validate` against
   `BlockDef.output_json_schema`. On failure: retry once with the schema appended to
   the prompt; second failure → `validation_status="failed"`.
6. **Concurrency cap.** Max `max_subagent_processes` (8) alive; further steps queue.

Tool registry (`tools.py`): `calculator` (whitelisted-AST evaluator below),
`file_reader` (rule 4), `web_search` (raises `AirGapError` when `AIRGAP=true`),
`vector_search` (local sqlite-vec query).

```python
# calculator — the ONLY permitted expression evaluator. eval/exec are forbidden.
import ast, operator as op
_OPS = {ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv,
        ast.Pow: op.pow, ast.Mod: op.mod, ast.USub: op.neg, ast.UAdd: op.pos}

def calculator(expression: str) -> float:
    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.operand))
        raise ValueError(f"disallowed expression node: {type(node).__name__}")
    return ev(ast.parse(expression, mode="eval"))
```

**Acceptance Test (`/tests/unit/test_spawner.py`):**
```python
import os, pytest, psutil
from agent.swarm.spawner import SubAgentSpawner
from agent.swarm.tools import calculator
from common.models.schemas import PlanStep

def test_timeout_kills_process(registry_with_sleeper, spawner_env):
    spawner = SubAgentSpawner(registry_with_sleeper)
    step = PlanStep(block_id="sleeper", prompt_scope="sleep",
                    tool_set=[], timeout_seconds=1)
    out = spawner.run_block(step, {}, [])
    assert out.validation_status == "failed"
    assert out.output["reason"] == "timeout"
    assert not psutil.pid_exists(out.output["pid"])   # spawner reports child pid

def test_child_env_is_scrubbed(registry_with_envdump, spawner_env):
    os.environ["AGENT_SECRET"] = "s" * 32
    os.environ["AGENT_API_TOKEN"] = "t" * 32
    spawner = SubAgentSpawner(registry_with_envdump)
    step = PlanStep(block_id="envdump", prompt_scope="dump", tool_set=[])
    out = spawner.run_block(step, {}, [])
    child_env = out.output["env"]
    assert not any(k.endswith(("_SECRET", "_TOKEN", "_KEY")) for k in child_env)

def test_airgap_blocks_web_search(registry_with_searcher, spawner_env, monkeypatch):
    monkeypatch.setenv("AIRGAP", "true")
    spawner = SubAgentSpawner(registry_with_searcher)
    step = PlanStep(block_id="searcher", prompt_scope="search",
                    tool_set=["web_search"], timeout_seconds=5)
    out = spawner.run_block(step, {}, [])
    assert out.validation_status == "failed"
    assert "airgap" in str(out.output).lower()

def test_file_reader_blocks_escape(spawner_env):
    from agent.swarm.tools import file_reader
    with pytest.raises(PermissionError):
        file_reader("/etc/passwd")
    with pytest.raises(PermissionError):
        file_reader(os.path.join(os.environ["DATA_DIR"],
                                 "evidence/../../etc/passwd"))

def test_calculator_rejects_code():
    assert calculator("2 + 3 * 4") == 14
    with pytest.raises(ValueError):
        calculator("__import__('os').system('id')")
```

### 8.6 Comms (`/agent/comms.py`)

Persistent authenticated WebSocket to Hive `/sync`. On
`command {"action": "run_mission", "profile": ...}` → enqueue. On
`{"action": "status"}` → reply with queue state. On completion → push
`msg_type="block_update"` with the full `ExecutionResult`. Block registry updates
from the Hive: compare `version_clock` per block; if a running mission uses an
outdated block, **finish the mission on the old version with a WARNING log**, then
reload. Never hot-swap mid-mission.

**Acceptance Test (`/tests/integration/test_comms.py`):**
```python
import pytest
pytestmark = pytest.mark.asyncio

async def test_command_triggers_mission_and_reports(full_stack, adder_profile):
    # full_stack (conftest): live Hive + live agent (MockLLM adder), both on
    # dynamic ports, agent authenticated.
    hive = full_stack.hive
    await hive.send_command(agent_id=full_stack.agent_id,
                            payload={"action": "run_mission",
                                     "profile": adder_profile.model_dump()})
    msg = await hive.wait_for("block_update", timeout=30)
    result = ExecutionResult(**msg["payload"])
    assert result.mission_id == adder_profile.mission_id
    assert result.status == "success"
```

### 8.7 Airgap (`/agent/security/airgap.py`)

The **enforcement** is `docker run --network none` with `Dockerfile.airgap` — an
image that bundles Ollama and a small model (`llama3.2:3b`) so inference is
in-container at `http://127.0.0.1:11434`. The verifier is a **smoke report**, not the
guarantee, and the spec must say so in its module docstring.

Checks (each appends to `tests_passed`/`tests_failed`):
1. No cloud LLM env vars (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GROQ_API_KEY`,
   `KIMI_API_KEY`, `GEMINI_API_KEY`).
2. `socket.create_connection(("8.8.8.8", 53), timeout=2)` must FAIL.
3. `socket.gethostbyname("example.com")` must FAIL.
4. Bundled Ollama responds on `127.0.0.1:11434/api/tags`.
5. SHA-256 checksum of `/app/config/agent.yaml` recorded.

`verified = not tests_failed`. Exit code 0 iff verified.

**Acceptance Test (`/tests/integration/test_airgap.py`)** — runs only inside the
airgapped container (marked `@pytest.mark.airgap`, invoked by `make verify-airgap`):
```python
def test_airgap_report_verified():
    from agent.security.airgap import AirGapVerifier
    report = AirGapVerifier().verify()
    assert report.verified is True
    assert report.tests_failed == []
    assert len(report.config_checksum) == 64
```

---

## 9. LOGGING, COMPILER, DEPLOYMENT

### 9.1 Structured logging (`/common/logging.py`)

Single factory `get_logger(component: str)` emitting one JSON line per event,
conforming to `LogEntry`. `print()` is forbidden in production code and flagged by
the detector. Rotation: 100 MB, 5 backups (`logging.handlers.RotatingFileHandler`).

### 9.2 Domain kit compiler (`/domain-kits/compiler/engine.py`)

Input: a domain YAML (see `sheets/port_ops.yaml`, which must ship with real content:
at least one `data_table` **tariffs**, one `calculation` **berth_fee**, one
`decision_rule` **max_dwell**, one `failure_mode`). For each:

1. `data_table` → `BlockDef` `{domain}_{table}_lookup`
2. `calculation` → `BlockDef` `{domain}_{calc}` (formula in system prompt)
3. `decision_rule` → `BlockDef` `{domain}_{rule}_gate` with
   `output_json_schema` requiring `{"allowed": bool, "reason": str}`
4. `failure_mode` → pytest file in `/tests/generated/test_{domain}_{name}.py`

Every emitted JSON must round-trip through `BlockDef.model_validate`. Output to
`/domain-kits/generated/`.

**Acceptance Test (`/tests/unit/test_compiler.py`):**
```python
import glob, json, os, subprocess
from common.models.schemas import BlockDef

def test_compiler_generates_valid_blocks(tmp_generated_dir):
    from domain_kits.compiler.engine import compile_sheet
    compile_sheet("domain-kits/sheets/port_ops.yaml", out_dir=tmp_generated_dir)
    names = {os.path.basename(p) for p in glob.glob(f"{tmp_generated_dir}/*.json")}
    assert {"port_terminal_tariffs_lookup.json",
            "port_terminal_berth_fee.json",
            "port_terminal_max_dwell_gate.json"} <= names
    for p in glob.glob(f"{tmp_generated_dir}/*.json"):
        with open(p) as f:
            BlockDef.model_validate(json.load(f))
    r = subprocess.run(["python3", "-m", "pytest", "tests/generated/", "-q"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
```

### 9.3 Docker & Makefile

`docker-compose.yml` services: `hive-server` (:8001), `agent-instance` (:8000),
`ollama` (:11434, pulls `llama3.2:3b` on first start). Both app containers run as a
non-root user:

```dockerfile
RUN groupadd -r agent && useradd -r -g agent agent \
 && mkdir -p /data && chown -R agent:agent /data /app
USER agent
```

No Docker socket is mounted anywhere. Health checks are mandatory and `depends_on`
uses `condition: service_healthy`:

```yaml
services:
  hive-server:
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8001/health"]
      interval: 5s
      timeout: 3s
      retries: 5
  ollama:
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:11434/api/tags"]
      interval: 10s
      timeout: 5s
      retries: 10
  agent-instance:
    depends_on:
      hive-server: {condition: service_healthy}
      ollama: {condition: service_healthy}
```

```makefile
.PHONY: build up down test test-unit test-integration compile-domain verify-airgap detect clean

build:
	docker compose build

up:
	docker compose up -d --wait

down:
	docker compose down

detect:
	python3 scripts/stub_detector.py
	python3 scripts/secret_scan.py

test-unit:
	python3 -m pytest tests/unit/ -v

test-integration:
	python3 -m pytest tests/integration/ -v -m "not airgap"

test: detect test-unit test-integration

compile-domain:
	python3 -m domain_kits.compiler.engine --sheet domain-kits/sheets/port_ops.yaml

verify-airgap:
	docker build -f agent/Dockerfile.airgap -t self_agent_airgap .
	docker run --rm --network none -e AIRGAP=true -e TEST_MODE=true \
		-v $(PWD)/tests/fixtures/preload:/data/preload \
		self_agent_airgap python3 -m agent.security.airgap

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
```

Integration tests do **not** depend on compose ports — the harness (Section 12)
boots Hive and agent in-process on OS-assigned ports. Compose is for running the
system, not for testing it.

---

## 10. OPERATIONAL CONSTRAINTS — ENFORCED, NOT DECLARED

```python
OPERATIONAL_CONSTRAINTS = {
    "max_disk_gb": 10,
    "log_rotate_mb": 100,
    "log_backups": 5,
    "solo_mode_threshold_days": 7,
    "max_subagent_processes": 8,
    "subagent_default_timeout_sec": 60,
    "mission_queue_max_depth": 100,
}
```

Each constraint names its enforcement point, and each enforcement point has a test:

| Constraint | Enforced in | Test |
|---|---|---|
| `max_disk_gb` | bootstrapper (boot) + orchestrator (per mission accept: 429 when over) | `test_disk_limit_rejects_missions` (patch `shutil.disk_usage`) |
| `solo_mode_threshold_days` | SyncEngine on every failed sync attempt | `test_solo_mode_activates` (freeze time) |
| `max_subagent_processes` | spawner semaphore | `test_spawner_caps_concurrency` |
| `mission_queue_max_depth` | mission API (429) | `test_queue_saturation_returns_429` |
| log rotation | `get_logger` handler config | `test_logger_rotates` (small limit override) |

A constraint present in the dict but not wired to an enforcement point is a build
failure.

---

## 11. VERIFICATION PROTOCOL

### 11.1 AST stub detector (`/scripts/stub_detector.py`) — final version

Flags, in all non-test production code:

- `pass`-only, docstring-only, and bare-literal-return bodies
- bodies that are only `raise NotImplementedError(...)` / `raise Exception(...)` /
  `raise RuntimeError(...)` (the BLOCKED marker cannot survive into a finished build)
- any call to `eval` or `exec`
- any call to `print`

Triviality check — **calls count as meaningful** (an `ast.Expr` wrapping an
`ast.Call` is real work; only docstring `Expr`s are excluded), and the check applies
**only** to the components this spec names, via an explicit allowlist — otherwise it
cries wolf on legitimate one-liners and gets disabled.

```python
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
```

Meta-test (`/tests/unit/test_stub_detector.py`) — the detector itself is tested:
feed it fixture files containing (a) a `pass` stub, (b) a docstring-only body,
(c) `raise NotImplementedError("BLOCKED")`, (d) an `eval` call, (e) a legitimate
two-call function like `def f(db): db.begin(); db.commit()`. Assert a–d are flagged
and e is NOT — this pins the `ast.Expr(Call)`-counts-as-meaningful behavior.

### 11.2 Secret scan (`/scripts/secret_scan.py`)

Walks all committed text files; fails on: 32+ char hex/base64 literals assigned to
names matching `(?i)(secret|token|key|password)`, `AKIA[0-9A-Z]{16}`,
`-----BEGIN.*PRIVATE KEY`. Allowlist file `scripts/secret_scan_allow.txt` for
documented false positives (must stay empty in V1).

### 11.3 Mutation gate — executable, not prose

```makefile
mutation-gate:
	python3 scripts/mutation_gate.py --targets \
		agent/orchestrator/engine.py agent/swarm/spawner.py hive/sync.py
```

`scripts/mutation_gate.py`: for each target module, for each public function named in
this spec, produce a mutated copy whose body is `return None` (via AST rewrite into a
shadow tree on `sys.path`), run that module's acceptance tests, and require **failure**.
Any test that passes against the mutant = that test is testing a mock, and the gate
exits 1 naming it. Budget: this runs the named modules only, not the whole tree.

### 11.4 Verification steps, in order — all must pass to claim completion

1. `make detect` — stub detector + secret scan, exit 0.
2. `make test-unit` — all pass.
3. `make test-integration` — all pass (dynamic ports; no compose dependency).
4. `make mutation-gate` — all mutants killed.
5. `make verify-airgap` — `verified: true`, `tests_failed: []`.
6. `make compile-domain && python3 -m pytest tests/generated/ -v` — all pass.
7. **Live E2E** (compose up):
   ```bash
   echo "Container type: dry. Dwell hours: 24. Base rate: 10 USD/hr." \
     > /tmp/evidence/tariff.txt
   curl -X POST http://localhost:8000/mission \
     -H "Authorization: Bearer $AGENT_API_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"natural_language": "Calculate berth fee using attached tariff evidence",
          "domain_hint": "port_terminal",
          "attached_evidence": [{"type": "txt", "path": "/tmp/evidence/tariff.txt"}],
          "required_confidence": 0.9}'
   ```
   Must return `ExecutionResult` with `status` in `["success", "partial"]`,
   non-empty `citations` whose every `excerpt` appears verbatim in the evidence
   file, and `confidence >= 0.9`. The same request without `attached_evidence`
   must return the Section 8.3 refusal.

Report the exact terminal output of all seven steps. A step that fails is a
component that is not done.

---

## 12. TEST HARNESS (`/tests/conftest.py`) — fixtures are code, not comments

Every fixture referenced by the acceptance tests above is implemented here. No
acceptance test may rely on manual pre-seeding, hardcoded ports, or hardcoded keys.

Required fixtures (implement all; signatures below are the contract):

```python
# Keys: generated fresh per session. NEVER read from the repo.
@pytest.fixture(scope="session")
def keys():
    import secrets
    return {"jwt": secrets.token_hex(32), "api": secrets.token_hex(32)}

@pytest.fixture()
def hive(keys, tmp_path_factory):
    """Boot the real Hive app via uvicorn on an OS-assigned port (bind port 0,
    read back the actual port). Yields an object with .url, .ws_url, .db_path,
    .send_command(), .wait_for(). Tears down the server and deletes the DB."""

@pytest.fixture()
def enrolled_agent(hive):
    """Enroll 'agent-under-test' through hive.admin (the real enrollment path),
    return (agent_id, per_agent_secret)."""

@pytest.fixture()
def authed_ws(hive, enrolled_agent):
    """Factory: perform real challenge-response, open /sync, send the JWT auth
    frame, return the connected websocket with .agent_id attached."""

@pytest.fixture()
def seed_hive_delta(hive):
    """Factory(key, value, clock=None): INSERT a MemoryDelta row directly into
    hive.db_path — executable seeding, replacing every 'pre-seed' comment from
    prior drafts."""

@pytest.fixture()
def agent_env(keys, hive, enrolled_agent, tmp_path): ...
@pytest.fixture()
def clean_env(monkeypatch): ...          # strips all AGENT_*/HIVE_* vars
@pytest.fixture()
def airgap_preload(tmp_path_factory): ...  # builds memory.sqlite + blocks.json
@pytest.fixture()
def evidence_file(tmp_path): ...           # writes "a=5, b=3"
@pytest.fixture()
def orchestrator_with_adder(...): ...      # MockLLMClient wired per Section 8.3 test
@pytest.fixture()
def orchestrator_with_port_blocks(...): ...
@pytest.fixture()
def full_stack(...): ...                   # live hive + live agent, dynamic ports
@pytest.fixture()
def registry_with_sleeper(...): ...        # block whose child sleeps 10s
@pytest.fixture()
def registry_with_envdump(...): ...        # block whose child returns os.environ keys
@pytest.fixture()
def spawner_env(monkeypatch, tmp_path): ...
```

Port allocation helper (mandatory for anything network-bound):

```python
def free_port() -> int:
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]; s.close()
    return port
```

Acceptance tests use `MockLLMClient` (`LLM_CLIENT=mock`, `TEST_MODE=true`)
everywhere except the live E2E in Section 11.4 step 7, which uses real Ollama.

---

## 13. FINAL INSTRUCTION TO THE CODING AGENT

You have the complete, self-consistent specification. Implement every V1 component.
Do not build, scaffold, or "prepare for" anything in the V2 table.

**Emit `BLOCKED` and stop the affected component if:**
- Any two statements in this document conflict.
- A required library fails to install on Python 3.11.
- The live Ollama API schema differs from `/api/tags` + `/api/generate`.
- You cannot write a test that fails on a stub.
- You believe a schema field is missing (Rule 4).

**BLOCKED escalation:** halt ALL work on the blocked component AND every component
that depends on it. Do not write tests that assume the blocked component exists.
Report as: `BLOCKED: <component> — <reason> — <unresolved dependency>`. Continue
independent components.

**Workflow per component:** (1) run its acceptance test against the empty codebase —
it must fail; if it passes, fix the test first; (2) implement until green;
(3) `make detect` after every component; (4) after all components, run the full
Section 11.4 protocol and report exact output.

`BLOCKED` is always preferable to a stub. A refusal is always preferable to an
invented answer. This applies to you, and it applies to the agent you are building.    Python 3.11 exactly. The spec pins it. If your local default is 3.12+, datetime.utcnow() deprecation noise and other drift will muddy the coding agent's error signals. Use a venv pinned to 3.11.

Ollama first, separately. Pull llama3.2:3b (~2GB) before the build starts, and confirm curl localhost:11434/api/tags responds. If the agent discovers mid-build that Ollama's live API differs from /api/tags + /api/generate, the spec instructs it to emit BLOCKED — you want that resolved before hour one, not during.

spawn context is cross-platform safe. If you're building on Mac or Windows, spawn is already the default — the tests will pass trivially there. The scrubbed-env test only proves something meaningful on Linux, where fork is the default being overridden. Fine for dev, but run the full suite inside the Linux container before calling it done.

sqlite-vec install. It's a loadable extension; on some systems pip install sqlite-vec works clean, on others the bundled SQLite lacks extension loading (macOS system Python notoriously). If the coding agent hits this, correct move is BLOCKED, and your fix is Python.org or Homebrew Python, not a workaround.

Airgap verify needs Docker regardless. make verify-airgap builds the bundled-Ollama image and runs --network none. That step can't be done bare-metal — everything else can, but keep Docker available for steps 5 and 7 of the verification protocol.

And the standing reminder from last turn, since local means you're the only reviewer: when the agent produces /tests/conftest.py, read that diff before any component code. Green means nothing until you've verified the fixtures are honest.
</user_query>
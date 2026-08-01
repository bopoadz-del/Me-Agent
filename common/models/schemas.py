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

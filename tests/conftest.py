"""Section 12 — test harness fixtures (executable code, not comments).

Rules:
- No hardcoded ports or secrets. Keys are session-scoped secrets.token_hex.
- Hive/agent are booted for real when sibling modules exist.
- If hive/agent/foundation are not landed, fixtures fail with BLOCKED — they do
  not stub green against empty logic.
- Declared test seams: MockLLMClient, InProcessBroker, BlockRegistry.register_runner.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac as hmac_mod
import importlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

PRELOAD_STATIC = Path(__file__).resolve().parent / "fixtures" / "preload"
AGENT_UNDER_TEST = "agent-under-test"


# ── Port helper (mandatory) ───────────────────────────────────────────────────

def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ── Declared test seam: in-process broker (Rule 3) ────────────────────────────

class InProcessBroker:
    """Declared Rule-3 test seam #2: in-process broker (not a fourth seam).

    Production counterpart is the live WebSocket path. Under TEST_MODE Hive
    publishes outbound frames to ``app.state.test_broker`` so
    ``HiveHandle.wait_for`` can observe ``block_update`` without faking results.
    """

    def __init__(self) -> None:
        self._queues: dict[str, list[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()
        self._history: list[dict[str, Any]] = []

    async def publish(self, message: dict[str, Any]) -> None:
        msg_type = message.get("msg_type", "")
        async with self._lock:
            self._history.append(message)
            for q in self._queues.get(msg_type, []):
                await q.put(message)
            for q in self._queues.get("*", []):
                await q.put(message)

    def publish_threadsafe(self, loop: asyncio.AbstractEventLoop, message: dict[str, Any]) -> None:
        asyncio.run_coroutine_threadsafe(self.publish(message), loop)

    async def wait_for(self, msg_type: str, timeout: float = 30.0) -> dict[str, Any]:
        q: asyncio.Queue = asyncio.Queue()
        async with self._lock:
            self._queues.setdefault(msg_type, []).append(q)
            for past in self._history:
                if past.get("msg_type") == msg_type:
                    await q.put(past)
        try:
            return await asyncio.wait_for(q.get(), timeout=timeout)
        finally:
            async with self._lock:
                lst = self._queues.get(msg_type, [])
                if q in lst:
                    lst.remove(q)


# ── Import / readiness helpers ────────────────────────────────────────────────

def _blocked(component: str, reason: str, dependency: str) -> None:
    pytest.fail(
        f"BLOCKED: {component} - {reason} - unresolved dependency: {dependency}",
        pytrace=False,
    )


def _import_component(module: str, component: str):
    try:
        mod = importlib.import_module(module)
    except ImportError as exc:
        err = str(exc)
    else:
        return mod
    # Outside except so pytest does not chain ImportError into the failure.
    _blocked(component, f"module {module!r} not importable", err)


def _require_attr(obj: Any, name: str, component: str, hint: str) -> Any:
    if not hasattr(obj, name):
        _blocked(component, f"missing attribute {name!r}", hint)
    return getattr(obj, name)


def _resolve_hive_app(hive_main: Any, *, db_path: Path, jwt_key: str, data_dir: Path):
    """Prefer create_app(...); fall back to module-level ``app`` after env injection."""
    os.environ["HIVE_JWT_KEY"] = jwt_key
    os.environ["DATA_DIR"] = str(data_dir)
    os.environ["HIVE_DB_PATH"] = str(db_path)
    os.environ["TEST_MODE"] = "true"
    os.environ.setdefault("EMBEDDING_DIM", "384")

    if hasattr(hive_main, "create_app"):
        try:
            return hive_main.create_app(
                db_path=str(db_path),
                jwt_key=jwt_key,
                data_dir=str(data_dir),
            )
        except TypeError:
            return hive_main.create_app()

    app = getattr(hive_main, "app", None)
    if app is None:
        _blocked(
            "hive",
            "hive.main exposes neither create_app() nor app",
            "Hive sibling must export FastAPI app",
        )
    return app


def _enroll_via_admin(db_path: Path, jwt_key: str, agent_id: str) -> str:
    """Enroll through the real admin path; return per-agent secret (hex str)."""
    admin = _import_component("hive.admin_cli", "hive")

    for fn_name in ("enroll_agent", "add_agent", "agent_add"):
        fn = getattr(admin, fn_name, None)
        if callable(fn):
            try:
                result = fn(agent_id, db_path=str(db_path), jwt_key=jwt_key)
            except TypeError:
                try:
                    result = fn(agent_id, str(db_path))
                except TypeError:
                    result = fn(agent_id)
            return _normalize_secret(result)

    # CLI entry: python -m hive.admin_cli agent add <id>
    env = os.environ.copy()
    env["HIVE_JWT_KEY"] = jwt_key
    env["HIVE_DB_PATH"] = str(db_path)
    env["DATA_DIR"] = str(db_path.parent)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "hive.admin_cli", "agent", "add", agent_id],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(REPO_ROOT),
            check=False,
        )
    except OSError as exc:
        _blocked("hive", "admin enrollment subprocess failed", str(exc))

    if proc.returncode != 0:
        _blocked(
            "hive",
            "admin enrollment failed",
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}",
        )
    # Expect secret on stdout (last non-empty token/line)
    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    if not lines:
        _blocked("hive", "admin enrollment produced no secret on stdout", proc.stderr)
    secret = lines[-1].split()[-1]
    if len(secret) < 32:
        _blocked("hive", f"admin secret too short: {secret!r}", "hive.admin_cli")
    return secret


def _normalize_secret(result: Any) -> str:
    if isinstance(result, tuple) and len(result) >= 2:
        return str(result[1])
    if isinstance(result, dict):
        for k in ("secret", "per_agent_secret", "agent_secret"):
            if k in result:
                return str(result[k])
    if isinstance(result, str):
        return result
    _blocked("hive", f"unrecognized enrollment return type: {type(result)!r}", "hive.admin_cli")


def _mint_admin_jwt(jwt_key: str) -> str:
    try:
        import jwt
    except ImportError as exc:
        _blocked("foundation", "PyJWT required for admin JWT minting", str(exc))
    now = int(time.time())
    return jwt.encode(
        {"sub": "admin", "role": "admin", "iat": now, "exp": now + 3600},
        jwt_key,
        algorithm="HS256",
    )


def _wait_http_ok(url: str, timeout: float = 15.0) -> None:
    import httpx

    deadline = time.time() + timeout
    last_err: Optional[Exception] = None
    while time.time() < deadline:
        try:
            r = httpx.get(url, timeout=0.5)
            if r.status_code == 200:
                return
        except Exception as exc:  # noqa: BLE001 — poll until up or timeout
            last_err = exc
        time.sleep(0.05)
    _blocked("hive", f"server did not become healthy at {url}", repr(last_err))


# ── Hive handle ───────────────────────────────────────────────────────────────

@dataclass
class HiveHandle:
    url: str
    ws_url: str
    db_path: str
    jwt_key: str
    broker: InProcessBroker
    _server: Any = None
    _thread: Optional[threading.Thread] = None
    _loop: Optional[asyncio.AbstractEventLoop] = None
    _data_dir: Optional[Path] = None

    async def send_command(self, agent_id: str, payload: dict[str, Any]) -> None:
        """Deliver a command to the agent via the real Hive message path."""
        import httpx

        token = _mint_admin_jwt(self.jwt_key)
        body = {"target_agent_id": agent_id, "payload": payload}
        # Prefer command-shaped payload when Hive expects HiveMessage wrapping
        async with httpx.AsyncClient(base_url=self.url, timeout=10.0) as client:
            r = await client.post(
                "/message",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code >= 400:
                # Alternate shape used by some hive drafts
                r2 = await client.post(
                    "/message",
                    json={
                        "target_agent_id": agent_id,
                        "payload": {"action": payload.get("action"), **payload},
                    },
                    headers={"Authorization": f"Bearer {token}"},
                )
                if r2.status_code >= 400:
                    _blocked(
                        "hive",
                        f"POST /message failed ({r.status_code}/{r2.status_code})",
                        r.text + r2.text,
                    )

    async def wait_for(self, msg_type: str, timeout: float = 30.0) -> dict[str, Any]:
        return await self.broker.wait_for(msg_type, timeout=timeout)


def _boot_uvicorn(app: Any, host: str, port: int) -> tuple[Any, threading.Thread]:
    uvicorn = _import_component("uvicorn", "hive")
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)

    def _run() -> None:
        asyncio.set_event_loop(asyncio.new_event_loop())
        server.run()

    thread = threading.Thread(target=_run, name="hive-uvicorn", daemon=True)
    thread.start()
    return server, thread


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def keys():
    """Fresh secrets per session. NEVER read from the repo."""
    import secrets

    return {"jwt": secrets.token_hex(32), "api": secrets.token_hex(32)}


@pytest.fixture()
def hive(keys, tmp_path_factory):
    """Boot the real Hive app via uvicorn on an OS-assigned port (bind 0)."""
    hive_main = _import_component("hive.main", "hive")
    data_dir = tmp_path_factory.mktemp("hive_data")
    db_path = data_dir / "hive.db"

    broker = InProcessBroker()
    app = _resolve_hive_app(
        hive_main, db_path=db_path, jwt_key=keys["jwt"], data_dir=data_dir
    )

    # Attach declared broker seam
    state = getattr(app, "state", None)
    if state is not None:
        state.test_broker = broker

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    host, port = sock.getsockname()
    sock.close()

    server, thread = _boot_uvicorn(app, host, port)
    url = f"http://{host}:{port}"
    ws_url = f"ws://{host}:{port}/sync"
    _wait_http_ok(f"{url}/health")

    handle = HiveHandle(
        url=url,
        ws_url=ws_url,
        db_path=str(db_path),
        jwt_key=keys["jwt"],
        broker=broker,
        _server=server,
        _thread=thread,
        _data_dir=data_dir,
    )
    try:
        yield handle
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        if db_path.exists():
            try:
                db_path.unlink()
            except OSError:
                pass
        if data_dir.exists():
            shutil.rmtree(data_dir, ignore_errors=True)


@pytest.fixture()
def enrolled_agent(hive):
    """Enroll 'agent-under-test' through hive.admin; return (agent_id, secret)."""
    secret = _enroll_via_admin(Path(hive.db_path), hive.jwt_key, AGENT_UNDER_TEST)
    return AGENT_UNDER_TEST, secret


@pytest.fixture()
def authed_ws(hive, enrolled_agent):
    """Factory: challenge-response → open /sync → JWT auth frame."""
    import httpx
    import websockets

    agent_id, secret = enrolled_agent

    async def _factory():
        async with httpx.AsyncClient(base_url=hive.url, timeout=10.0) as client:
            nonce = (
                await client.get("/auth/challenge", params={"agent_id": agent_id})
            ).json()["nonce"]
            proof = hmac_mod.new(
                secret.encode(), nonce.encode(), hashlib.sha256
            ).hexdigest()
            r = await client.post(
                "/auth/prove",
                json={"agent_id": agent_id, "nonce": nonce, "proof": proof},
            )
            if r.status_code != 200:
                _blocked(
                    "hive",
                    f"challenge-response failed ({r.status_code})",
                    r.text,
                )
            token = r.json()["session_token"]

        ws = await websockets.connect(hive.ws_url)
        await ws.send(json.dumps({"auth": token}))
        # Attach agent_id for acceptance tests (ws.agent_id)
        ws.agent_id = agent_id  # type: ignore[attr-defined]
        ws.session_token = token  # type: ignore[attr-defined]
        return ws

    return _factory


@pytest.fixture()
def seed_hive_delta(hive):
    """Factory(key, value, clock=None): INSERT MemoryDelta into hive.db_path."""

    def _seed(
        key: str,
        value: Optional[dict[str, Any]] = None,
        clock: Optional[dict[str, int]] = None,
    ) -> str:
        if clock is None:
            con = sqlite3.connect(hive.db_path)
            try:
                row = con.execute(
                    "SELECT COUNT(*) FROM memory_deltas WHERE origin='hive'"
                ).fetchone()
                n = int(row[0]) + 1 if row else 1
            except sqlite3.Error:
                n = 1
            finally:
                con.close()
            clock = {"hive": n}

        delta_id = str(uuid.uuid4())
        ts = datetime.now(timezone.utc).isoformat()
        con = sqlite3.connect(hive.db_path)
        try:
            con.execute(
                """
                INSERT INTO memory_deltas
                    (delta_id, key, value, origin, vector_clock, operation, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    delta_id,
                    key,
                    json.dumps(value) if value is not None else None,
                    "hive",
                    json.dumps(clock),
                    "upsert",
                    ts,
                ),
            )
            con.commit()
        except sqlite3.Error as exc:
            _blocked(
                "hive",
                "seed_hive_delta INSERT failed — memory_deltas schema missing?",
                str(exc),
            )
        finally:
            con.close()
        return delta_id

    return _seed


@pytest.fixture()
def hive_with_deltas(hive, seed_hive_delta):
    """Live Hive pre-seeded with 3 memory deltas (bootstrapper acceptance)."""
    for i in range(3):
        seed_hive_delta(key=f"boot{i}", value={"v": i}, clock={"hive": i + 1})
    return hive


@pytest.fixture()
def agent_env(keys, hive, enrolled_agent, tmp_path):
    """Full valid online-agent environment dict (caller applies via update)."""
    agent_id, secret = enrolled_agent
    return {
        "AGENT_ID": agent_id,
        "AGENT_SECRET": secret,
        "AGENT_API_TOKEN": keys["api"],
        "HIVE_URL": hive.url,
        "DATA_DIR": str(tmp_path),
        "TEST_MODE": "true",
        "LLM_CLIENT": "mock",
        "AIRGAP": "false",
    }


@pytest.fixture()
def clean_env(monkeypatch):
    """Strip all AGENT_*/HIVE_* vars (and common test toggles)."""
    for key in list(os.environ):
        if key.startswith("AGENT_") or key.startswith("HIVE_"):
            monkeypatch.delenv(key, raising=False)
    for key in (
        "LLM_CLIENT",
        "AIRGAP",
        "TEST_MODE",
        "DATA_DIR",
        "PRELOAD_DIR",
        "OLLAMA_URL",
        "SKIP_AIRGAP_VERIFY",
        "EMBEDDING_DIM",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def airgap_preload(tmp_path_factory):
    """Build memory.sqlite + blocks.json in a temp preload dir; return path str."""
    from tests.fixtures.preload.build_memory_sqlite import build_memory_sqlite

    dest = tmp_path_factory.mktemp("airgap_preload")
    build_memory_sqlite(dest / "memory.sqlite")

    src_blocks = PRELOAD_STATIC / "blocks.json"
    if src_blocks.is_file():
        shutil.copy(src_blocks, dest / "blocks.json")
    else:
        (dest / "blocks.json").write_text("[]", encoding="utf-8")
    return str(dest)


@pytest.fixture()
def evidence_file(tmp_path):
    """Write evidence containing a=5, b=3; return path str."""
    path = tmp_path / "evidence.txt"
    path.write_text("a=5, b=3", encoding="utf-8")
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir(exist_ok=True)
    shutil.copy(path, evidence_dir / "evidence.txt")
    return str(path)


def _adder_block():
    schemas = _import_component("common.models.schemas", "foundation")
    BlockDef = schemas.BlockDef
    return BlockDef(
        block_id="adder",
        name="adder",
        domain="math",
        input_schema={
            "type": "object",
            "properties": {
                "a": {"type": "integer"},
                "b": {"type": "integer"},
            },
            "required": ["a", "b"],
        },
        output_json_schema={
            "type": "object",
            "properties": {"sum": {"type": "integer"}},
            "required": ["sum"],
        },
        system_prompt_template=(
            "Add integers a and b from evidence. Return JSON {\"sum\": a+b} "
            "and cite the evidence excerpt."
        ),
        tools=["file_reader"],
        evidence_standard=None,
        dependencies=[],
        version_clock=1,
    )


def _port_blocks_with_evidence_standard():
    schemas = _import_component("common.models.schemas", "foundation")
    BlockDef = schemas.BlockDef
    return [
        BlockDef(
            block_id="port_terminal_berth_fee",
            name="berth_fee",
            domain="port_terminal",
            input_schema={
                "type": "object",
                "properties": {
                    "dwell_hours": {"type": "number"},
                    "base_rate_usd_per_hour": {"type": "number"},
                },
                "required": ["dwell_hours", "base_rate_usd_per_hour"],
            },
            output_json_schema={
                "type": "object",
                "properties": {"berth_fee_usd": {"type": "number"}},
                "required": ["berth_fee_usd"],
            },
            system_prompt_template="Compute berth fee from evidence.",
            tools=["calculator", "file_reader"],
            evidence_standard=(
                "dwell_hours and base_rate_usd_per_hour must be grounded in "
                "attached evidence excerpts before returning a fee."
            ),
            dependencies=[],
            version_clock=1,
        )
    ]


def _as_registry(blocks: list[Any]):
    """Wrap BlockDefs in Agent BlockRegistry when available; else dict."""
    for mod_name in (
        "agent.swarm.registry",
        "agent.orchestrator.registry",
        "agent.blocks",
    ):
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:
            continue
        for cls_name in ("BlockRegistry", "Registry"):
            cls = getattr(mod, cls_name, None)
            if cls is None:
                continue
            reg = cls()
            for b in blocks:
                if hasattr(reg, "register"):
                    reg.register(b)
                elif hasattr(reg, "add"):
                    reg.add(b)
                else:
                    reg[b.block_id] = b  # type: ignore[index]
            return reg
    return {b.block_id: b for b in blocks}


def _mock_llm_for_adder():
    llm_mod = _import_component("common.models.llm_client", "foundation")
    MockLLMClient = _require_attr(
        llm_mod, "MockLLMClient", "foundation", "common.models.llm_client.MockLLMClient"
    )

    def _handler(prompt: str, system: str, format: str = "json") -> dict:
        # Deterministic: parse a/b from prompt/evidence text if present
        import re

        nums = [int(x) for x in re.findall(r"\b(\d+)\b", prompt + " " + system)]
        a = 5
        b = 3
        if "a=" in prompt or "a=" in system:
            m = re.search(r"a\s*=\s*(\d+)", prompt + " " + system)
            if m:
                a = int(m.group(1))
            m = re.search(r"b\s*=\s*(\d+)", prompt + " " + system)
            if m:
                b = int(m.group(1))
        elif len(nums) >= 2:
            a, b = nums[0], nums[1]
        return {
            "sum": a + b,
            "citations": [
                {
                    "source": "evidence",
                    "excerpt": f"a={a}, b={b}",
                    "confidence": 1.0,
                }
            ],
        }

    try:
        return MockLLMClient(handler=_handler)
    except TypeError:
        try:
            return MockLLMClient(responses={"adder": _handler, "default": _handler})
        except TypeError:
            client = MockLLMClient()
            if hasattr(client, "configure"):
                client.configure(_handler)
                return client
            if hasattr(client, "set_handler"):
                client.set_handler(_handler)
                return client
            _blocked(
                "foundation",
                "MockLLMClient constructor/configure seam unrecognized",
                "common.models.llm_client.MockLLMClient",
            )


def _build_orchestrator(registry, llm_client, tmp_path: Path):
    engine_mod = _import_component("agent.orchestrator.engine", "agent")
    HeadlessOrchestrator = _require_attr(
        engine_mod,
        "HeadlessOrchestrator",
        "agent",
        "agent.orchestrator.engine.HeadlessOrchestrator",
    )
    spawner_mod = _import_component("agent.swarm.spawner", "agent")
    SubAgentSpawner = _require_attr(
        spawner_mod, "SubAgentSpawner", "agent", "agent.swarm.spawner.SubAgentSpawner"
    )

    # Optional collaborators — fail clearly if constructor requires them
    retriever = None
    store = None
    try:
        mem = importlib.import_module("agent.memory")
        if hasattr(mem, "LocalStore"):
            store = mem.LocalStore(str(tmp_path / "agent.db"))
        if hasattr(mem, "Retriever"):
            retriever = mem.Retriever(store) if store is not None else mem.Retriever()
    except ImportError:
        pass

    spawner = SubAgentSpawner(registry)
    if hasattr(spawner, "llm_client"):
        spawner.llm_client = llm_client
    elif hasattr(spawner, "set_llm"):
        spawner.set_llm(llm_client)

    try:
        return HeadlessOrchestrator(registry, retriever, spawner, store)
    except TypeError:
        try:
            return HeadlessOrchestrator(
                registry=registry,
                retriever=retriever,
                spawner=spawner,
                store=store,
                llm_client=llm_client,
            )
        except TypeError as exc:
            _blocked(
                "agent",
                "HeadlessOrchestrator signature mismatch",
                str(exc),
            )


@pytest.fixture()
def orchestrator_with_adder(tmp_path, monkeypatch, evidence_file):
    """HeadlessOrchestrator + adder block + MockLLMClient (Section 8.3 E2E)."""
    monkeypatch.setenv("TEST_MODE", "true")
    monkeypatch.setenv("LLM_CLIENT", "mock")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    registry = _as_registry([_adder_block()])
    llm = _mock_llm_for_adder()
    return _build_orchestrator(registry, llm, tmp_path)


@pytest.fixture()
def orchestrator_with_port_blocks(tmp_path, monkeypatch):
    """Orchestrator with port_terminal blocks that require evidence."""
    monkeypatch.setenv("TEST_MODE", "true")
    monkeypatch.setenv("LLM_CLIENT", "mock")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    registry = _as_registry(_port_blocks_with_evidence_standard())
    llm_mod = _import_component("common.models.llm_client", "foundation")
    MockLLMClient = _require_attr(
        llm_mod, "MockLLMClient", "foundation", "common.models.llm_client.MockLLMClient"
    )
    try:
        llm = MockLLMClient(handler=lambda *a, **k: {"berth_fee_usd": 0})
    except TypeError:
        llm = MockLLMClient()
    return _build_orchestrator(registry, llm, tmp_path)


@pytest.fixture()
def adder_profile(evidence_file):
    schemas = _import_component("common.models.schemas", "foundation")
    MissionProfile = schemas.MissionProfile
    return MissionProfile(
        natural_language="add the two numbers in the evidence",
        domain_hint="math",
        attached_evidence=[{"type": "txt", "path": evidence_file}],
    )


@pytest.fixture()
def spawner_env(monkeypatch, tmp_path):
    """Environment required by SubAgentSpawner isolation tests."""
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TEST_MODE", "true")
    monkeypatch.setenv("AIRGAP", "false")
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    return {"data_dir": str(tmp_path), "evidence_dir": str(evidence)}


def _block(block_id: str, **kwargs):
    schemas = _import_component("common.models.schemas", "foundation")
    BlockDef = schemas.BlockDef
    base = dict(
        block_id=block_id,
        name=block_id,
        domain="test",
        input_schema={"type": "object", "properties": {}},
        output_json_schema={"type": "object"},
        system_prompt_template=f"test harness block {block_id}",
        tools=[],
        version_clock=1,
    )
    base.update(kwargs)
    return BlockDef(**base)


@pytest.fixture()
def registry_with_sleeper(monkeypatch):
    """Block whose child sleeps 10s (timeout acceptance)."""
    monkeypatch.setenv("TEST_MODE", "true")
    block = _block(
        "sleeper",
        output_json_schema={
            "type": "object",
            "properties": {"pid": {"type": "integer"}, "reason": {"type": "string"}},
            "required": ["pid"],
        },
        system_prompt_template=(
            "TEST_HARNESS_SLEEPER: sleep 10 seconds in the child process, "
            "then return {\"pid\": <os.getpid()>}."
        ),
    )
    reg = _as_registry([block])
    # Optional runner seam if Agent Runtime exposes it
    _try_attach_test_runner(reg, "sleeper", _runner_sleeper)
    return reg


@pytest.fixture()
def registry_with_envdump(monkeypatch):
    """Block whose child returns os.environ key list."""
    monkeypatch.setenv("TEST_MODE", "true")
    block = _block(
        "envdump",
        output_json_schema={
            "type": "object",
            "properties": {"env": {"type": "array"}},
            "required": ["env"],
        },
        system_prompt_template=(
            "TEST_HARNESS_ENVDUMP: return {\"env\": list(os.environ.keys())}."
        ),
    )
    reg = _as_registry([block])
    _try_attach_test_runner(reg, "envdump", _runner_envdump)
    return reg


@pytest.fixture()
def registry_with_searcher(monkeypatch):
    """Block that invokes web_search (airgap must fail)."""
    monkeypatch.setenv("TEST_MODE", "true")
    block = _block(
        "searcher",
        tools=["web_search"],
        output_json_schema={
            "type": "object",
            "properties": {"results": {"type": "array"}, "reason": {"type": "string"}},
        },
        system_prompt_template=(
            "TEST_HARNESS_SEARCHER: call web_search tool and return results."
        ),
    )
    reg = _as_registry([block])
    _try_attach_test_runner(reg, "searcher", _runner_searcher)
    return reg


def _runner_sleeper(_step, _ctx, _tools):
    import time

    time.sleep(10)
    return {"pid": os.getpid()}


def _runner_envdump(_step, _ctx, _tools):
    return {"env": list(os.environ.keys())}


def _runner_searcher(_step, _ctx, _tools):
    from agent.swarm.tools import web_search

    return {"results": web_search("test")}


def _try_attach_test_runner(registry: Any, block_id: str, runner: Callable) -> None:
    if hasattr(registry, "register_runner"):
        registry.register_runner(block_id, runner)
    elif hasattr(registry, "set_runner"):
        registry.set_runner(block_id, runner)
    elif isinstance(registry, dict):
        entry = registry.get(block_id)
        if entry is not None and hasattr(entry, "__dict__"):
            try:
                setattr(entry, "test_runner", runner)
            except Exception:  # noqa: BLE001
                pass


@pytest.fixture()
def full_stack(hive, enrolled_agent, keys, tmp_path_factory):
    """Live Hive + live agent (MockLLM adder) on dynamic ports."""
    _import_component("agent.bootstrapper", "agent")
    agent_id, secret = enrolled_agent
    data_dir = tmp_path_factory.mktemp("agent_data")
    agent_port = free_port()

    env = {
        **os.environ,
        "AGENT_ID": agent_id,
        "AGENT_SECRET": secret,
        "AGENT_API_TOKEN": keys["api"],
        "HIVE_URL": hive.url,
        "DATA_DIR": str(data_dir),
        "TEST_MODE": "true",
        "LLM_CLIENT": "mock",
        "AIRGAP": "false",
        "AGENT_API_PORT": str(agent_port),
        "PORT": str(agent_port),
    }

    # Register adder on hive so agent pulls it at auth
    _register_block_on_hive(hive, _adder_block())

    proc = subprocess.Popen(
        [sys.executable, "-m", "agent.bootstrapper"],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    agent_url = f"http://127.0.0.1:{agent_port}"
    try:
        _wait_http_ok(f"{agent_url}/health", timeout=30.0)
    except Exception:
        out = ""
        if proc.stdout:
            try:
                out = proc.stdout.read(8000).decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                out = ""
        proc.kill()
        _blocked(
            "agent",
            "full_stack agent failed to become healthy",
            out or f"exit={proc.poll()}",
        )

    stack = SimpleNamespace(
        hive=hive,
        agent_id=agent_id,
        agent_secret=secret,
        agent_url=agent_url,
        agent_port=agent_port,
        api_token=keys["api"],
        data_dir=str(data_dir),
        process=proc,
    )
    try:
        yield stack
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(data_dir, ignore_errors=True)


def _register_block_on_hive(hive: HiveHandle, block: Any) -> None:
    import httpx

    token = _mint_admin_jwt(hive.jwt_key)
    payload = block.model_dump() if hasattr(block, "model_dump") else block
    try:
        r = httpx.post(
            f"{hive.url}/admin/blocks",
            json=[payload],
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        )
    except Exception as exc:  # noqa: BLE001 — surface transport failures
        _blocked("hive", "POST /admin/blocks transport failed", repr(exc))
    if r.status_code >= 400:
        _blocked(
            "hive",
            f"POST /admin/blocks failed ({r.status_code})",
            r.text,
        )


@pytest.fixture()
def agent_api(keys, tmp_path_factory):
    """Live agent mission API on a dynamic port (Section 5.4 acceptance).

    Boots real agent bootstrapper/API. Fails BLOCKED if agent sibling missing.
    """
    _import_component("agent.bootstrapper", "agent")
    data_dir = tmp_path_factory.mktemp("agent_api_data")
    port = free_port()
    # Offline-ish: agent may require hive — prefer AIRGAP boot for API-only tests
    # when hive not needed; mission API auth test only hits /mission and /health.
    env = {
        **os.environ,
        "AGENT_ID": "agent-api-test",
        "AGENT_API_TOKEN": keys["api"],
        "DATA_DIR": str(data_dir),
        "TEST_MODE": "true",
        "LLM_CLIENT": "mock",
        "AIRGAP": "true",
        "PRELOAD_DIR": str(PRELOAD_STATIC),
        "SKIP_AIRGAP_VERIFY": "true",
        "AGENT_API_PORT": str(port),
        "PORT": str(port),
    }
    # Ensure preload sqlite exists for airgap branch
    from tests.fixtures.preload.build_memory_sqlite import build_memory_sqlite

    if not (PRELOAD_STATIC / "memory.sqlite").is_file():
        build_memory_sqlite(PRELOAD_STATIC / "memory.sqlite")

    proc = subprocess.Popen(
        [sys.executable, "-m", "agent.bootstrapper"],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        _wait_http_ok(f"{url}/health", timeout=30.0)
    except Exception:
        out = ""
        if proc.stdout:
            try:
                out = proc.stdout.read(8000).decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                out = ""
        proc.kill()
        _blocked("agent", "agent_api failed to become healthy", out or f"exit={proc.poll()}")

    handle = SimpleNamespace(url=url, port=port, api_token=keys["api"], process=proc)
    try:
        yield handle
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(data_dir, ignore_errors=True)

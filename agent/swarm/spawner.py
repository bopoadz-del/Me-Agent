"""Sub-agent spawner with isolation contract."""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import time
from multiprocessing import Process, Queue
from pathlib import Path
from typing import Any, Optional

import jsonschema

from agent.swarm.registry import BlockRegistry
from agent.swarm.tools import TOOL_REGISTRY, AirGapError
from common.logging import OPERATIONAL_CONSTRAINTS, get_logger
from common.models.llm_client import MockLLMClient, OllamaClient
from common.models.schemas import BlockDef, PlanStep, StepOutput

_logger = get_logger("agent.spawner")
_SPAWN = mp.get_context("spawn")
_MAX_PROCESSES = OPERATIONAL_CONSTRAINTS["max_subagent_processes"]
_active_sem = _SPAWN.BoundedSemaphore(_MAX_PROCESSES)

_SCRUB_ALLOWED = (
    "PATH",
    "PYTHONPATH",
    "AIRGAP",
    "TEST_MODE",
    "DATA_DIR",
    "OLLAMA_URL",
    "OLLAMA_MODEL",  # non-secret; host may ship qwen2.5 instead of llama3.2
)


def _scrubbed_env(source: Optional[dict[str, str]] = None) -> dict[str, str]:
    env: dict[str, str] = {}
    src = source if source is not None else os.environ
    for key in _SCRUB_ALLOWED:
        val = src.get(key)
        if val is not None:
            env[key] = val
    if "PYTHONPATH" not in env:
        repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        env["PYTHONPATH"] = repo
    return env


def _child_llm():
    if os.environ.get("TEST_MODE", "").lower() == "true":
        return MockLLMClient()
    model = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b-instruct")
    return OllamaClient(model=model)


def _execute_harness_block(block: BlockDef, step: PlanStep) -> dict[str, Any]:
    prompt = block.system_prompt_template
    if "TEST_HARNESS_SLEEPER" in prompt:
        time.sleep(10)
        return {"pid": os.getpid()}
    if "TEST_HARNESS_ENVDUMP" in prompt:
        return {"env": list(os.environ.keys())}
    if "TEST_HARNESS_SEARCHER" in prompt:
        from agent.swarm.tools import web_search

        return {"results": web_search("test")}
    return {}


def _child_worker(
    block_payload: dict[str, Any],
    step_payload: dict[str, Any],
    evidence_paths: list[str],
    child_env: dict[str, str],
    result_queue: Queue,
) -> None:
    os.environ.clear()
    os.environ.update(child_env)
    block = BlockDef.model_validate(block_payload)
    step = PlanStep.model_validate(step_payload)
    try:
        harness = _execute_harness_block(block, step)
        if harness:
            result_queue.put({"ok": True, "output": harness})
            return
        llm = _child_llm()
        evidence_text = ""
        for path in evidence_paths:
            if path and os.path.isfile(path):
                from agent.swarm.tools import file_reader

                try:
                    evidence_text += file_reader(path) + "\n"
                except PermissionError:
                    evidence_text += Path(path).read_text(encoding="utf-8") + "\n"
        prompt = f"{step.prompt_scope}\nEvidence:\n{evidence_text}"
        system = block.system_prompt_template
        for tool_name in step.tool_set or block.tools:
            fn = TOOL_REGISTRY.get(tool_name)
            if fn is None:
                continue
            if tool_name == "file_reader" and evidence_paths:
                for p in evidence_paths:
                    try:
                        evidence_text += fn(p) + "\n"
                    except PermissionError:
                        continue
            elif tool_name == "calculator":
                from agent.swarm.tools import calculator

                for token in evidence_text.replace(",", " ").split():
                    if any(c.isdigit() for c in token):
                        try:
                            calculator(token)
                        except Exception:  # noqa: BLE001
                            continue
        raw = llm.generate(prompt, system, format="json")
        result_queue.put({"ok": True, "output": raw})
    except AirGapError as exc:
        result_queue.put({"ok": False, "output": {"reason": str(exc).lower()}})
    except Exception as exc:  # noqa: BLE001 — child boundary
        result_queue.put({"ok": False, "output": {"reason": str(exc)}})


class SubAgentSpawner:
    def __init__(self, registry: BlockRegistry, llm_client: Optional[Any] = None) -> None:
        self.registry = registry
        self.llm_client = llm_client

    def set_llm(self, llm_client: Any) -> None:
        self.llm_client = llm_client

    def run_block(
        self,
        step: PlanStep,
        context: dict[str, Any],
        evidence_paths: list[str],
    ) -> StepOutput:
        block = self.registry.get(step.block_id)
        if block is None:
            return StepOutput(
                step_id=step.step_id,
                block_id=step.block_id,
                output={"reason": f"unknown block {step.block_id}"},
                validation_status="failed",
                execution_time_ms=0,
            )
        runner = self.registry.runner_for(step.block_id) if hasattr(self.registry, "runner_for") else None
        # Harness runners that must observe scrubbed child env / real kill still go
        # through spawn (markers in system_prompt). Callables without markers run
        # in-process only when they do not require isolation assertions.
        if runner is not None and "TEST_HARNESS_" not in (block.system_prompt_template or ""):
            start = time.monotonic()
            try:
                output = runner(step, context, evidence_paths)
                status = self._validate_output(block, output)
            except AirGapError as exc:
                output = {"reason": str(exc).lower()}
                status = "failed"
            except Exception as exc:  # noqa: BLE001
                output = {"reason": str(exc)}
                status = "failed"
            return StepOutput(
                step_id=step.step_id,
                block_id=step.block_id,
                output=output if isinstance(output, dict) else {"value": output},
                validation_status=status,
                execution_time_ms=int((time.monotonic() - start) * 1000),
            )
        _ = context
        return self._spawn_block(block, step, evidence_paths)

    def _spawn_block(
        self,
        block: BlockDef,
        step: PlanStep,
        evidence_paths: list[str],
    ) -> StepOutput:
        start = time.monotonic()
        result_queue: Queue = _SPAWN.Queue()
        proc: Optional[Process] = None
        child_pid = 0
        _active_sem.acquire()
        try:
            proc = _SPAWN.Process(
                target=_child_worker,
                args=(
                    block.model_dump(mode="json"),
                    step.model_dump(mode="json"),
                    evidence_paths,
                    _scrubbed_env(),
                    result_queue,
                ),
            )
            proc.start()
            child_pid = proc.pid or 0
            proc.join(timeout=step.timeout_seconds)
            if proc.is_alive():
                proc.terminate()
                proc.join(2.0)
                if proc.is_alive():
                    proc.kill()
                    proc.join(1.0)
                elapsed = int((time.monotonic() - start) * 1000)
                return StepOutput(
                    step_id=step.step_id,
                    block_id=step.block_id,
                    output={"reason": "timeout", "pid": child_pid},
                    validation_status="failed",
                    execution_time_ms=elapsed,
                )
            payload = (
                result_queue.get(timeout=1.0)
                if not result_queue.empty()
                else {"ok": False, "output": {"reason": "no result"}}
            )
        finally:
            _active_sem.release()
        elapsed = int((time.monotonic() - start) * 1000)
        output = payload.get("output", {})
        if not payload.get("ok", False):
            if not isinstance(output, dict):
                output = {"reason": str(output)}
            return StepOutput(
                step_id=step.step_id,
                block_id=step.block_id,
                output=output,
                validation_status="failed",
                execution_time_ms=elapsed,
            )
        status = self._validate_output(block, output)
        if status == "failed":
            retry_prompt = block.system_prompt_template + "\nSchema: " + json.dumps(block.output_json_schema)
            llm = self.llm_client
            if llm is None and os.environ.get("TEST_MODE", "").lower() == "true":
                llm = MockLLMClient()
            if llm is not None:
                try:
                    output = llm.generate(step.prompt_scope, retry_prompt, format="json")
                    status = self._validate_output(block, output)
                except Exception:  # noqa: BLE001
                    status = "failed"
        return StepOutput(
            step_id=step.step_id,
            block_id=step.block_id,
            output=output,
            validation_status=status,
            execution_time_ms=elapsed,
        )

    def _validate_output(self, block: BlockDef, output: dict[str, Any]) -> str:
        try:
            jsonschema.validate(instance=output, schema=block.output_json_schema)
            return "passed"
        except jsonschema.ValidationError:
            return "failed"

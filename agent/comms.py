"""Comms link — hive commands and mission completion reporting."""
from __future__ import annotations

import os
import threading
from collections import deque
from typing import Any, Callable, Optional

from common.logging import OPERATIONAL_CONSTRAINTS, get_logger
from common.models.schemas import ExecutionResult, MissionProfile

_logger = get_logger("agent.comms")


def _mission_queue_max_depth() -> int:
    raw = os.environ.get("MISSION_QUEUE_MAX_DEPTH")
    if raw is not None and str(raw).strip() != "":
        return max(0, int(raw))
    return int(OPERATIONAL_CONSTRAINTS["mission_queue_max_depth"])


class CommsLink:
    def __init__(
        self,
        orchestrator: Any,
        publish_fn: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> None:
        self.orchestrator = orchestrator
        self.publish_fn = publish_fn
        self._max_depth = _mission_queue_max_depth()
        # Manual capacity check — refuse rather than silently drop (deque maxlen).
        self.queue: deque[MissionProfile] = deque()
        self._lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._process_loop, name="comms-worker", daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()
        if self._worker:
            self._worker.join(timeout=5.0)

    def enqueue(self, profile: MissionProfile) -> bool:
        with self._lock:
            if len(self.queue) >= self._max_depth:
                return False
            self.queue.append(profile)
            return True

    def queue_depth(self) -> int:
        with self._lock:
            return len(self.queue)

    def max_depth(self) -> int:
        return self._max_depth

    def handle_command(self, payload: dict[str, Any]) -> None:
        action = payload.get("action")
        if action == "run_mission":
            raw = payload.get("profile") or payload
            profile = MissionProfile.model_validate(raw)
            if not self.enqueue(profile):
                _logger.warning("mission queue saturated")
                return
        elif action == "status":
            status = {"queue_depth": self.queue_depth()}
            self._publish({"msg_type": "ack", "payload": status})
        elif action == "resume":
            _logger.info(f"resume notification: {payload}")

    def _process_loop(self) -> None:
        while not self._stop.is_set():
            profile: Optional[MissionProfile] = None
            with self._lock:
                if self.queue:
                    profile = self.queue.popleft()
            if profile is None:
                self._stop.wait(0.1)
                continue
            plan = self.orchestrator.resolve(profile)
            result = self.orchestrator.execute(plan)
            self._persist_result(result)
            self._emit_block_update(result)

    def _persist_result(self, result: ExecutionResult) -> None:
        """Store the terminal result so the agent API can report it (Section 11.4 step 7).

        Publishing to the Hive alone left the agent unable to answer for its own
        missions. A store that cannot persist must not kill the worker thread.
        """
        store = getattr(self.orchestrator, "store", None)
        saver = getattr(store, "save_mission_result", None)
        if saver is None:
            return
        try:
            saver(result)
        except Exception as exc:  # noqa: BLE001 - worker must survive a bad write
            _logger.error(f"failed to persist mission result {result.mission_id}: {exc}")

    def _emit_block_update(self, result: ExecutionResult) -> None:
        # Payload MUST be a full ExecutionResult dump (schemas.py) — not a blocks registry push.
        payload = result.model_dump(mode="json")
        message = {
            "msg_type": "block_update",
            "session_id": "-",
            "payload": {
                "mission_id": payload["mission_id"],
                "status": payload["status"],
                "outputs": payload["outputs"],
                "citations": payload["citations"],
                "confidence": payload["confidence"],
            },
            "vector_clock": {},
        }
        self._publish(message)

    def _publish(self, message: dict[str, Any]) -> None:
        if self.publish_fn is not None:
            self.publish_fn(message)

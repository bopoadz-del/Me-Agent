"""Structured JSON logging factory (spec Section 9.1 / 10)."""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

from common.models.schemas import LogEntry

OPERATIONAL_CONSTRAINTS = {
    "max_disk_gb": 10,
    "log_rotate_mb": 100,
    "log_backups": 5,
    "solo_mode_threshold_days": 7,
    "max_subagent_processes": 8,
    "subagent_default_timeout_sec": 60,
    "mission_queue_max_depth": 100,
}


class _JsonFormatter(logging.Formatter):
    def __init__(self, component: str) -> None:
        super().__init__()
        self.component = component

    def format(self, record: logging.LogRecord) -> str:
        level = record.levelname
        if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            level = "INFO"
        trace_id = getattr(record, "trace_id", "-")
        metrics = getattr(record, "metrics", None)
        entry = LogEntry(
            level=level,  # type: ignore[arg-type]
            component=self.component,
            trace_id=str(trace_id),
            message=record.getMessage(),
            metrics=metrics,
        )
        return entry.model_dump_json()


def get_logger(component: str) -> logging.Logger:
    """Return a logger emitting one JSON LogEntry line per event."""
    logger = logging.getLogger(f"self-agent.{component}")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    data_dir = os.environ.get("DATA_DIR", "/data")
    log_dir = Path(data_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    rotate_mb = int(os.environ.get("LOG_ROTATE_MB", OPERATIONAL_CONSTRAINTS["log_rotate_mb"]))
    backups = int(os.environ.get("LOG_BACKUPS", OPERATIONAL_CONSTRAINTS["log_backups"]))
    handler = RotatingFileHandler(
        log_dir / f"{component.replace('.', '_')}.log",
        maxBytes=max(1024, rotate_mb * 1024),
        backupCount=backups,
        encoding="utf-8",
    )
    handler.setFormatter(_JsonFormatter(component))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def log_event(
    logger: logging.Logger,
    level: str,
    message: str,
    *,
    trace_id: str = "-",
    metrics: Optional[dict[str, float]] = None,
) -> None:
    extra: dict[str, Any] = {"trace_id": trace_id}
    if metrics is not None:
        extra["metrics"] = metrics
    logger.log(getattr(logging, level.upper(), logging.INFO), message, extra=extra)

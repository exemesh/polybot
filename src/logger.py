"""Structured logging. Writes human logs to bot.log and JSONL events elsewhere."""
from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict


def _ensure_dir(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def setup(log_level: str, main_log_path: str, error_log_path: str) -> logging.Logger:
    """Set up the root polybot logger with console + file handlers."""
    _ensure_dir(main_log_path)
    _ensure_dir(error_log_path)

    logger = logging.getLogger("polybot")
    logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    logger.propagate = False

    if logger.handlers:  # idempotent
        return logger

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    main_fh = logging.FileHandler(main_log_path)
    main_fh.setFormatter(fmt)
    logger.addHandler(main_fh)

    error_fh = logging.FileHandler(error_log_path)
    error_fh.setLevel(logging.WARNING)
    error_fh.setFormatter(fmt)
    logger.addHandler(error_fh)

    return logger


class JsonlWriter:
    """Append-only structured-event writer (one JSON object per line)."""

    def __init__(self, path: str) -> None:
        _ensure_dir(path)
        self.path = path

    def write(self, event_type: str, payload: Dict[str, Any]) -> None:
        record = {"ts": time.time(), "event": event_type, **payload}
        with open(self.path, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")

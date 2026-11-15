"""Structured logging: one JSON object per line on stdout.

Usage:  log.event("source_summary", company="jane-street", fetched=41, matched=3)
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

_logger = logging.getLogger("jobtracker")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="seconds"),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
            **getattr(record, "fields", {}),
        }
        return json.dumps(entry, default=str, ensure_ascii=False)


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    _logger.handlers = [handler]
    _logger.setLevel(logging.INFO)
    _logger.propagate = False
    # httpx logs every request URL at INFO; we log our own per-source summaries instead.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def event(name: str, level: int = logging.INFO, **fields: Any) -> None:
    _logger.log(level, name, extra={"fields": fields})


def warning(name: str, **fields: Any) -> None:
    event(name, logging.WARNING, **fields)

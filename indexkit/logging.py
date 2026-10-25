"""Structured logging: one JSON object per line with --log-format json, readable text otherwise."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

_STANDARD = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        payload.update({k: v for k, v in vars(record).items() if k not in _STANDARD})
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        extra = {k: v for k, v in vars(record).items() if k not in _STANDARD}
        suffix = " " + " ".join(f"{k}={v}" for k, v in extra.items()) if extra else ""
        return f"{record.levelname.lower()}: {record.getMessage()}{suffix}"


def configure(json_output: bool = False, verbose: bool = False) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if json_output else TextFormatter())
    root = logging.getLogger("indexkit")
    root.handlers = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.propagate = False
    logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx logs full URLs, which can contain API keys

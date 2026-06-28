"""Safe event logging for QLA."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any


_SECRET_WORDS = ("PRIVATE_KEY", "POLY_API_KEY", "POLY_API_SECRET", "POLY_PASSPHRASE", "SECRET", "KEY")


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            if any(secret in str(key).upper() for secret in _SECRET_WORDS):
                clean[key] = "[REDACTED]"
            else:
                clean[key] = _sanitize(item)
        return clean
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


def get_logger(name: str = "qla") -> logging.Logger:
    Path("logs").mkdir(exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not logger.handlers:
        handler = logging.FileHandler("logs/qla_events.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)

    return logger


def log_event(event: str, **fields: Any) -> None:
    logger = get_logger()
    logger.info("%s %s", event, _sanitize(fields))

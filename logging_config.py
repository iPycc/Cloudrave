"""Logging setup with colorized CREATE and DELETE operations."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from config import CloudraveConfig


RESET = "\033[0m"
BLUE = "\033[34m"
RED = "\033[31m"
GRAY = "\033[90m"
GREEN = "\033[32m"
YELLOW = "\033[33m"


class CloudraveFormatter(logging.Formatter):
    """Formatter that colors CREATE and DELETE audit lines."""

    def __init__(self, use_color: bool) -> None:
        super().__init__()
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        request_id = getattr(record, "request_id", "-")
        ip = getattr(record, "ip", "-")
        operation = getattr(record, "operation", "")
        actor = getattr(record, "actor", "-")

        message = record.getMessage()
        level = record.levelname
        if self.use_color:
            level = self._color_level(record.levelname)
            operation = self._color_operation(operation)
            timestamp = f"{GRAY}{timestamp}{RESET}"

        parts = [
            timestamp,
            level,
            f"rid={request_id}",
            f"ip={ip}",
            f"actor={actor}",
        ]
        if operation:
            parts.append(f"op={operation}")
        parts.append(message)
        return " ".join(parts)

    def _color_level(self, level: str) -> str:
        if level == "ERROR":
            return f"{RED}{level}{RESET}"
        if level == "WARNING":
            return f"{YELLOW}{level}{RESET}"
        if level == "INFO":
            return f"{GREEN}{level}{RESET}"
        return level

    def _color_operation(self, operation: str) -> str:
        if operation == "CREATE":
            return f"{BLUE}{operation}{RESET}"
        if operation == "DELETE":
            return f"{RED}{operation}{RESET}"
        return operation


def configure_logging(config: CloudraveConfig) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, config.logging.level.upper(), logging.INFO))
    handler = logging.StreamHandler()
    handler.setFormatter(CloudraveFormatter(config.logging.use_color))
    root.handlers.clear()
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)

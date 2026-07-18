"""Cloudrave console logging and startup presentation."""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from config import CloudraveConfig


RESET = "\033[0m"
BLUE = "\033[34m"
CYAN = "\033[36m"
RED = "\033[31m"
GRAY = "\033[90m"
GREEN = "\033[32m"
YELLOW = "\033[33m"

STARTUP_BANNER = r"""
  ____ _                 _
 / ___| | ___  _   _  __| |_ __ __ ___   _____
| |   | |/ _ \| | | |/ _` | '__/ _` \ \ / / _ \
| |___| | (_) | |_| | (_| | | | (_| |\ V /  __/
 \____|_|\___/ \__,_|\__,_|_|  \__,_| \_/ \___|
""".strip("\n")


class LogOperation(StrEnum):
    """Stable business-operation labels for console and audit correlation."""

    STARTUP = "STARTUP"
    SHUTDOWN = "SHUTDOWN"
    DATABASE_CHECK = "DATABASE_CHECK"
    REGISTER = "REGISTER"
    LOGIN = "LOGIN"
    LOGOUT = "LOGOUT"
    REFRESH = "REFRESH"
    REVOKE = "REVOKE"
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    RENAME = "RENAME"
    MOVE = "MOVE"
    COPY = "COPY"
    RECYCLE = "RECYCLE"
    RESTORE = "RESTORE"
    DELETE = "DELETE"
    UPLOAD = "UPLOAD"
    DOWNLOAD = "DOWNLOAD"
    ABORT = "ABORT"


class LogOutcome(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    DENIED = "denied"
    PARTIAL = "partial"


class CloudraveFormatter(logging.Formatter):
    """Format service and audit logs into one compact console layout."""

    def __init__(self, use_color: bool, root_dir: Path | None = None) -> None:
        super().__init__()
        self.use_color = use_color
        self.root_dir = root_dir.resolve() if root_dir else None

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        level = f"{record.levelname.upper():<8}"
        source = self._source(record)
        message = record.getMessage()
        context = self._context(record)
        if context:
            message = f"{message} [{context}]"

        if self.use_color:
            timestamp = f"{GRAY}{timestamp}{RESET}"
            level = self._color_level(level, record.levelname)
            source = f"{CYAN}{source}{RESET}"

        rendered = f"{timestamp} | {level} | {source} | {message}"
        if record.exc_info:
            rendered = f"{rendered}\n{self.formatException(record.exc_info)}"
        if record.stack_info:
            rendered = f"{rendered}\n{self.formatStack(record.stack_info)}"
        return rendered

    def _source(self, record: logging.LogRecord) -> str:
        path = Path(record.pathname)
        if self.root_dir:
            try:
                relative_path = path.resolve().relative_to(self.root_dir)
                if "site-packages" in relative_path.parts:
                    package_index = relative_path.parts.index("site-packages") + 1
                    relative_path = Path(*relative_path.parts[package_index:])
                display_path = relative_path.as_posix()
                return f"{display_path}:{record.lineno}"
            except (OSError, ValueError):
                pass

        package = record.name.partition(".")[0]
        display_path = f"{package}/{path.name}" if package and package != "root" else path.name
        return f"{display_path}:{record.lineno}"

    def _context(self, record: logging.LogRecord) -> str:
        items: list[str] = []
        operation = self._context_value(record, "operation")
        resource = self._context_value(record, "resource")
        outcome = self._context_value(record, "outcome")
        scope = self._context_value(record, "scope")
        count = self._context_value(record, "count")
        request_id = self._context_value(record, "request_id")
        ip = self._context_value(record, "ip")
        actor = self._context_value(record, "actor")

        if operation:
            rendered_operation = self._color_operation(operation) if self.use_color else operation
            items.append(f"op={rendered_operation}")
        if resource:
            items.append(f"resource={resource}")
        if outcome:
            items.append(f"outcome={outcome}")
        if scope:
            items.append(f"scope={scope}")
        if count:
            items.append(f"count={count}")
        if request_id and request_id not in {"-", "bootstrap"}:
            items.append(f"rid={request_id}")
        if ip and ip != "-":
            items.append(f"ip={ip}")
        if actor and actor not in {"-", "system"}:
            items.append(f"actor={actor}")
        return " ".join(items)

    def _context_value(self, record: logging.LogRecord, name: str) -> str:
        value = getattr(record, name, None)
        return "" if value is None else str(value).strip()

    def _color_level(self, padded_level: str, level: str) -> str:
        if level in {"ERROR", "CRITICAL"}:
            return f"{RED}{padded_level}{RESET}"
        if level == "WARNING":
            return f"{YELLOW}{padded_level}{RESET}"
        if level == "INFO":
            return f"{GREEN}{padded_level}{RESET}"
        if level == "DEBUG":
            return f"{BLUE}{padded_level}{RESET}"
        return padded_level

    def _color_operation(self, operation: str) -> str:
        if operation in {LogOperation.CREATE, LogOperation.REGISTER}:
            return f"{BLUE}{operation}{RESET}"
        if operation == "DELETE":
            return f"{RED}{operation}{RESET}"
        if operation in {LogOperation.LOGIN, LogOperation.LOGOUT, LogOperation.REFRESH}:
            return f"{GREEN}{operation}{RESET}"
        return operation


def configure_logging(config: CloudraveConfig) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, config.logging.level.upper(), logging.INFO))
    handler = logging.StreamHandler()
    handler.setFormatter(CloudraveFormatter(config.logging.use_color, config.root_dir))
    root.handlers.clear()
    root.addHandler(handler)

    # Uvicorn should use the same formatter, while HTTP access lines are emitted
    # by Cloudrave's request middleware with request-id and actor context.
    for logger_name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.propagate = True
    logging.getLogger("uvicorn.access").handlers.clear()


def print_startup_banner(config: CloudraveConfig, version: str, mode: str = "API") -> None:
    """Write a small unprefixed product header before structured logs begin."""

    details = f"Cloudrave v{version}  |  {config.app.env}  |  Python {mode}"
    rule = "=" * max(len(details), 48)
    sys.stderr.write(f"Starting Cloudrave {version} in {config.app.env} environment...\n")
    sys.stderr.write(f"\n{STARTUP_BANNER}\n\n{details}\n{rule}\n\n")
    sys.stderr.flush()


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)

"""Allowlisted operational logs: never render message text or exception details."""

import logging
import math
import sqlite3
from datetime import UTC, datetime

import aiohttp
from aiogram.exceptions import TelegramAPIError

from .sheets import SheetError

EVENTS = frozenset(
    {
        "bot_starting",
        "bot_stopped",
        "process_failed",
        "admin_configured",
        "access_denied",
        "handler_failed",
        "sheet_removed",
        "refresh_started",
        "refresh_finished",
        "refresh_failed",
        "cache_refresh_failed",
        "notification_sent",
        "notification_blocked",
        "notification_invalid",
        "notification_retry",
        "delivery_throttled",
        "delivery_failed",
        "monitor_failed",
        "sheet_added",
        "sheet_input_failed",
        "moderation_finished",
        "moderation_failed",
        "members_granted",
        "member_revoked",
    }
)
NUMBERS = frozenset(
    {
        "sheet_id",
        "notification_id",
        "updated",
        "failed",
        "notifications",
        "elapsed_ms",
        "interval",
        "retry_after",
        "attempt",
        "count",
    }
)
ENUMS = {
    "mode": {"scheduled", "manual"},
    "result": {"approved", "rejected"},
    "error": {"timeout", "network", "telegram", "database", "sheet", "validation", "internal"},
}
logger = logging.getLogger("gradebot.audit")


def error_category(error):
    for category, types in [
        ("timeout", (TimeoutError,)),
        ("telegram", (TelegramAPIError,)),
        ("network", (aiohttp.ClientError, OSError)),
        ("database", (sqlite3.Error,)),
        ("sheet", (SheetError,)),
        ("validation", (ValueError,)),
    ]:
        if isinstance(error, types):
            return category
    return "internal"


def safe_fields(fields):
    if not isinstance(fields, dict):
        return {}
    return {
        key: value
        for key, value in fields.items()
        if (
            key in NUMBERS
            and (
                (type(value) is int and -(2**63) <= value < 2**63)
                or (type(value) is float and math.isfinite(value))
            )
        )
        or (key == "proxy" and type(value) is bool)
        or (key in ENUMS and isinstance(value, str) and value in ENUMS[key])
    }


def audit(event, *, level=logging.INFO, error=None, **fields):
    if not isinstance(event, str) or event not in EVENTS:
        raise ValueError("Unknown audit event")
    safe = safe_fields(fields)
    if error is not None:
        safe["error"] = error_category(error)
    logger.log(level, event, extra={"audit_event": event, "audit_fields": safe})


class SafeFormatter(logging.Formatter):
    def format(self, record):
        try:
            timestamp = datetime.fromtimestamp(record.created, UTC).isoformat(timespec="seconds")
            event = getattr(record, "audit_event", None)
            if record.name == "gradebot.audit" and isinstance(event, str) and event in EVENTS:
                fields = safe_fields(getattr(record, "audit_fields", {}))
                detail = " ".join(f"{key}={fields[key]}" for key in sorted(fields))
            else:
                # Never call getMessage() or render traceback/locals from external records.
                event, detail = "external_log", ""
            level = {10: "DEBUG", 20: "INFO", 30: "WARNING", 40: "ERROR", 50: "CRITICAL"}.get(
                record.levelno, "WARNING"
            )
            return f"{timestamp} {level} {event}" + (f" {detail}" if detail else "")
        except Exception:
            return "ERROR log_format_failed"


class SafeStreamHandler(logging.StreamHandler):
    def handleError(self, record):
        # The standard handler prints raw message/args when formatting or writing fails.
        return


def configure_logging(level="INFO"):
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL")
    handler = SafeStreamHandler()
    handler.setFormatter(SafeFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
    logging.captureWarnings(True)
    for name, item in logging.root.manager.loggerDict.items():
        if isinstance(item, logging.Logger):
            item.handlers.clear()
            item.propagate = True
            item.setLevel(logging.NOTSET if name.startswith("gradebot") else logging.WARNING)

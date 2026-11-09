import asyncio
import json
import logging
import re
import sqlite3
import time
from datetime import datetime
from decimal import Decimal
from typing import TypedDict
from zoneinfo import ZoneInfo

import aiohttp
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import LinkPreviewOptions

from .db import Database
from .logging_setup import audit
from .messages import messages
from .presentation import bold, paginate, safe_lines, table_link, view_markup
from .sheets import IndexedSheetLookup, Sheet, discover_sheets, fetch_sheet
from .structure import (
    header_signature,
    structure_message,
    workbook_changes,
    workbook_url,
)


class CacheState(TypedDict):
    """Timestamp and freshness used by cache captions."""

    fetched_at: float | None
    stale: bool


class GradeReport(CacheState):
    """Cached grades and lookup status for one user and sheet."""

    sheet_id: int
    workbook_id: int
    title: str
    url: str
    status: str
    grades: dict[str, str]


class ScanResult(TypedDict):
    """Counters returned by a completed refresh."""

    updated: int
    failed: list[str]
    notifications: int
    structure_notifications: int


def cache_caption(report: CacheState, label: str = "Последнее обновление") -> str:
    """Format cache freshness and update time for Telegram HTML."""
    timestamp = report["fetched_at"]
    if timestamp is None:
        return "<i>Кэш ещё не загружен</i>"
    date = datetime.fromtimestamp(timestamp, ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y %H:%M:%S МСК")
    prefix = "Кэш устарел. " if report["stale"] else ""
    return f"<i>{prefix}{label}: {date}</i>"


def format_grades(report: GradeReport, *, limit: int | None = 3,
                  include_caption: bool = True) -> str:
    """Format one student grade report as safe Telegram HTML."""
    lines = [f"<b>{table_link(report['title'], report['url'])}</b>"]
    if report["status"] == "found":
        filled = [(label, value) for label, value in report["grades"].items() if value.strip()]
        recent = list(reversed(filled))
        visible = recent if limit is None else recent[:limit]
        if visible:
            lines.append("<pre>")
            for label, value in visible:
                lines.extend(safe_lines(f"- {label.replace(' → ', ' — ')}: {value}"))
            lines.append("</pre>")
        if not filled:
            lines.append("Оценок пока нет")
        numbers = [
            Decimal(value.strip().replace(",", "."))
            for _, value in filled
            if re.fullmatch(r"[+-]?\d+(?:[.,]\d+)?", value.strip())
        ]
        average = (
            f"{sum(numbers) / len(numbers):.2f}".rstrip("0").rstrip(".").replace(".", ",") if numbers else "—"
        )
        lines.extend(safe_lines(f"Среднее: {average}"))
        lines.append(f"Количество: {len(filled)}")
    else:
        lines.append(
            {
                "missing": "ФИО не найдено",
                "ambiguous": "Несколько совпадений ФИО",
                "unavailable": messages.render("replies.grades_sheet_unavailable"),
            }[report["status"]]
        )
    if include_caption:
        lines.extend(["", cache_caption(report)])
    return "\n".join(lines).replace("<pre>\n", "<pre>").replace("\n</pre>", "</pre>")


def subject_pages(report: GradeReport) -> list[str]:
    """Split a complete subject report into Telegram-sized pages."""
    if report["status"] != "found":
        return [format_grades(report)]
    grades = list(reversed([(k, v) for k, v in report["grades"].items() if v.strip()]))
    pages = []
    summary = format_grades(report, limit=0, include_caption=False).splitlines()[-2:]
    for offset in range(0, max(1, len(grades)), 10):
        part = {**report, "grades": dict(reversed(grades[offset:offset + 10]))}
        lines = format_grades(part, limit=None, include_caption=False).splitlines()
        lines[-2:] = summary
        pages.extend(paginate([*lines, "", cache_caption(report)]))
    return pages


class Monitor:
    def __init__(self, db: Database, session: aiohttp.ClientSession | None,
                 bot: Bot, interval: int = 60) -> None:
        """Initialize this component and its state."""
        self.db, self.session, self.bot = db, session, bot
        self.interval = interval
        self.lock = asyncio.Lock()
        self.delivery_lock = asyncio.Lock()
        self.scan_lock = asyncio.Lock()
        self.schedule_changed = asyncio.Event()
        self.next_refresh = 0.0
        deadline = db.all("SELECT value FROM settings WHERE key='delivery_after'")
        self.delivery_after = float(deadline[0][0]) if deadline else 0.0

    async def inspect_user(self, user_id: int, *, baseline: bool = False) -> list[GradeReport]:
        """Read persisted cache only; never wait for Google or mutate it on /grades."""
        user = self.db.all("SELECT * FROM users WHERE telegram_id=?", (user_id,))[0]
        reports = []
        for sheet in self.db.all("SELECT * FROM sheets WHERE status='approved'"):
            cache = self.db.all("SELECT * FROM sheet_cache WHERE sheet_id=?", (sheet["id"],))
            status, grades, fetched_at = "unavailable", {}, None
            if cache:
                fetched_at = cache[0]["fetched_at"]
                parsed = Sheet(
                    json.loads(cache[0]["rows"]),
                    sheet["header_row"],
                    tuple(json.loads(sheet["name_columns"])),
                    **json.loads(sheet["parser_options"]),
                )
                status, grades = parsed.lookup(user["name"])
                if baseline and status == "found":
                    self.db.snapshot(user_id, sheet["id"], grades, [])
            reports.append(
                {
                    "sheet_id": sheet["id"],
                    "workbook_id": sheet["workbook_id"],
                    "title": self.db.grade_display_title(sheet["title"], sheet["url"]),
                    "url": sheet["url"],
                    "status": status,
                    "grades": grades,
                    "fetched_at": fetched_at,
                    "stale": fetched_at is None or time.time() - fetched_at > self.interval * 2,
                }
            )
        return reports

    async def check_user(self, user_id: int, name: str | None = None) -> list[str]:
        """Inspect a user while saving the initial comparison snapshot."""
        if name is not None:
            self.db.register(user_id, name)
        return [format_grades(r) for r in await self.inspect_user(user_id, baseline=True)]

    async def fetch(self, sheet: sqlite3.Row) -> Sheet:
        """Fetch and parse an approved sheet."""
        if self.session is None:
            raise RuntimeError("Google Sheets session is not available")
        return await fetch_sheet(
            self.session, sheet["url"], sheet["header_row"], json.loads(sheet["name_columns"]),
            **json.loads(sheet["parser_options"])
        )

    async def discover(self, url: str) -> list[dict[str, str]] | None:
        """Discover tabs from a Google workbook URL."""
        if self.session is None:
            return None
        return await discover_sheets(self.session, url)

    def record_structure(self, sheet: sqlite3.Row, rows: list[list[str]],
                         invalid: bool = False) -> int:
        """Compare sheet structure and queue an administrator alert if changed."""
        options = json.loads(sheet["parser_options"])
        signature = header_signature(rows, sheet["header_row"], options.get("header_start"))
        previous = self.db.all("SELECT * FROM sheet_structure WHERE sheet_id=?", (sheet["id"],))
        old = json.loads(previous[0]["signature"]) if previous else None
        was_invalid = bool(previous[0]["invalid"]) if previous else False
        if old is None:
            cache = self.db.all("SELECT rows FROM sheet_cache WHERE sheet_id=?", (sheet["id"],))
            if cache:
                old = header_signature(json.loads(cache[0]["rows"]), sheet["header_row"],
                                       options.get("header_start"))
        changed = (old is not None and old != signature) or invalid != was_invalid
        admin = self.db.admin_id()
        if changed and admin is not None:
            self.db.queue(admin, structure_message(sheet, old, signature, invalid), kind="structure", admin=True)
        self.db.conn.execute(
            "INSERT INTO sheet_structure VALUES(?,?,?) ON CONFLICT(sheet_id) "
            "DO UPDATE SET signature=excluded.signature,invalid=excluded.invalid",
            (sheet["id"], json.dumps(signature), int(invalid)),
        )
        return int(changed and admin is not None)

    async def scan(self, *, scheduled: bool = False) -> ScanResult | None:
        """Refresh cached sheets and queue detected changes."""
        async with self.scan_lock:
            # A manual refresh may have moved the deadline while the scheduler waited.
            if scheduled and time.monotonic() < self.next_refresh:
                return None
            started = time.monotonic()
            audit("refresh_started", mode="scheduled" if scheduled else "manual")
            try:
                result = await self._scan()
                audit(
                    "refresh_finished",
                    updated=result["updated"],
                    failed=len(result["failed"]),
                    notifications=result["notifications"],
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
                return result
            except Exception as exc:
                audit("refresh_failed", level=logging.ERROR, error=exc)
                raise
            finally:
                self.next_refresh = time.monotonic() + self.interval
                self.schedule_changed.set()

    async def _scan(self) -> ScanResult:
        """Fetch all sheets and update persistent state under the monitor lock."""
        fetched, failed, structures, workbooks = [], [], [], []
        notifications = 0
        sheets = self.db.all("SELECT * FROM sheets WHERE status='approved'")
        # Discover each workbook once, even if many of its tabs are saved.
        for url in dict.fromkeys(workbook_url(sheet["url"]) for sheet in sheets):
            try:
                tabs = await self.discover(url + "/edit")
                if tabs is not None:
                    workbooks.append((url, tabs))
            except Exception as exc:
                # A failed discovery is not evidence that tabs have disappeared.
                audit("workbook_discovery_failed", level=logging.WARNING, error=exc)
        # Fetch outside the state lock: cached commands remain immediate during network outages.
        for sheet in sheets:
            try:
                parsed = await self.fetch(sheet)
                fetched.append((sheet["id"], parsed, time.time()))
                structures.append((sheet["id"], parsed.rows, False))
            except Exception as exc:
                failed.append(sheet["title"])
                if hasattr(exc, "rows"):
                    structures.append((sheet["id"], exc.rows, True))
                audit("cache_refresh_failed", level=logging.WARNING, sheet_id=sheet["id"], error=exc)
        async with self.lock:
            with self.db.conn:
                structure_notifications = 0
                active = {sheet["id"]: sheet for sheet in self.db.all(
                    "SELECT * FROM sheets WHERE status='approved'")}
                bases = {workbook_url(sheet["url"]) for sheet in active.values()}
                for watch in self.db.all("SELECT url FROM workbook_watch"):
                    if watch["url"] not in bases:
                        self.db.conn.execute("DELETE FROM workbook_watch WHERE url=?", (watch["url"],))
                for url, tabs in workbooks:
                    if url not in bases:
                        continue
                    previous = self.db.all("SELECT tabs FROM workbook_watch WHERE url=?", (url,))
                    if previous:
                        changes = workbook_changes(json.loads(previous[0]["tabs"]), tabs)
                        if changes and self.db.admin_id() is not None:
                            self.db.queue(self.db.admin_id(), [bold("Изменились листы Google-таблицы"),
                                          table_link("Открыть таблицу", url + "/edit"), *changes,
                                          messages.render("structure.new_tabs")],
                                          kind="structure", admin=True)
                            structure_notifications += 1
                    self.db.remember_workbook(url, tabs, replace=True)
                for sheet_id, rows, invalid in structures:
                    if sheet_id in active:
                        structure_notifications += self.record_structure(active[sheet_id], rows, invalid)
                users = self.db.notification_users()
                fetched_by_id = {sheet_id: (parsed, timestamp) for sheet_id, parsed, timestamp in fetched}
                original = {sheet["id"]: sheet for sheet in sheets}
                changes_by_user: dict[int, dict[int, list[tuple[str, str]]]] = {}
                for sheet in sorted(active.values(), key=lambda item: item["id"]):
                    sheet_id = sheet["id"]
                    cache = self.db.all("SELECT rows FROM sheet_cache WHERE sheet_id=?", (sheet_id,))
                    fresh = fetched_by_id.get(sheet_id)
                    if fresh is None and not cache:
                        continue
                    if fresh is not None:
                        parsed, timestamp = fresh
                        source = original[sheet_id]
                        name_columns = tuple(json.loads(sheet["name_columns"]))
                        options = json.loads(sheet["parser_options"])
                        grade_columns = options.get("grade_columns")
                        selected = tuple(grade_columns) if grade_columns is not None else None
                        settings_changed = any(source[key] != sheet[key] for key in (
                            "header_row", "name_columns", "parser_options",
                        ))
                        if (settings_changed or parsed.header_row != sheet["header_row"]
                                or parsed.name_columns != name_columns
                                or parsed.header_start != options.get("header_start")
                                or parsed.grade_columns != selected):
                            parsed = Sheet(parsed.rows, sheet["header_row"], name_columns, **options)
                        rows_changed = not cache or json.loads(cache[0]["rows"]) != parsed.rows
                        if rows_changed:
                            self.db.cache(sheet_id, parsed, timestamp)
                        else:
                            self.db.touch_cache(sheet_id, timestamp)
                    else:
                        parsed = Sheet(json.loads(cache[0]["rows"]), sheet["header_row"],
                                       tuple(json.loads(sheet["name_columns"])),
                                       **json.loads(sheet["parser_options"]))
                    snapshots = {row["user_id"]: row["grades"] for row in self.db.all(
                        "SELECT user_id,grades FROM snapshots WHERE sheet_id=?", (sheet_id,)
                    )}
                    lookup = IndexedSheetLookup(parsed)
                    for user in users:
                        user_id = user["id"]
                        status, grades = lookup.lookup(user["name"])
                        if status != "found":
                            continue
                        previous_json = snapshots.get(user_id)
                        previous = json.loads(previous_json) if previous_json is not None else {}
                        values = [
                            (column, value or "оценка удалена")
                            for column, value in grades.items()
                            if previous.get(column, "") != value
                        ]
                        if values:
                            changes_by_user.setdefault(user_id, {})[sheet_id] = values
                        if user_id not in snapshots or previous != grades:
                            self.db.conn.execute(
                                "INSERT INTO snapshots VALUES(?,?,?) "
                                "ON CONFLICT(user_id,sheet_id) DO UPDATE SET grades=excluded.grades",
                                (user_id, sheet_id, json.dumps(grades)),
                            )
                for user in users:
                    changes = changes_by_user.get(user["id"])
                    if not changes or not user["notifications"]:
                        continue
                    lines = [bold("Найдены новые оценки"), ""]
                    grade_sections: dict[int, list[str]] = {}
                    for sheet_id, values in changes.items():
                        sheet = active[sheet_id]
                        section = [bold(self.db.grade_display_title(sheet["title"], sheet["url"]))]
                        for column, value in values:
                            section.extend(safe_lines(f"- {column}: {value}"))
                        section.append("")
                        grade_sections[sheet_id] = section
                        lines.extend(section)
                    self.db.queue(user["telegram_id"], lines, grade_sections=grade_sections)
                    notifications += 1
        await self.deliver()
        return {"updated": len(fetched), "failed": failed, "notifications": notifications,
                "structure_notifications": structure_notifications}

    async def deliver(self) -> None:
        """Send pending notifications with retry and failure handling."""
        async with self.delivery_lock:
            if time.time() < self.delivery_after:
                return
            for item in self.db.all(
                "SELECT * FROM outbox WHERE status='pending' AND next_attempt<=? "
                "ORDER BY next_attempt,id LIMIT 100",
                (time.time(),),
            ):
                # Name changes may discard grade notifications while other messages are retained.
                if not self.db.all("SELECT id FROM outbox WHERE id=? AND status='pending'", (item["id"],)):
                    continue
                if not self.db.authorized(item["user_id"]):
                    self.db.execute("DELETE FROM outbox WHERE id=?", (item["id"],))
                    continue
                if item["kind"] in {"proposal", "structure"} and item["user_id"] != self.db.admin_id():
                    self.db.execute("UPDATE outbox SET status='cancelled' WHERE id=?", (item["id"],))
                    continue
                try:
                    kwargs = {}
                    if item["view_id"]:
                        kwargs = {
                            "parse_mode": "HTML",
                            "reply_markup": view_markup(self.db, item["view_id"]),
                            "link_preview_options": LinkPreviewOptions(is_disabled=True),
                        }
                    await self.bot.send_message(item["user_id"], item["text"], **kwargs)
                except TelegramForbiddenError as exc:
                    self.db.execute(
                        "UPDATE outbox SET status='blocked' WHERE user_id=? AND status='pending'",
                        (item["user_id"],),
                    )
                    audit(
                        "notification_blocked", level=logging.WARNING, notification_id=item["id"], error=exc
                    )
                except TelegramBadRequest as exc:
                    self.db.execute("UPDATE outbox SET status='failed' WHERE id=?", (item["id"],))
                    audit("notification_invalid", level=logging.ERROR, notification_id=item["id"], error=exc)
                except TelegramRetryAfter as exc:
                    audit("delivery_throttled", level=logging.WARNING, retry_after=exc.retry_after)
                    self.delivery_after = time.time() + exc.retry_after
                    self.db.execute(
                        "INSERT INTO settings VALUES('delivery_after',?) ON CONFLICT(key) "
                        "DO UPDATE SET value=excluded.value",
                        (str(self.delivery_after),),
                    )
                    self.db.execute(
                        "UPDATE outbox SET next_attempt=? WHERE status='pending'", (self.delivery_after,)
                    )
                    break
                except Exception as exc:
                    attempts = item["attempts"] + 1
                    self.db.execute(
                        "UPDATE outbox SET attempts=?,next_attempt=? WHERE id=?",
                        (attempts, time.time() + min(3600, 2 ** min(attempts, 12)), item["id"]),
                    )
                    audit(
                        "notification_retry",
                        level=logging.WARNING,
                        notification_id=item["id"],
                        attempt=attempts,
                        error=exc,
                    )
                else:
                    self.db.execute("UPDATE outbox SET status='sent' WHERE id=?", (item["id"],))
                    audit("notification_sent", level=logging.DEBUG, notification_id=item["id"])

    async def run_delivery(self) -> None:
        """Poll the outbox independently of sheet refreshes."""
        while True:
            try:
                await self.deliver()
                self.db.cleanup()
            except Exception as exc:
                audit("delivery_failed", level=logging.ERROR, error=exc)
            await asyncio.sleep(2)

    async def run(self, interval: int) -> None:
        """Schedule periodic sheet refreshes."""
        self.interval = interval
        while True:
            self.schedule_changed.clear()
            delay = max(0, self.next_refresh - time.monotonic())
            if delay:
                try:
                    await asyncio.wait_for(self.schedule_changed.wait(), timeout=delay)
                except TimeoutError:
                    pass
                else:
                    continue
            try:
                await self.scan(scheduled=True)
            except Exception as exc:
                audit("monitor_failed", level=logging.ERROR, error=exc)

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import LinkPreviewOptions

from .logging_setup import audit
from .presentation import bold, safe_lines, table_link, view_markup
from .sheets import Sheet, fetch_sheet


def cache_caption(report, label="Последнее обновление"):
    timestamp = report["fetched_at"]
    if timestamp is None:
        return "<i>Кэш ещё не загружен</i>"
    date = datetime.fromtimestamp(timestamp, ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y %H:%M:%S МСК")
    prefix = "Кэш устарел · " if report["stale"] else ""
    return f"<i>{prefix}{label}: {date}</i>"


def format_grades(report):
    lines = [f"<b>{table_link(report['title'], report['url'])}</b>", cache_caption(report)]
    if report["status"] == "found":
        filled = [(label, value) for label, value in report["grades"].items() if value.strip()]
        for label, value in filled[-5:]:
            lines.extend(safe_lines(f"- {label}: {value}"))
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
        lines.append(f"Количество оценок: {len(filled)}")
    else:
        lines.append(
            {
                "missing": "ФИО не найдено",
                "ambiguous": "Несколько совпадений ФИО",
                "unavailable": "Таблица ещё не загружена",
            }[report["status"]]
        )
    return "\n".join(lines)


class Monitor:
    def __init__(self, db, session, bot, interval=60):
        self.db, self.session, self.bot = db, session, bot
        self.interval = interval
        self.lock = asyncio.Lock()
        self.delivery_lock = asyncio.Lock()
        self.scan_lock = asyncio.Lock()
        self.schedule_changed = asyncio.Event()
        self.next_refresh = 0.0
        deadline = db.all("SELECT value FROM settings WHERE key='delivery_after'")
        self.delivery_after = float(deadline[0][0]) if deadline else 0.0

    async def inspect_user(self, user_id, *, baseline=False):
        """Read persisted cache only; never wait for Google or mutate it on /grades."""
        user = self.db.all("SELECT * FROM users WHERE id=?", (user_id,))[0]
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
                )
                status, grades = parsed.lookup(user["name"])
                if baseline and status == "found":
                    self.db.snapshot(user_id, sheet["id"], grades, [])
            reports.append(
                {
                    "title": sheet["title"],
                    "url": sheet["url"],
                    "status": status,
                    "grades": grades,
                    "fetched_at": fetched_at,
                    "stale": fetched_at is None or time.time() - fetched_at > self.interval * 2,
                }
            )
        return reports

    async def check_user(self, user_id, name=None):
        if name is not None:
            self.db.register(user_id, name)
        return [format_grades(r) for r in await self.inspect_user(user_id, baseline=True)]

    async def fetch(self, sheet):
        return await fetch_sheet(
            self.session, sheet["url"], sheet["header_row"], json.loads(sheet["name_columns"])
        )

    async def scan(self, *, scheduled=False):
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

    async def _scan(self):
        fetched, failed = [], []
        notifications = 0
        # Fetch outside the state lock: cached commands remain immediate during network outages.
        for sheet in self.db.all("SELECT * FROM sheets WHERE status='approved'"):
            try:
                parsed = await self.fetch(sheet)
                fetched.append((sheet["id"], parsed, time.time()))
            except Exception as exc:
                failed.append(sheet["title"])
                audit("cache_refresh_failed", level=logging.WARNING, sheet_id=sheet["id"], error=exc)
        async with self.lock:
            with self.db.conn:
                for sheet_id, parsed, timestamp in fetched:
                    if self.db.all("SELECT id FROM sheets WHERE id=? AND status='approved'", (sheet_id,)):
                        self.db.cache(sheet_id, parsed, timestamp)
                for user in self.db.all("SELECT u.* FROM users u JOIN members m ON m.id=u.id"):
                    lines = [bold("Найдены новые оценки:"), ""]
                    for sheet in self.db.all("SELECT * FROM sheets WHERE status='approved'"):
                        cache = self.db.all("SELECT rows FROM sheet_cache WHERE sheet_id=?", (sheet["id"],))
                        if not cache:
                            continue
                        parsed = Sheet(
                            json.loads(cache[0]["rows"]),
                            sheet["header_row"],
                            tuple(json.loads(sheet["name_columns"])),
                        )
                        status, grades = parsed.lookup(user["name"])
                        if status != "found":
                            continue
                        snapshot = self.db.all(
                            "SELECT grades FROM snapshots WHERE user_id=? AND sheet_id=?",
                            (user["id"], sheet["id"]),
                        )
                        old = json.loads(snapshot[0]["grades"]) if snapshot else {}
                        values = [
                            (column, value or "оценка удалена")
                            for column, value in grades.items()
                            if old.get(column, "") != value
                        ]
                        if values:
                            lines.append(bold(sheet["title"]))
                            for column, value in values:
                                lines.extend(safe_lines(f"- {column}: {value}"))
                            lines.append("")
                        self.db.conn.execute(
                            "INSERT INTO snapshots VALUES(?,?,?) "
                            "ON CONFLICT(user_id,sheet_id) DO UPDATE SET grades=excluded.grades",
                            (user["id"], sheet["id"], json.dumps(grades)),
                        )
                    if len(lines) > 2 and user["notifications"]:
                        self.db.queue(user["id"], lines)
                        notifications += 1
        await self.deliver()
        return {"updated": len(fetched), "failed": failed, "notifications": notifications}

    async def deliver(self):
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
                if item["kind"] == "proposal" and item["user_id"] != self.db.admin_id():
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

    async def run_delivery(self):
        while True:
            try:
                await self.deliver()
                self.db.cleanup()
            except Exception as exc:
                audit("delivery_failed", level=logging.ERROR, error=exc)
            await asyncio.sleep(2)

    async def run(self, interval):
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

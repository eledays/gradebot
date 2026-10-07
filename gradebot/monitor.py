import asyncio
import json
import logging
import re
import time
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import LinkPreviewOptions

from .presentation import bold, safe_lines, table_link, view_markup
from .sheets import Sheet, fetch_sheet

log = logging.getLogger(__name__)


def cache_caption(report):
    timestamp = report["fetched_at"]
    if timestamp is None:
        return "<i>Кэш ещё не загружен</i>"
    date = datetime.fromtimestamp(timestamp, ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y %H:%M:%S МСК")
    prefix = "Кэш устарел · " if report["stale"] else ""
    return f"<i>{prefix}Последнее обновление: {date}</i>"


def format_grades(report):
    lines = [f"<b>{table_link(report['title'], report['url'])}</b>", cache_caption(report)]
    if report["status"] == "found":
        filled = [(label, value) for label, value in report["grades"].items() if value.strip()]
        for label, value in filled[-5:]:
            lines.extend(safe_lines(f"{label}: {value}"))
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

    async def scan(self):
        fetched = []
        # Fetch outside the state lock: cached commands remain immediate during network outages.
        for sheet in self.db.all("SELECT * FROM sheets WHERE status='approved'"):
            try:
                parsed = await self.fetch(sheet)
                fetched.append((sheet["id"], parsed, time.time()))
            except Exception:
                log.exception("Failed to refresh sheet %s; preserving cache", sheet["id"])
        async with self.lock:
            with self.db.conn:
                for sheet_id, parsed, timestamp in fetched:
                    if self.db.all("SELECT id FROM sheets WHERE id=? AND status='approved'", (sheet_id,)):
                        self.db.cache(sheet_id, parsed, timestamp)
                for user in self.db.all("SELECT * FROM users"):
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
                            value or "оценка удалена"
                            for column, value in grades.items()
                            if old.get(column, "") != value
                        ]
                        if values:
                            lines.extend(safe_lines(f"{sheet['title']}: {', '.join(values)}"))
                            lines.append("")
                        self.db.conn.execute(
                            "INSERT INTO snapshots VALUES(?,?,?) "
                            "ON CONFLICT(user_id,sheet_id) DO UPDATE SET grades=excluded.grades",
                            (user["id"], sheet["id"], json.dumps(grades)),
                        )
                    if len(lines) > 2:
                        self.db.queue(user["id"], lines)
        await self.deliver()

    async def deliver(self):
        async with self.delivery_lock:
            for item in self.db.all("SELECT * FROM outbox WHERE status='pending' ORDER BY id LIMIT 100"):
                # Name changes may discard grade notifications while other messages are retained.
                if not self.db.all("SELECT id FROM outbox WHERE id=? AND status='pending'", (item["id"],)):
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
                except TelegramForbiddenError:
                    self.db.execute("UPDATE outbox SET status='blocked' WHERE id=?", (item["id"],))
                except TelegramRetryAfter as exc:
                    await asyncio.sleep(min(exc.retry_after, 60))
                    break
                except Exception:
                    log.exception("Notification %s will be retried", item["id"])
                else:
                    self.db.execute("UPDATE outbox SET status='sent' WHERE id=?", (item["id"],))

    async def run(self, interval):
        self.interval = interval
        while True:
            try:
                await self.scan()
            except Exception:
                log.exception("Monitor iteration failed")
            await asyncio.sleep(interval)

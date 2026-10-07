import asyncio
import json
import logging

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from .sheets import fetch_sheet

log = logging.getLogger(__name__)


def changes(title, old, new):
    # Removed columns are layout edits; blanking an existing cell is a grade change.
    return [
        f"{title}\n{column}: {value or 'оценка удалена'}"
        + (f" (было: {old[column]})" if old.get(column) else "")
        for column, value in new.items()
        if old.get(column, "") != value
    ]


def format_grades(report):
    status = report["status"]
    if status == "found":
        body = "\n".join(f"{k}: {v}" for k, v in report["grades"].items() if v) or "Оценок пока нет."
    else:
        body = {
            "missing": "Не нашёл ваше ФИО.",
            "ambiguous": "Нашёл несколько совпадений. Уточните ФИО через /name.",
            "unavailable": "Не удалось прочитать таблицу. Попробуйте позже.",
        }[status]
    return f"{report['title']}\n{body}"


class Monitor:
    def __init__(self, db, session, bot):
        self.db, self.session, self.bot = db, session, bot
        self.lock = asyncio.Lock()

    async def inspect_user(self, user_id, *, baseline=False):
        """Fetch current grades; read-only requests must not consume pending changes."""
        async with self.lock:
            user = self.db.all("SELECT * FROM users WHERE id=?", (user_id,))[0]
            reports = []
            for sheet in self.db.all("SELECT * FROM sheets WHERE status='approved'"):
                try:
                    parsed = await self.fetch(sheet)
                    status, grades = parsed.lookup(user["name"])
                    if status == "found" and baseline:
                        self.db.snapshot(user_id, sheet["id"], grades, [])
                except Exception:
                    log.exception("User check failed for sheet %s", sheet["id"])
                    status, grades = "unavailable", {}
                reports.append({"title": sheet["title"], "status": status, "grades": grades})
            return reports

    async def check_user(self, user_id, name=None):
        """Compatibility helper: registration baseline and formatted grade reports."""
        if name is not None:
            async with self.lock:
                self.db.register(user_id, name)
        reports = await self.inspect_user(user_id, baseline=True)
        return [format_grades(report) for report in reports]

    async def fetch(self, sheet):
        return await fetch_sheet(
            self.session, sheet["url"], sheet["header_row"], json.loads(sheet["name_columns"])
        )

    async def scan(self):
        async with self.lock:
            for sheet in self.db.all("SELECT * FROM sheets WHERE status='approved'"):
                try:
                    parsed = await self.fetch(sheet)
                    for user in self.db.all("SELECT * FROM users"):
                        status, grades = parsed.lookup(user["name"])
                        if status != "found":
                            continue  # Preserve baseline during missing/ambiguous rows.
                        rows = self.db.all(
                            "SELECT grades FROM snapshots WHERE user_id=? AND sheet_id=?",
                            (user["id"], sheet["id"]),
                        )
                        old = json.loads(rows[0]["grades"]) if rows else {}
                        messages = changes(sheet["title"], old, grades)
                        self.db.snapshot(user["id"], sheet["id"], grades, messages)
                except Exception:
                    log.exception("Failed to scan sheet %s", sheet["id"])
        await self.deliver()

    async def deliver(self):
        async with self.lock:
            await self._deliver_locked()

    async def _deliver_locked(self):
        for item in self.db.all("SELECT * FROM outbox WHERE status='pending' ORDER BY id LIMIT 100"):
            try:
                await self.bot.send_message(item["user_id"], item["text"])
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
        while True:
            try:
                await self.scan()
            except Exception:
                log.exception("Monitor iteration failed")
            await asyncio.sleep(interval)

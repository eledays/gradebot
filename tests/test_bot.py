from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Chat, Message, Update, User

from gradebot.bot import create_router, parse_spec
from gradebot.db import Database
from gradebot.monitor import Monitor
from gradebot.sheets import SheetError


def test_spec():
    assert parse_spec("Матан | https://docs.google.com/spreadsheets/d/id/edit | 2 | 1,2,3")[2:] == (
        2,
        (1, 2, 3),
    )
    with pytest.raises(SheetError):
        parse_spec("Матан | https://docs.google.com/spreadsheets/d/id/edit | 0 | 1")


async def test_non_admin_cannot_add_or_remove(tmp_path):
    db = Database(str(tmp_path / "test.sqlite3"))
    try:
        db.execute("INSERT INTO settings VALUES('admin','999')")
        db.register(123, "Иванов Иван")
        bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk")
        bot.session = AsyncMock()
        monitor = Monitor(db, None, bot)
        monitor.fetch = AsyncMock()
        dispatcher = Dispatcher()
        dispatcher.include_router(create_router(db, monitor))
        for text in ["/add Матан | https://docs.google.com/spreadsheets/d/id/edit", "/remove 1", "/pending"]:
            message = Message(
                message_id=1,
                date=datetime.now(UTC),
                chat=Chat(id=123, type="private"),
                from_user=User(id=123, is_bot=False, first_name="Иван"),
                text=text,
            )
            await dispatcher.feed_update(bot, Update(update_id=1, message=message))
        assert not db.all("SELECT * FROM sheets")
        assert bot.session.await_count == 3
        monitor.fetch.assert_not_called()
    finally:
        db.close()


@pytest.mark.parametrize(
    "user_id,action,expected",
    [(123, "approve", "pending"), (999, "approve", "approved"), (999, "reject", None)],
)
async def test_moderation_checks_admin_and_changes_status(tmp_path, user_id, action, expected):
    from aiogram.types import CallbackQuery

    db = Database(str(tmp_path / "test.sqlite3"))
    try:
        db.execute("INSERT INTO settings VALUES('admin','999')")
        sheet_id = db.add_sheet(
            "Матан", "https://docs.google.com/spreadsheets/d/id/edit", 1, (1,), 123, "pending"
        )
        bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk")
        bot.session = AsyncMock()
        monitor = Monitor(db, None, bot)
        monitor.fetch = AsyncMock()
        dispatcher = Dispatcher()
        dispatcher.include_router(create_router(db, monitor))
        callback = CallbackQuery(
            id="query",
            from_user=User(id=user_id, is_bot=False, first_name="Test"),
            chat_instance="instance",
            data=f"{action}:{sheet_id}",
        )
        await dispatcher.feed_update(bot, Update(update_id=1, callback_query=callback))
        rows = db.all("SELECT status FROM sheets")
        assert (rows[0]["status"] if rows else None) == expected
        if user_id != 999:
            monitor.fetch.assert_not_called()
    finally:
        db.close()

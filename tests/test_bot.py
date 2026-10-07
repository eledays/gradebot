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


@pytest.fixture
def dialog_bot(tmp_path):
    from aiogram.fsm.storage.memory import SimpleEventIsolation

    db = Database(str(tmp_path / "dialogs.sqlite3"))
    db.execute("INSERT INTO settings VALUES('admin','999')")
    bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk")
    bot.session = AsyncMock()
    monitor = Monitor(db, None, bot)
    dispatcher = Dispatcher(events_isolation=SimpleEventIsolation())
    dispatcher.include_router(create_router(db, monitor))

    class Harness:
        async def send(self, text, user_id=123):
            message = Message(
                message_id=1,
                date=datetime.now(UTC),
                chat=Chat(id=user_id, type="private"),
                from_user=User(id=user_id, is_bot=False, first_name="Test"),
                text=text,
            )
            await dispatcher.feed_update(bot, Update(update_id=1, message=message))

        def replies(self):
            return [
                call.args[1].text for call in bot.session.await_args_list if hasattr(call.args[1], "text")
            ]

        async def state(self, user_id=123):
            context = dispatcher.fsm.get_context(bot=bot, chat_id=user_id, user_id=user_id)
            return await context.get_state()

    harness = Harness()
    harness.db, harness.bot, harness.monitor = db, bot, monitor
    yield harness
    db.close()


async def test_start_checks_tables_and_summarizes_failures(dialog_bot):
    from gradebot.bot import Dialog
    from gradebot.sheets import Sheet

    h = dialog_bot
    h.db.add_sheet("Матан", "https://docs.google.com/spreadsheets/d/math/edit", 1, (1,), 999, "approved")
    h.db.add_sheet("Физика", "https://docs.google.com/spreadsheets/d/physics/edit", 1, (1,), 999, "approved")
    h.monitor.fetch = AsyncMock(
        side_effect=[
            Sheet([["ФИО", "С1"], ["Иванов Иван", "5"]]),
            Sheet([["ФИО", "С1"], ["Петров Петр", "4"]]),
        ]
    )
    await h.send("/start")
    assert await h.state() == Dialog.registration.state
    await h.send("Иванов")
    assert await h.state() == Dialog.registration.state
    await h.send("Иванов Иван")
    assert await h.state() is None
    assert h.db.all("SELECT name FROM users")[0]["name"] == "Иванов Иван"
    assert h.replies()[-2] == "Сохранил, проверяю по таблицам…"
    assert "кроме" in h.replies()[-1] and "Физика — ФИО не найдено" in h.replies()[-1]
    assert "С1: 5" not in h.replies()[-1]
    assert h.monitor.fetch.await_count == 2


async def test_name_only_saves_and_command_switches_states(dialog_bot):
    h = dialog_bot
    h.monitor.fetch = AsyncMock()
    await h.send("/start")
    await h.send("/name")
    assert h.replies()[-1] == "Отправь новое имя, которое хочешь указать."
    await h.send("Иванов Иван")
    assert h.replies()[-1] == "Сохранил."
    assert await h.state() is None
    h.monitor.fetch.assert_not_called()


async def test_add_keeps_title_and_link_state_after_parse_error(dialog_bot, monkeypatch):
    from gradebot.bot import Dialog
    from gradebot.sheets import Sheet

    h = dialog_bot
    h.db.register(999, "Иванов Иван")
    fetch = AsyncMock(side_effect=[SheetError("not CSV"), Sheet([["ФИО", "С1"], ["Иванов Иван", "5"]])])
    monkeypatch.setattr("gradebot.bot.fetch_sheet", fetch)
    await h.send("/add", 999)
    assert await h.state(999) == Dialog.title.state
    await h.send("Матан", 999)
    assert await h.state(999) == Dialog.link.state
    await h.send("https://docs.google.com/spreadsheets/d/id/edit", 999)
    assert "Не получилось спарсить" in h.replies()[-1]
    assert await h.state(999) == Dialog.link.state
    assert not h.db.all("SELECT * FROM sheets")
    await h.send("https://docs.google.com/spreadsheets/d/id/edit", 999)
    assert await h.state(999) is None
    row = h.db.all("SELECT * FROM sheets")[0]
    assert row["title"] == "Матан" and row["status"] == "approved"
    assert h.replies()[-1] == "Таблицу добавил."


async def test_suggest_saves_pending_and_notifies_admin(dialog_bot, monkeypatch):
    from gradebot.sheets import Sheet

    h = dialog_bot
    h.db.register(123, "Иванов Иван")
    monkeypatch.setattr(
        "gradebot.bot.fetch_sheet", AsyncMock(return_value=Sheet([["ФИО", "С1"], ["Иванов Иван", "5"]]))
    )
    await h.send("/suggest")
    await h.send("Матан")
    await h.send("https://docs.google.com/spreadsheets/d/id/edit")
    assert h.db.all("SELECT status FROM sheets")[0]["status"] == "pending"
    assert any("Отправил таблицу админу" in reply for reply in h.replies())
    assert h.bot.session.await_args_list[-1].args[1].chat_id == 999


async def test_cancel_and_help_do_not_expose_admin_commands(dialog_bot):
    h = dialog_bot
    await h.send("/name")
    await h.send("/help")
    assert "/add" not in h.replies()[-1] and "/pending" not in h.replies()[-1]
    assert "/grades" in h.replies()[-1]
    await h.send("/cancel")
    assert await h.state() is None
    await h.send("Иванов Иван")
    assert not h.db.all("SELECT * FROM users")
    await h.send("/help", 999)
    assert "/add" in h.replies()[-1] and "/pending" in h.replies()[-1]


async def test_grades_reads_live_data_without_consuming_notification(dialog_bot):
    from gradebot.sheets import Sheet

    h = dialog_bot
    h.db.register(123, "Иванов Иван")
    sheet_id = h.db.add_sheet(
        "Матан", "https://docs.google.com/spreadsheets/d/id/edit", 1, (1,), 999, "approved"
    )
    h.db.snapshot(123, sheet_id, {"С1": "4"}, [])
    h.monitor.fetch = AsyncMock(return_value=Sheet([["ФИО", "С1"], ["Иванов Иван", "5"]]))
    await h.send("/grades")
    assert "Матан\nС1: 5" in h.replies()[-1]
    import json

    assert json.loads(h.db.all("SELECT grades FROM snapshots")[0]["grades"]) == {"С1": "4"}
    h.monitor.bot.send_message = AsyncMock()
    await h.monitor.scan()
    h.monitor.bot.send_message.assert_awaited_once_with(123, "Матан\nС1: 5 (было: 4)")


async def test_grades_requires_registration(dialog_bot):
    h = dialog_bot
    h.monitor.fetch = AsyncMock()
    await h.send("/grades")
    assert "/start" in h.replies()[-1]
    h.monitor.fetch.assert_not_called()


async def test_dialogs_are_isolated_between_users(dialog_bot):
    from gradebot.bot import Dialog

    h = dialog_bot
    await h.send("/start", 123)
    await h.send("/name", 456)
    await h.send("Иванов Иван", 456)
    assert await h.state(123) == Dialog.registration.state
    assert await h.state(456) is None
    assert h.db.all("SELECT id FROM users")[0]["id"] == 456


async def test_remove_retries_unknown_id_then_clears_state(dialog_bot):
    from gradebot.bot import Dialog

    h = dialog_bot
    sheet_id = h.db.add_sheet(
        "Матан", "https://docs.google.com/spreadsheets/d/id/edit", 1, (1,), 999, "approved"
    )
    await h.send("/remove", 999)
    await h.send("не число", 999)
    assert await h.state(999) == Dialog.remove.state
    await h.send("9999", 999)
    assert await h.state(999) == Dialog.remove.state
    await h.send(str(sheet_id), 999)
    assert await h.state(999) is None
    assert not h.db.all("SELECT * FROM sheets")


async def test_optional_layout_and_duplicate_link_keep_dialog(dialog_bot, monkeypatch):
    from gradebot.bot import Dialog
    from gradebot.sheets import Sheet

    h = dialog_bot
    h.db.register(999, "Иванов Иван")
    h.db.add_sheet(
        "Матан",
        "https://docs.google.com/spreadsheets/d/id/export?format=csv&gid=0",
        2,
        (1, 2),
        999,
        "approved",
    )
    fetch = AsyncMock(
        return_value=Sheet([["Title"], ["Фамилия", "Имя", "С1"], ["Иванов", "Иван", "5"]], 2, (1, 2))
    )
    monkeypatch.setattr("gradebot.bot.fetch_sheet", fetch)
    await h.send("/add", 999)
    await h.send("Физика", 999)
    await h.send("/layout 2 | 1,2", 999)
    await h.send("https://docs.google.com/spreadsheets/d/id/edit", 999)
    assert await h.state(999) == Dialog.link.state
    assert "уже добавлена" in h.replies()[-1]
    await h.send("https://docs.google.com/spreadsheets/d/physics/edit", 999)
    assert await h.state(999) is None
    row = h.db.all("SELECT * FROM sheets WHERE title='Физика'")[0]
    assert row["header_row"] == 2
    assert row["name_columns"] == "[1, 2]"


def test_registration_summary_distinguishes_unavailable_from_missing():
    from gradebot.bot import registration_summary

    assert "во всех таблицах." in registration_summary([{"title": "Матан", "status": "found"}])
    summary = registration_summary([{"title": "Матан", "status": "unavailable"}])
    assert "Не удалось проверить" in summary
    assert "ФИО не найдено" not in summary
    assert "Таблиц пока нет" in registration_summary([])

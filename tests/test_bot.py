from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Chat, Message, Update, User

from gradebot.bot import create_router, parse_spec
from gradebot.db import Database
from gradebot.monitor import Monitor
from gradebot.sheets import Sheet, SheetError


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
        monitor.fetch = AsyncMock(return_value=Sheet([["ФИО", "С1"], ["Иванов Иван", "5"]]))
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
        monitor.fetch = AsyncMock(return_value=Sheet([["ФИО", "С1"], ["Иванов Иван", "5"]]))
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
def dialog_bot(tmp_path, monkeypatch):
    from aiogram.fsm.storage.memory import SimpleEventIsolation

    db = Database(str(tmp_path / "dialogs.sqlite3"))
    db.execute("INSERT INTO settings VALUES('admin','999')")
    db.grant_members([(123, "ivanov", "Иванов Иван")])
    bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk")
    bot.session = AsyncMock()
    monitor = Monitor(db, None, bot)
    dispatcher = Dispatcher(events_isolation=SimpleEventIsolation())
    dispatcher.include_router(create_router(db, monitor))

    class Harness:
        async def callback(self, data, user_id=999):
            from aiogram.types import CallbackQuery

            callback = CallbackQuery(
                id="test",
                from_user=User(id=user_id, is_bot=False, first_name="Test"),
                chat_instance="instance",
                data=data,
                message=Message(
                    message_id=1,
                    date=datetime.now(UTC),
                    chat=Chat(id=user_id, type="private"),
                    text="Подтверждение",
                ),
            )
            await dispatcher.feed_update(bot, Update(update_id=1, callback_query=callback))

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

    async def tabs(session, url):
        return [{"name": "Лист 1", "url": url}]

    monkeypatch.setattr("gradebot.bot.discover_sheets", tabs)
    monkeypatch.setattr("gradebot.bot.fetch_sheet", AsyncMock(return_value=[["ФИО", "С1"], ["Иванов Иван", "5"]]))
    harness = Harness()
    harness.db, harness.bot, harness.monitor, harness.dispatcher = db, bot, monitor, dispatcher
    yield harness
    db.close()


async def test_start_checks_tables_and_summarizes_failures(dialog_bot):
    h = dialog_bot
    h.db.add_sheet(
        "Матан",
        "https://docs.google.com/spreadsheets/d/math/edit",
        1,
        (1,),
        999,
        "approved",
        parsed=Sheet([["ФИО", "С1"], ["Иванов Иван", "5"]]),
    )
    h.db.add_sheet(
        "Физика",
        "https://docs.google.com/spreadsheets/d/physics/edit",
        1,
        (1,),
        999,
        "approved",
        parsed=Sheet([["ФИО", "С1"], ["Петров Петр", "4"]]),
    )
    h.monitor.fetch = AsyncMock()
    await h.send("/start")
    assert await h.state() is None
    assert h.db.all("SELECT name FROM users")[0]["name"] == "Иванов Иван"
    assert "Привет, Иванов Иван" in h.replies()[0]
    assert "- Физика" in h.replies()[-1]
    assert "Матан" not in h.replies()[-1]
    h.monitor.fetch.assert_not_called()


async def test_user_cannot_change_admin_assigned_name(dialog_bot):
    h = dialog_bot
    await h.send("/start")
    await h.send("/name")
    assert "администратор" in h.replies()[-1]
    await h.send("Петров Петр")
    assert h.db.all("SELECT name FROM users")[0]["name"] == "Иванов Иван"
    assert await h.state() is None


async def test_add_keeps_title_and_link_state_after_parse_error(dialog_bot, monkeypatch):
    from gradebot.bot import Dialog

    h = dialog_bot
    h.db.register(999, "Иванов Иван")
    discover = AsyncMock(side_effect=[SheetError("not CSV"), [{
        "name": "Лист 1", "url": "https://docs.google.com/spreadsheets/d/id/edit#gid=0"}]])
    monkeypatch.setattr("gradebot.bot.discover_sheets", discover)
    await h.send("/add", 999)
    assert await h.state(999) == Dialog.title.state
    await h.send("Матан", 999)
    assert await h.state(999) == Dialog.link.state
    await h.send("https://docs.google.com/spreadsheets/d/id/edit", 999)
    assert "Не удалось прочитать" in h.replies()[-1]
    assert await h.state(999) == Dialog.link.state
    assert not h.db.all("SELECT * FROM sheets")
    await h.send("https://docs.google.com/spreadsheets/d/id/edit", 999)
    token = (await h.dispatcher.fsm.get_context(bot=h.bot, chat_id=999, user_id=999).get_data())["setup_token"]
    await h.callback(f"tabs:{token}:next")
    await h.send("1 | A", 999)
    await h.send("готово", 999)
    await h.callback(f"worksave:{token}")
    assert await h.state(999) is None
    row = h.db.all("SELECT * FROM sheets")[0]
    assert row["title"] == "Матан" and row["status"] == "approved"
    assert "Добавлено листов: 1" in h.replies()


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
    with h.db.conn:
        h.db.cache(sheet_id, Sheet([["ФИО", "С1"], ["Иванов Иван", "5"]]))
    await h.send("/grades")
    assert "С1: 5" in h.replies()[-1]
    import json

    assert json.loads(h.db.all("SELECT grades FROM snapshots")[0]["grades"]) == {"С1": "4"}
    h.monitor.bot.send_message = AsyncMock()
    await h.monitor.scan()
    assert h.monitor.bot.send_message.await_count == 1
    assert "<b>Матан</b>\n- С1: 5" in h.monitor.bot.send_message.await_args.args[1]


async def test_grades_requires_registration(dialog_bot):
    h = dialog_bot
    h.monitor.fetch = AsyncMock()
    await h.send("/grades")
    assert "/start" in h.replies()[-1]
    h.monitor.fetch.assert_not_called()


async def test_dialogs_are_isolated_between_users(dialog_bot):
    from gradebot.bot import Dialog

    h = dialog_bot
    h.db.grant_members([(456, "petrov", "Петров Петр")])
    await h.send("/start", 123)
    await h.send("/start", 456)
    await h.send("/suggest", 123)
    assert await h.state(123) == Dialog.title.state
    assert await h.state(456) is None
    assert [row["name"] for row in h.db.all("SELECT name FROM users ORDER BY id")] == [
        "Иванов Иван",
        "Петров Петр",
    ]


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
    assert await h.state(999) == Dialog.remove_confirm.state
    assert h.db.all("SELECT * FROM sheets")
    context = h.dispatcher.fsm.get_context(bot=h.bot, chat_id=999, user_id=999)
    data = await context.get_data()
    await h.callback(f"remove:{data['token']}")
    assert await h.state(999) is None
    assert not h.db.all("SELECT * FROM sheets")


async def test_optional_layout_and_duplicate_link_keep_dialog(dialog_bot, monkeypatch):
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
    fetch = AsyncMock(return_value=[["Title"], ["Фамилия", "Имя", "С1"], ["Иванов", "Иван", "5"]])
    monkeypatch.setattr("gradebot.bot.fetch_sheet", fetch)
    await h.send("/add", 999)
    await h.send("Физика", 999)
    await h.send("/layout 2 | 1,2", 999)
    await h.send("https://docs.google.com/spreadsheets/d/id/edit", 999)
    token = (await h.dispatcher.fsm.get_context(bot=h.bot, chat_id=999, user_id=999).get_data())["setup_token"]
    await h.callback(f"tabs:{token}:next")
    await h.send("2 | A:B", 999)
    await h.send("готово", 999)
    await h.callback(f"worksave:{token}")
    assert "Ничего не сохранено" in h.replies()[-1]
    await h.send("/cancel", 999)
    await h.send("/add", 999)
    await h.send("Физика", 999)
    await h.send("https://docs.google.com/spreadsheets/d/physics/edit", 999)
    token = (await h.dispatcher.fsm.get_context(bot=h.bot, chat_id=999, user_id=999).get_data())["setup_token"]
    await h.callback(f"tabs:{token}:next")
    await h.send("2 | A:B", 999)
    await h.send("готово", 999)
    await h.callback(f"worksave:{token}")
    assert await h.state(999) is None
    row = h.db.all("SELECT * FROM sheets WHERE title='Физика'")[0]
    assert row["header_row"] == 2
    assert row["name_columns"] == "[1, 2]"


def test_registration_summary_distinguishes_unavailable_from_missing():
    from gradebot.bot import registration_summary

    assert registration_summary([{"title": "Матан", "status": "found"}]) == ""
    summary = registration_summary([{"title": "Матан", "status": "unavailable"}])
    assert "Не удалось проверить" in summary
    assert "ФИО не найдено" not in summary
    assert "Таблиц пока нет" in registration_summary([])


async def test_grades_and_tables_each_send_one_cached_message(dialog_bot):
    h = dialog_bot
    h.db.register(123, "Иванов Иван")
    for index in range(3):
        h.db.add_sheet(
            f"Предмет {index}",
            f"https://docs.google.com/spreadsheets/d/id{index}/edit",
            1,
            (1,),
            999,
            "approved",
            parsed=Sheet([["ФИО", "С1"], ["Иванов Иван", "5"]]),
        )
    h.monitor.fetch = AsyncMock(side_effect=RuntimeError("must not call Google"))
    await h.send("/grades")
    assert h.bot.session.await_count == 1
    assert all(f"Предмет {index}" in h.replies()[0] for index in range(3))
    assert "Последнее обновление" in h.replies()[0]
    await h.send("/tables")
    assert h.bot.session.await_count == 2
    assert "/edit#gid=0" in h.replies()[-1] and "/export" not in h.replies()[-1]
    h.monitor.fetch.assert_not_called()


async def test_pending_is_one_message_with_moderation_actions(dialog_bot):
    h = dialog_bot
    for index in range(3):
        h.db.add_sheet(
            f"Предмет {index}",
            f"https://docs.google.com/spreadsheets/d/id{index}/edit",
            1,
            (1,),
            123,
            "pending",
        )
    await h.send("/pending", 999)
    assert h.bot.session.await_count == 1
    markup = h.bot.session.await_args.args[1].reply_markup
    assert len(markup.inline_keyboard) == 3
    assert all(f"Предмет {index}" in h.replies()[-1] for index in range(3))


@pytest.mark.parametrize("action", ["approve", "reject"])
async def test_moderation_notifies_proposer_once_and_broadcasts_only_approval(dialog_bot, action):
    from aiogram.types import CallbackQuery

    h = dialog_bot
    h.db.register(123, "Иванов Иван")
    h.db.register(456, "Петров Петр")
    sheet_id = h.db.add_sheet(
        "ОРГ", "https://docs.google.com/spreadsheets/d/org/edit", 1, (1,), 123, "pending"
    )
    h.monitor.fetch = AsyncMock(return_value=Sheet([["ФИО", "С1"], ["Иванов Иван", "5"]]))
    callback = CallbackQuery(
        id="query",
        from_user=User(id=999, is_bot=False, first_name="Admin"),
        chat_instance="instance",
        data=f"{action}:{sheet_id}",
    )
    for _ in range(2):
        dispatcher = Dispatcher()
        dispatcher.include_router(create_router(h.db, h.monitor))
        await dispatcher.feed_update(h.bot, Update(update_id=1, callback_query=callback))
    results = h.db.all("SELECT * FROM outbox WHERE kind='moderation'")
    assert len(results) == 1 and results[0]["user_id"] == 123
    assert ("одобрена" if action == "approve" else "отклонена") in results[0]["text"]
    assert results[0]["status"] == "sent"
    tables = h.db.all("SELECT * FROM outbox WHERE kind='table'")
    assert len(tables) == (2 if action == "approve" else 0)


async def test_paginated_grades_are_owner_scoped_and_survive_restart(dialog_bot):
    import json

    from aiogram.types import CallbackQuery

    from gradebot.presentation import create_view, view_markup

    h = dialog_bot
    with h.db.conn:
        view_id, pages = create_view(h.db, 123, ["<b>Оценки</b>", *["x" * 400 for _ in range(20)]])
    assert len(pages) > 1
    path = h.db.all("PRAGMA database_list")[0]["file"]
    reopened = Database(path)
    try:
        dispatcher = Dispatcher()
        dispatcher.include_router(create_router(reopened, Monitor(reopened, None, h.bot)))
        message = Message(
            message_id=10,
            date=datetime.now(UTC),
            chat=Chat(id=123, type="private"),
            text=pages[0],
            reply_markup=view_markup(reopened, view_id),
        )
        for owner in [456, 123]:
            h.bot.session.reset_mock()
            callback = CallbackQuery(
                id="page",
                from_user=User(id=owner, is_bot=False, first_name="Test"),
                chat_instance="instance",
                message=message,
                data=f"page:{view_id}:1",
            )
            await dispatcher.feed_update(h.bot, Update(update_id=10, callback_query=callback))
            methods = [call.args[1] for call in h.bot.session.await_args_list]
            edits = [method for method in methods if method.__class__.__name__ == "EditMessageText"]
            assert len(edits) == (1 if owner == 123 else 0)
            if edits:
                assert edits[0].text == json.loads(reopened.all("SELECT pages FROM views")[0]["pages"])[1]
    finally:
        reopened.close()


async def test_help_handler_uses_configured_contact_and_interval(dialog_bot):
    h = dialog_bot
    h.monitor.interval = 120
    dispatcher = Dispatcher()
    dispatcher.include_router(create_router(h.db, h.monitor, "@group_admin"))
    for user_id in [123, 999]:
        message = Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=user_id, type="private"),
            from_user=User(id=user_id, is_bot=False, first_name="Test"),
            text="/help",
        )
        await dispatcher.feed_update(h.bot, Update(update_id=1, message=message))
        method = h.bot.session.await_args.args[1]
        assert method.parse_mode == "HTML"
        assert "каждые 2 мин" in method.text
        assert "Для связи: @group_admin" in method.text
        assert method.text.endswith("<i>Кэш ещё не загружен</i>")
        assert ("<b>Админ</b>" in method.text) == (user_id == 999)


@pytest.mark.parametrize("command,user_id", [("/add", 999), ("/suggest", 123)])
async def test_duplicate_title_retries_before_asking_for_link(dialog_bot, command, user_id):
    from gradebot.bot import Dialog

    h = dialog_bot
    h.db.register(user_id, "Иванов Иван")
    h.db.add_sheet(
        "Математический анализ", "https://docs.google.com/spreadsheets/d/id/edit", 1, (1,), 999, "pending"
    )
    await h.send(command, user_id)
    await h.send("  математический   АНАЛИЗ  ", user_id)
    assert await h.state(user_id) == Dialog.title.state
    assert "Введи другое название" in h.replies()[-1]
    await h.send("История", user_id)
    assert await h.state(user_id) == Dialog.link.state
    assert h.replies()[-1].startswith("Отправь ссылку на таблицу")


async def test_title_taken_during_link_input_returns_to_title(dialog_bot, monkeypatch):
    from gradebot.bot import Dialog

    h = dialog_bot
    h.db.register(999, "Иванов Иван")
    await h.send("/add", 999)
    await h.send("История", 999)
    h.db.add_sheet("История", "https://docs.google.com/spreadsheets/d/other/edit", 1, (1,), 999, "approved")
    monkeypatch.setattr(
        "gradebot.bot.fetch_sheet", AsyncMock(return_value=Sheet([["ФИО", "С1"], ["Иванов Иван", "5"]]))
    )
    await h.send("https://docs.google.com/spreadsheets/d/new/edit", 999)
    assert await h.state(999) == Dialog.title.state
    assert "Введи другое название" in h.replies()[-1]
    assert len(h.db.all("SELECT * FROM sheets")) == 1


async def test_start_opens_admin_menu_without_student_registration(dialog_bot):
    h = dialog_bot
    await h.send("/start", 999)
    assert "Панель администратора" in h.replies()[-1]
    method = h.bot.session.await_args.args[1]
    labels = [button.text for row in method.reply_markup.keyboard for button in row]
    assert "Добавить пользователей" in labels
    assert "Добавить таблицу" in labels
    assert not h.db.all("SELECT * FROM users")


async def test_start_uses_assigned_name_and_opens_simple_menu(dialog_bot):
    h = dialog_bot
    h.db.add_sheet(
        "ОРГ",
        "https://docs.google.com/spreadsheets/d/org/edit",
        1,
        (1,),
        999,
        "approved",
        parsed=Sheet([["ФИО", "С1"], ["Иванов Иван", "5"]]),
    )
    await h.send("/start")
    assert len(h.replies()) == 1
    assert h.replies()[0].startswith("Привет, Иванов Иван!")
    method = h.bot.session.await_args.args[1]
    labels = [button.text for row in method.reply_markup.keyboard for button in row]
    assert "Мои оценки" in labels and "Таблицы" in labels
    assert "Добавить пользователей" not in labels
    assert h.db.all("SELECT * FROM snapshots")


async def test_help_greets_saved_name_and_shows_latest_approved_cache(dialog_bot):
    h = dialog_bot
    h.db.register(123, "Иванов Иван")
    for index, status in enumerate(["approved", "approved", "pending"], 1):
        h.db.add_sheet(
            f"Предмет {index}", f"https://docs.google.com/spreadsheets/d/id{index}/edit", 1, (1,), 999, status
        )
        with h.db.conn:
            h.db.cache(
                index,
                Sheet([["ФИО", "С1"], ["Иванов Иван", "5"]]),
                fetched_at=1_700_000_000 + (index - 1) * 3600,
            )
    await h.send("/help")
    text = h.replies()[-1]
    assert text.startswith("Рад тебя видеть, Иванов Иван\n\n")
    assert text.endswith("<i>Кэш устарел · Последнее обновление кэша: 15.11.2023 02:13:20 МСК</i>")
    h.db.register(123, "Петров Петр")
    await h.send("/help")
    assert h.replies()[-1].startswith("Рад тебя видеть, Петров Петр\n\n")


async def test_refresh_is_admin_only_and_reports_failures(dialog_bot):
    h = dialog_bot
    h.monitor.scan = AsyncMock(return_value={"updated": 2, "failed": ["История"], "notifications": 1})
    await h.send("/refresh", 123)
    h.monitor.scan.assert_not_called()
    await h.send("/help", 123)
    assert "/refresh" not in h.replies()[-1]
    await h.send("/refresh", 999)
    h.monitor.scan.assert_awaited_once()
    assert "Обновлено таблиц: 2" in h.replies()[-1]
    assert "- История" in h.replies()[-1]
    assert "Уведомлений об изменениях: 1" in h.replies()[-1]
    await h.send("/help", 999)
    assert "/refresh" in h.replies()[-1]


async def test_refresh_acknowledges_before_network_work(dialog_bot):
    h = dialog_bot

    async def scan():
        assert h.replies()[-1] == "Секундочку"
        return {"updated": 1, "failed": [], "notifications": 0}

    h.monitor.scan = AsyncMock(side_effect=scan)
    await h.send("/refresh", 999)
    assert h.replies()[0] == "Секундочку"
    assert "Обновление кэша завершено" in h.replies()[-1]


async def test_table_link_acknowledges_before_fetch_and_error(dialog_bot, monkeypatch):
    h = dialog_bot
    h.db.register(999, "Иванов Иван")

    async def fetch(*args):
        assert h.replies()[-1] == "Секундочку"
        raise SheetError("Google unavailable")

    monkeypatch.setattr("gradebot.bot.discover_sheets", fetch)
    await h.send("/add", 999)
    await h.send("История", 999)
    await h.send("https://docs.google.com/spreadsheets/d/id/edit", 999)
    assert h.replies()[-2] == "Секундочку"
    assert "Не удалось прочитать" in h.replies()[-1]


async def test_approval_acknowledges_before_fetch(dialog_bot):
    from aiogram.types import CallbackQuery

    h = dialog_bot
    sheet_id = h.db.add_sheet(
        "ОРГ", "https://docs.google.com/spreadsheets/d/org/edit", 1, (1,), 123, "pending"
    )

    async def fetch(sheet):
        assert h.replies()[-1] == "Секундочку"
        return Sheet([["ФИО", "С1"], ["Иванов Иван", "5"]])

    h.monitor.fetch = AsyncMock(side_effect=fetch)
    dispatcher = Dispatcher()
    dispatcher.include_router(create_router(h.db, h.monitor))
    message = Message(
        message_id=1, date=datetime.now(UTC), chat=Chat(id=999, type="private"), text="Предложение"
    )
    callback = CallbackQuery(
        id="approve",
        from_user=User(id=999, is_bot=False, first_name="Admin"),
        chat_instance="instance",
        message=message,
        data=f"approve:{sheet_id}",
    )
    await dispatcher.feed_update(h.bot, Update(update_id=1, callback_query=callback))
    assert h.replies()[0] == "Секундочку"
    assert "Таблица одобрена" in h.replies()


async def test_subject_grades_pagination_is_owned_and_handles_deleted_sheet(dialog_bot):
    import json

    h = dialog_bot
    h.db.activate(123)
    sheet_id = h.db.add_sheet(
        "Матан", "https://docs.google.com/spreadsheets/d/math/edit", 1, (1,), 999, "approved",
        parsed=Sheet([["ФИО", *[f"С{i}" for i in range(1, 24)]], ["Иванов Иван", *["5"] * 23]]),
    )
    await h.send("/grades")
    text = h.replies()[-1]
    assert "- С23: 5" in text and "- С20:" not in text
    assert text.endswith("</i>")
    await h.callback(f"subject:{sheet_id}", 123)
    view = h.db.all("SELECT * FROM views ORDER BY created_at DESC")[0]
    pages = json.loads(view["pages"])
    assert view["owner"] == 123 and len(pages) == 3
    assert "- С23: 5" in pages[0] and "- С13: 5" in pages[1] and "- С1: 5" in pages[2]
    assert all("Количество: 23" in page for page in pages)
    await h.callback(f"page:{view['id']}:1", 123)
    assert pages[1] in h.replies()
    h.db.execute("DELETE FROM sheets WHERE id=?", (sheet_id,))
    await h.callback(f"subject:{sheet_id}", 123)
    assert h.bot.session.await_args.args[1].text == "Таблица больше недоступна"


async def setup_data(h):
    return await h.dispatcher.fsm.get_context(bot=h.bot, chat_id=999, user_id=999).get_data()


async def test_workbook_selection_and_complex_configuration(dialog_bot, monkeypatch):
    import json

    from gradebot.bot import Dialog

    h = dialog_bot
    base = "https://docs.google.com/spreadsheets/d/complex/edit#gid="
    tabs = [{"name": "Квизы", "url": base + "10"}, {"name": "Семинары", "url": base + "20"}]
    monkeypatch.setattr("gradebot.bot.discover_sheets", AsyncMock(return_value=tabs))
    rows = [["ФИО", "Квиз 1", "ИТОГ", "Квиз 2", "ИТОГ"],
            ["", "№1", "№2", "№1", ""], ["Иванов Иван", "1", "2,5", "3", "4"]]
    monkeypatch.setattr("gradebot.bot.fetch_sheet", AsyncMock(return_value=rows))
    await h.send("/add", 999)
    await h.send("Матан", 999)
    await h.send(base + "10", 999)
    data = await setup_data(h)
    token = data["setup_token"]
    assert data["selected"] == [0, 1]
    await h.callback(f"tabs:{token}:all", 123)
    assert (await setup_data(h))["selected"] == [0, 1]
    await h.callback(f"tabs:{token}:all")
    assert (await setup_data(h))["selected"] == []
    await h.callback(f"tabs:{token}:next")
    assert await h.state(999) == Dialog.sheet_selection.state
    await h.callback(f"tabs:{token}:all")
    await h.callback(f"tabs:{token}:1")
    await h.callback(f"tabs:{token}:next")
    await h.send("1-2 | A", 999)
    await h.send("только по названию итог", 999)
    assert (await setup_data(h))["grade_columns"] == [3, 5]
    await h.send("только C,E", 999)
    assert (await setup_data(h))["grade_columns"] == [3, 5]
    await h.send("исключить C:E", 999)
    assert (await setup_data(h))["grade_columns"] == [3, 5]
    await h.send("готово", 999)
    assert not h.db.all("SELECT * FROM sheets")
    await h.callback(f"worksave:{token}")
    saved = h.db.all("SELECT * FROM sheets")[0]
    assert saved["title"] == "Матан · Квизы"
    assert json.loads(saved["parser_options"]) == {"header_start": 1, "grade_columns": [3, 5]}
    h.db.register(123, "Иванов Иван")
    reports = await h.monitor.inspect_user(123)
    assert reports[0]["grades"] == {"ИТОГ → №2": "2,5", "ИТОГ": "4"}
    h.monitor.session = object()
    fetch = AsyncMock(return_value=Sheet(rows, 2, (1,), 1, (3, 5)))
    monkeypatch.setattr("gradebot.monitor.fetch_sheet", fetch)
    await h.monitor.fetch(saved)
    assert fetch.await_args.kwargs == {"header_start": 1, "grade_columns": [3, 5]}
    await h.callback(f"worksave:{token}")
    assert len(h.db.all("SELECT * FROM sheets")) == 1


async def test_workbook_save_rolls_back_every_sheet_on_conflict(dialog_bot, monkeypatch):
    h = dialog_bot
    h.db.register(123, "Иванов Иван")
    tabs = [{"name": "Первый", "url": "https://docs.google.com/spreadsheets/d/book/edit#gid=1"},
            {"name": "Второй", "url": "https://docs.google.com/spreadsheets/d/book/edit#gid=2"}]
    monkeypatch.setattr("gradebot.bot.discover_sheets", AsyncMock(return_value=tabs))
    await h.send("/add", 999)
    await h.send("Матан", 999)
    await h.send(tabs[0]["url"], 999)
    token = (await setup_data(h))["setup_token"]
    await h.callback(f"tabs:{token}:next")
    for _ in tabs:
        await h.send("1 | A", 999)
        await h.send("готово", 999)
    h.db.add_sheet("Матан · Второй", "https://docs.google.com/spreadsheets/d/conflict/edit", 1, (1,),
                   999, "approved")
    await h.callback(f"worksave:{token}")
    assert [row["title"] for row in h.db.all("SELECT * FROM sheets")] == ["Матан · Второй"]
    assert not h.db.all("SELECT * FROM sheet_cache")
    assert not h.db.all("SELECT * FROM outbox")


async def test_proposal_picker_selection_survives_until_admin_configuration(dialog_bot, monkeypatch):
    import json
    h = dialog_bot
    h.db.register(123, "Иванов Иван")
    tabs = [{"name": "Первый", "url": "https://docs.google.com/spreadsheets/d/book/edit#gid=1"},
            {"name": "Второй", "url": "https://docs.google.com/spreadsheets/d/book/edit#gid=2"}]
    monkeypatch.setattr("gradebot.bot.discover_sheets", AsyncMock(return_value=tabs))
    await h.send("/suggest", 123)
    await h.send("Матан", 123)
    await h.send(tabs[0]["url"], 123)
    row = h.db.all("SELECT * FROM sheets")[0]
    assert json.loads(row["parser_options"])["selected"] == [0, 1]
    await h.callback(f"ptabs:{row['id']}:0")
    await h.callback(f"configure:{row['id']}")
    data = await setup_data(h)
    assert data["selected"] == [1]
    await h.send("1 | A", 999)
    await h.send("готово", 999)
    await h.callback(f"worksave:{data['setup_token']}")
    saved = h.db.all("SELECT * FROM sheets")[0]
    assert saved["title"] == "Матан · Второй" and saved["status"] == "approved"
    assert saved["url"] == tabs[1]["url"]


async def test_compact_auto_setup_buttons_and_stale_sheet_callback(dialog_bot, monkeypatch):
    from gradebot.bot import Dialog

    h = dialog_bot
    tabs = [{"name": "Семинары", "url": "https://docs.google.com/spreadsheets/d/id/edit#gid=0"},
            {"name": "Квизы", "url": "https://docs.google.com/spreadsheets/d/id/edit#gid=1"}]
    rows = [["№", "ФИО", "03.09.2026", "", "Итоги"],
            ["", "", "Семинар", "", "Текущая оценка"],
            ["", "", "Отсутствие", "Выход к доске", ""],
            ["1", "Боженко Станислав Павлович", "0", "2", "0,0"]]
    monkeypatch.setattr("gradebot.bot.discover_sheets", AsyncMock(return_value=tabs))
    monkeypatch.setattr("gradebot.bot.fetch_sheet", AsyncMock(return_value=rows))
    await h.send("/add", 999)
    await h.send("Матан", 999)
    await h.send(tabs[0]["url"], 999)
    token = (await setup_data(h))["setup_token"]
    h.bot.session.reset_mock()
    await h.callback(f"tabs:{token}:next")
    assert await h.state(999) == Dialog.sheet_columns.state
    replies = [reply for reply in h.replies() if reply]
    assert len(replies) == 1
    assert len(replies[0]) < 650
    assert "ФИО: B · Заголовки: строки 1–3" in replies[0]
    assert "Боженко" not in replies[0]
    assert "• 03.09.2026 → Семинар → Отсутствие: 0" in replies[0]
    assert "• 03.09.2026 → Семинар → Выход к доске: 2" in replies[0]
    assert "• Итоги → Текущая оценка: 0,0" in replies[0]
    assert (await setup_data(h))["grade_columns"] == (3, 4, 5)
    await h.callback(f"sheetcfg:{token}:0:totals", 123)
    assert (await setup_data(h))["grade_columns"] == (3, 4, 5)
    await h.callback(f"sheetcfg:{token}:0:totals")
    assert (await setup_data(h))["grade_columns"] == [5]
    assert "• Итоги → Текущая оценка: 0,0" in h.replies()[-1]
    assert "Выход к доске" not in h.replies()[-1]
    await h.callback(f"sheetcfg:{token}:0:details")
    assert any("○ C:" in reply for reply in h.replies() if reply)
    await h.callback(f"sheetcfg:{token}:0:next")
    assert (await setup_data(h))["current_index"] == 1
    await h.callback(f"sheetcfg:{token}:0:skip")
    assert (await setup_data(h))["selected"] == [0, 1]
    await h.callback(f"sheetcfg:{token}:1:layout")
    assert await h.state(999) == Dialog.sheet_layout.state
    await h.send("1-3 | B", 999)
    await h.callback(f"sheetcfg:{token}:1:next")
    assert await h.state(999) == Dialog.workbook_confirm.state
    await h.callback(f"worksave:{token}")
    assert len(h.db.all("SELECT * FROM sheets WHERE status='approved'")) == 2


async def test_auto_setup_falls_back_to_short_manual_prompt(dialog_bot, monkeypatch):
    from gradebot.bot import Dialog

    h = dialog_bot
    rows = [["Студент", "Семинар"], ["Иванов Иван", "5"]]
    monkeypatch.setattr("gradebot.bot.fetch_sheet", AsyncMock(return_value=rows))
    await h.send("/add", 999)
    await h.send("Матан", 999)
    await h.send("https://docs.google.com/spreadsheets/d/id/edit", 999)
    token = (await setup_data(h))["setup_token"]
    await h.callback(f"tabs:{token}:next")
    assert await h.state(999) == Dialog.sheet_layout.state
    assert len(h.replies()[-1]) < 600
    assert "Иванов Иван" not in h.replies()[-1]
    await h.send("1 | A", 999)
    await h.callback(f"sheetcfg:{token}:0:next")
    assert await h.state(999) == Dialog.workbook_confirm.state


@pytest.mark.parametrize("student_values,expected", [
    (["", "0"], ["• С1: пусто", "• С2: 0", "• С3: пусто"]),
    (["5\n баллов", "x" * 100, "4"], ["• С1: 5 баллов", "• С2: " + "x" * 60 + "…", "• С3: 4"]),
])
async def test_setup_preview_uses_one_student_row_and_bounds_values(
    dialog_bot, monkeypatch, student_values, expected,
):
    h = dialog_bot
    rows = [["Фамилия", "Имя", "С1", "С2", "С3"],
            ["", "", "служебное значение"], ["Иванов", "Иван", *student_values]]
    monkeypatch.setattr("gradebot.bot.fetch_sheet", AsyncMock(return_value=rows))
    await h.send("/add", 999)
    await h.send("Матан", 999)
    await h.send("https://docs.google.com/spreadsheets/d/id/edit", 999)
    token = (await setup_data(h))["setup_token"]
    await h.callback(f"tabs:{token}:next")
    await h.send("1 | A:B", 999)
    preview = h.replies()[-1]
    assert all(line in preview for line in expected)
    assert "Иванов" not in preview and "служебное значение" not in preview

import json
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
        for text in ["/add Матан | https://docs.google.com/spreadsheets/d/id/edit", "/remove 1",
                     "/removetable", "/pending"]:
            message = Message(
                message_id=1,
                date=datetime.now(UTC),
                chat=Chat(id=123, type="private"),
                from_user=User(id=123, is_bot=False, first_name="Иван"),
                text=text,
            )
            await dispatcher.feed_update(bot, Update(update_id=1, message=message))
        assert not db.all("SELECT * FROM sheets")
        assert bot.session.await_count == 4
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
    assert "Привет, Иван" in h.replies()[0]
    assert "- Физика" in h.replies()[-1]
    assert "Матан" not in h.replies()[-1]
    h.monitor.fetch.assert_not_called()


async def test_name_change_requires_admin_approval(dialog_bot):
    h = dialog_bot
    await h.send("/start")
    await h.send("/name")
    assert "Администратор" in h.replies()[-1]
    await h.send("Петров Петр")
    assert h.db.all("SELECT name FROM users")[0]["name"] == "Иванов Иван"
    assert h.db.name_request(123) == "Петров Петр"
    assert await h.state() is None


async def test_add_keeps_title_and_link_state_after_parse_error(dialog_bot, monkeypatch):
    from gradebot.bot import Dialog

    h = dialog_bot
    h.db.register(999, "Иванов Иван")
    discover = AsyncMock(side_effect=[SheetError("not CSV"), [{
        "name": "Лист 1", "url": "https://docs.google.com/spreadsheets/d/id/edit#gid=0"}]])
    monkeypatch.setattr("gradebot.bot.discover_sheets", discover)
    await h.send("/add", 999)
    assert await h.state(999) == Dialog.link.state
    await h.send("https://docs.google.com/spreadsheets/d/id/edit", 999)
    assert await h.state(999) == Dialog.title.state
    await h.send("Матан", 999)
    assert "Не удалось прочитать" in h.replies()[-1]
    assert await h.state(999) == Dialog.title.state
    assert not h.db.all("SELECT * FROM sheets")
    await h.send("Матан", 999)
    token = (await h.dispatcher.fsm.get_context(bot=h.bot, chat_id=999, user_id=999).get_data())["setup_token"]
    await h.callback(f"tabs:{token}:next")
    await h.send("1 A", 999)
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
    await h.send("https://docs.google.com/spreadsheets/d/id/edit")
    await h.send("Матан")
    assert h.db.all("SELECT status FROM sheets")[0]["status"] == "pending"
    assert any("Отправил таблицу админу" in reply for reply in h.replies())
    assert h.bot.session.await_args_list[-1].args[1].chat_id == 999


async def test_sheet_picker_falls_back_when_custom_check_is_unavailable(dialog_bot):
    from types import SimpleNamespace

    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import SendMessage

    h = dialog_bot
    h.bot.get_sticker_set = AsyncMock(return_value=SimpleNamespace(stickers=[
        SimpleNamespace(emoji="✅", custom_emoji_id="12345")
    ]))
    attempted = []

    async def send(*args, **kwargs):
        method = args[1]
        if isinstance(method, SendMessage) and method.reply_markup:
            button = method.reply_markup.inline_keyboard[0][0]
            attempted.append(button.icon_custom_emoji_id)
            if button.icon_custom_emoji_id:
                raise TelegramBadRequest(method=method, message="custom icon requires Premium")
        return AsyncMock()

    h.bot.session.side_effect = send
    await h.send("/add", 999)
    await h.send("https://docs.google.com/spreadsheets/d/id/edit", 999)
    await h.send("Матан", 999)
    assert attempted == ["12345", None]
    message = h.bot.session.await_args_list[-1].args[1]
    assert message.reply_markup.inline_keyboard[0][0].text == "✅ Лист 1"


async def test_add_flow_removes_progress_and_selection_messages(dialog_bot):
    from aiogram.methods import DeleteMessage, SendMessage

    h = dialog_bot
    next_id = 10

    async def send(bot, method, **kwargs):
        nonlocal next_id
        if isinstance(method, SendMessage):
            next_id += 1
            return Message(message_id=next_id, date=datetime.now(UTC),
                           chat=Chat(id=999, type="private"), text=method.text).as_(bot)
        return True

    h.bot.session.side_effect = send
    await h.send("/add", 999)
    await h.send("https://docs.google.com/spreadsheets/d/id/edit", 999)
    await h.send("Матан", 999)
    data = await h.dispatcher.fsm.get_context(bot=h.bot, chat_id=999, user_id=999).get_data()
    await h.callback(f"tabs:{data['setup_token']}:next")
    methods = [call.args[1] for call in h.bot.session.await_args_list]
    busy_positions = [i for i, method in enumerate(methods)
                      if isinstance(method, SendMessage) and method.text == "Секундочку"]
    assert len(busy_positions) == 2
    for position in busy_positions:
        assert isinstance(methods[position + 1], DeleteMessage)
    assert sum(isinstance(method, DeleteMessage) for method in methods) == 3


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
    assert await h.state(123) == Dialog.link.state
    assert await h.state(456) is None
    assert [row["name"] for row in h.db.all("SELECT name FROM users ORDER BY id")] == [
        "Иванов Иван",
        "Петров Петр",
    ]


async def test_remove_retries_unknown_id_then_offers_empty_workbook_removal(dialog_bot):
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
    assert await h.state(999) == Dialog.remove_table_confirm.state
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
    await h.send("https://docs.google.com/spreadsheets/d/id/edit", 999)
    await h.send("/layout 2 | 1,2", 999)
    await h.send("Физика", 999)
    token = (await h.dispatcher.fsm.get_context(bot=h.bot, chat_id=999, user_id=999).get_data())["setup_token"]
    await h.callback(f"tabs:{token}:next")
    await h.send("2 A:B", 999)
    await h.send("готово", 999)
    await h.callback(f"worksave:{token}")
    assert "Ничего не сохранено" in h.replies()[-1]
    await h.send("/cancel", 999)
    await h.send("/add", 999)
    await h.send("https://docs.google.com/spreadsheets/d/physics/edit", 999)
    await h.send("Физика", 999)
    token = (await h.dispatcher.fsm.get_context(bot=h.bot, chat_id=999, user_id=999).get_data())["setup_token"]
    await h.callback(f"tabs:{token}:next")
    await h.send("2 A:B", 999)
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
    assert "Листов пока нет" in registration_summary([])


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


async def test_grades_group_tabs_with_full_titles_and_no_actions(dialog_bot):
    import json

    h = dialog_bot
    h.db.register(123, "Иванов Иван")
    for index, (book, title) in enumerate([
        ("org", "ОРГ"), ("algebra", "Алгебра · Семинары"),
        ("other", "Другой предмет"), ("algebra", "Алгебра · КР 1"),
    ]):
        h.db.add_sheet(title, f"https://docs.google.com/spreadsheets/d/{book}/edit#gid={index}",
                       1, (1,), 999, "approved",
                       parsed=Sheet([["ФИО", "С1"], ["Иванов Иван", "5"], ["Петров Петр", "2"]]))
    h.monitor.fetch = AsyncMock(side_effect=RuntimeError("must not call Google"))
    await h.send("/grades")
    text = h.replies()[-1]
    assert text.index(">ОРГ</a>") < text.index(">Алгебра. Семинары</a>")
    assert text.index(">Алгебра. Семинары</a>") < text.index(">Алгебра. КР 1</a>")
    assert text.index(">Алгебра. КР 1</a>") < text.index(">Другой предмет</a>")
    assert "<b>Алгебра</b>" not in text
    assert text.count("- С1: 5") == 4 and "- С1: 2" not in text
    view = h.db.all("SELECT * FROM views ORDER BY created_at DESC")[0]
    assert json.loads(view["actions"]) == []
    h.monitor.fetch.assert_not_called()


async def test_grades_show_original_tab_name_and_readable_column_paths(dialog_bot):
    h = dialog_bot
    h.db.register(123, "Иванов Иван")
    base = "https://docs.google.com/spreadsheets/d/algebra/edit#gid="
    first = Sheet(
        [["ФИО", "Модуль 1", ""], ["", "Что-то 1", "Что-то 2"],
         ["Иванов Иван", "1", "2"]], 2, (1,), 1
    )
    h.db.add_sheet("Алгебра", base + "1", 2, (1,), 999, "approved", parsed=first)
    h.db.add_sheet("Алгебра · Лист3", base + "3", 1, (1,), 999, "approved",
                   parsed=Sheet([["ФИО", "Ну"], ["Иванов Иван", "2"]]))
    with h.db.conn:
        h.db.remember_workbook(base + "1", [
            {"name": "Лист1", "url": base + "1"},
            {"name": "Лист3", "url": base + "3"},
        ])
    await h.send("/grades")
    method = h.bot.session.await_args.args[1]
    text = method.text
    assert method.parse_mode == "HTML"
    assert text.startswith("<b>Текущие оценки</b>\n\n")
    assert ">Алгебра. Лист1</a>" in text
    assert ">Алгебра. Лист3</a>" in text
    assert "Модуль 1 — Что-то 2: 2" in text
    assert "Модуль 1 — Что-то 1: 1" in text
    assert "Модуль 1 →" not in text
    assert text.index("Алгебра. Лист1") < text.index("Алгебра. Лист3")


@pytest.mark.parametrize("command,user_id,state_name", [
    ("/columns", 999, "columns_id"), ("/subject", 123, "subject_id"),
])
async def test_id_selection_allows_reading_tables_retries_and_cancel(dialog_bot, command, user_id, state_name):
    from gradebot.bot import Dialog

    h = dialog_bot
    h.db.register(123, "Иванов Иван")
    h.db.add_sheet("Матан", "https://docs.google.com/spreadsheets/d/id/edit", 1, (1,), 999, "approved",
                   parsed=Sheet([["ФИО", "Квиз"], ["Иванов Иван", "5"]]))
    expected = getattr(Dialog, state_name).state
    await h.send(command, user_id)
    assert await h.state(user_id) == expected
    for invalid in ["не число", "0", "-1", "9" * 30]:
        await h.send(invalid, user_id)
        assert "числовой ID" in h.replies()[-1]
        assert await h.state(user_id) == expected
    await h.send("Таблицы", user_id)
    assert "Таблицы группы" in h.replies()[-1]
    assert await h.state(user_id) == expected
    await h.send("/tables", user_id)
    assert "Таблицы группы" in h.replies()[-1]
    assert await h.state(user_id) == expected
    await h.send("/cancel", user_id)
    assert await h.state(user_id) is None
    await h.send(command, user_id)
    await h.send("1", user_id)
    if command == "/columns":
        assert await h.state(user_id) == Dialog.edit_columns.state
        assert "Нажми на столбец" in h.replies()[-1]
    else:
        assert await h.state(user_id) is None
        assert "Квиз: 5" in h.replies()[-1]


@pytest.mark.parametrize("command,user_id,state_name", [
    ("/subject", 123, "subject_id"),
    ("/columns", 999, "columns_id"),
    ("/remove", 999, "remove"),
    ("/addtab", 999, "addtab_id"),
])
async def test_sheet_id_commands_include_compact_escaped_preview(dialog_bot, command, user_id, state_name):
    from gradebot.bot import Dialog

    h = dialog_bot
    h.db.register(123, "Иванов Иван")
    first = h.db.add_sheet("Матан <1>", "https://docs.google.com/spreadsheets/d/first/edit",
                           1, (1,), 999, "approved")
    second = h.db.add_sheet("Физика & химия", "https://docs.google.com/spreadsheets/d/second/edit",
                            1, (1,), 999, "approved")
    await h.send(command, user_id)
    method = h.bot.session.await_args.args[1]
    assert await h.state(user_id) == getattr(Dialog, state_name).state
    assert method.parse_mode == "HTML"
    assert f"#{first}: Матан &lt;1&gt;" in method.text
    assert f"#{second}: Физика &amp; химия" in method.text
    assert method.reply_markup is None


async def test_addtab_retries_invalid_and_missing_id(dialog_bot):
    from gradebot.bot import Dialog

    h = dialog_bot
    h.db.add_sheet("Матан", "https://docs.google.com/spreadsheets/d/book/edit", 1, (1,), 999, "approved")
    await h.send("/addtab", 999)
    for value in ("не число", "0", "-1", "9" * 30):
        await h.send(value, 999)
        assert "числовой ID" in h.replies()[-1]
        assert await h.state(999) == Dialog.addtab_id.state
    await h.send("999", 999)
    assert "Таблица не найдена" in h.replies()[-1]
    assert await h.state(999) == Dialog.addtab_id.state


async def test_addtab_lists_workbooks_and_uses_workbook_id_not_sheet_id(dialog_bot, monkeypatch):
    h = dialog_bot
    algebra = "https://docs.google.com/spreadsheets/d/algebra/edit#gid="
    simple = "https://docs.google.com/spreadsheets/d/simple/edit#gid="
    h.db.add_sheet("Алгебра · Лист1", algebra + "1", 1, (1,), 999, "approved")
    h.db.add_sheet("Алгебра · Лист3", algebra + "3", 1, (1,), 999, "approved")
    h.db.add_sheet("Простые оценки", simple + "1", 1, (1,), 999, "approved")
    workbooks = h.db.extension_workbooks()
    assert len(workbooks) == 2
    assert workbooks[0]["id"] != workbooks[1]["id"]
    assert workbooks[0]["title"] == "Алгебра"

    async def discover(_session, url):
        assert "simple" in url
        return [{"name": "Лист1", "url": simple + "1"}, {"name": "Лист2", "url": simple + "2"}]

    monkeypatch.setattr("gradebot.bot.discover_sheets", discover)
    await h.send("/addtab", 999)
    preview = h.replies()[-1]
    assert f"#{workbooks[0]['id']}: Алгебра" in preview
    assert f"#{workbooks[1]['id']}: Простые оценки" in preview
    assert "Лист1" not in preview and "Лист3" not in preview
    await h.send(str(workbooks[1]["id"]), 999)
    data = await setup_data(h)
    assert data["title"] == "Простые оценки"
    assert data["tabs"] == [{"name": "Лист2", "url": simple + "2"}]
    assert data["extension_source"] == workbooks[1]["id"]


@pytest.mark.parametrize("command,user_id", [("/columns", 999), ("/subject", 123)])
async def test_id_commands_with_no_sheets_do_not_wait_for_input(dialog_bot, command, user_id):
    h = dialog_bot
    h.db.activate(123)
    await h.send(command, user_id)
    assert h.replies()[-1] == "Листов пока нет"
    assert await h.state(user_id) is None


async def test_tables_group_workbook_tabs_without_action_buttons(dialog_bot):
    import json

    from gradebot.presentation import view_markup

    h = dialog_bot
    titles = ["ОРГ", "Алгебра · Семинары 1 модуль", "Другая таблица", "Алгебра · КР 1"]
    urls = ["org", "algebra", "other", "algebra"]
    for index, (title, book) in enumerate(zip(titles, urls, strict=True)):
        h.db.add_sheet(title, f"https://docs.google.com/spreadsheets/d/{book}/edit#gid={index}",
                       1, (1,), 999, "approved",
                       parsed=Sheet([["ФИО", "С1"], ["Иванов Иван", "5"]]))
        h.db.execute("UPDATE sheet_cache SET fetched_at=? WHERE sheet_id=?", (1793561701 + index, index + 1))
    h.monitor.fetch = AsyncMock(side_effect=RuntimeError("must not call Google"))
    await h.send("/tables", 999)
    text = h.replies()[-1]
    assert '<b>Алгебра</b>\n#2: <a href=' in text
    assert ">Семинары 1 модуль</a>" in text and ">КР 1</a>" in text
    assert "Алгебра ·" not in text
    assert text.index("#1:") < text.index("#2:") < text.index("#4:") < text.index("#3:")
    assert text.count("Последнее обновление:") == 3
    assert "#gid=1" in text and "#gid=3" in text
    from gradebot.monitor import cache_caption
    assert cache_caption({"fetched_at": 1793561704, "stale": True}) in text
    view = h.db.all("SELECT * FROM views ORDER BY created_at DESC")[0]
    markup = view_markup(h.db, view["id"])
    assert markup is None
    assert len(h.db.all("SELECT * FROM sheets")) == 4
    await h.send("Таблицы", 123)
    view = h.db.all("SELECT * FROM views ORDER BY created_at DESC")[0]
    assert json.loads(view["actions"]) == []
    assert "<b>Алгебра</b>" in h.replies()[-1]
    h.monitor.fetch.assert_not_called()


@pytest.mark.parametrize("timestamps,expected", [
    ([None, None], "Кэш ещё не загружен"),
    ([None, 1000], "Кэш загружен не для всех листов"),
    ([1, 1000], "Кэш устарел"),
    ([995, 1000], "Последнее обновление:"),
])
async def test_table_group_cache_status_includes_all_tabs(dialog_bot, monkeypatch, timestamps, expected):
    h = dialog_bot
    monkeypatch.setattr("gradebot.bot.time.time", lambda: 1000)
    for index, timestamp in enumerate(timestamps):
        sheet_id = h.db.add_sheet(f"Алгебра · Лист {index}",
                                  f"https://docs.google.com/spreadsheets/d/algebra/edit#gid={index}",
                                  1, (1,), 999, "approved")
        if timestamp is not None:
            with h.db.conn:
                h.db.cache(sheet_id, Sheet([["ФИО", "С1"], ["Иванов Иван", "5"]]), fetched_at=timestamp)
    await h.send("/tables")
    text = h.replies()[-1]
    assert expected in text
    if timestamps == [995, 1000]:
        assert "Кэш устарел" not in text
    if any(timestamp is not None for timestamp in timestamps):
        assert text.count("Последнее обновление:") == 1


async def test_table_groups_use_workbook_identity_and_escape_titles(dialog_bot):
    h = dialog_bot
    for index, (book, title) in enumerate([
        ("first", "Алгебра <1> · Семинар & КР"),
        ("second", "Алгебра <1> · Экзамен"),
        ("first", "Алгебра <1> · Итоги · Модуль 1"),
        ("legacy", "Старое название"),
        ("legacy", "Отдельное название"),
    ]):
        h.db.add_sheet(title, f"https://docs.google.com/spreadsheets/d/{book}/edit#gid={index}",
                       1, (1,), 999, "approved")
    await h.send("/tables")
    text = h.replies()[-1]
    assert text.count("<b>Алгебра &lt;1&gt;</b>") == 1
    assert ">Семинар &amp; КР</a>" in text and ">Итоги · Модуль 1</a>" in text
    assert ">Алгебра &lt;1&gt; · Экзамен</a>" in text  # Another file remains a single table.
    assert "<b>Старое название</b>" in text
    assert ">Старое название</a>" in text and ">Отдельное название</a>" in text


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
    await h.send("https://docs.google.com/spreadsheets/d/new/edit", user_id)
    await h.send("  математический   АНАЛИЗ  ", user_id)
    assert await h.state(user_id) == Dialog.title.state
    assert "Введи другое название" in h.replies()[-1]
    await h.send("История", user_id)
    assert await h.state(user_id) == (Dialog.sheet_selection.state if command == "/add" else None)


async def test_title_taken_during_link_input_returns_to_title(dialog_bot, monkeypatch):
    from gradebot.bot import Dialog

    h = dialog_bot
    h.db.register(999, "Иванов Иван")
    await h.send("/add", 999)
    await h.send("https://docs.google.com/spreadsheets/d/new/edit", 999)
    h.db.add_sheet("История", "https://docs.google.com/spreadsheets/d/other/edit", 1, (1,), 999, "approved")
    monkeypatch.setattr(
        "gradebot.bot.fetch_sheet", AsyncMock(return_value=Sheet([["ФИО", "С1"], ["Иванов Иван", "5"]]))
    )
    await h.send("История", 999)
    assert await h.state(999) == Dialog.title.state
    assert "Введи другое название" in h.replies()[-1]
    assert len(h.db.all("SELECT * FROM sheets")) == 1


async def test_start_opens_admin_menu_without_student_registration(dialog_bot):
    h = dialog_bot
    await h.send("/start", 999)
    assert "Панель администратора" in h.replies()[-1]
    method = h.bot.session.await_args.args[1]
    labels = [button.text for row in method.reply_markup.keyboard for button in row]
    assert "Создать ссылку" in labels
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
    assert h.replies()[0].startswith("Привет, Иван!")
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
    assert text.startswith("Рад тебя видеть, Иван\n\n")
    assert text.endswith("<i>Кэш устарел · Последнее обновление кэша: 15.11.2023 02:13:20 МСК</i>")
    h.db.register(123, "Петров Петр")
    await h.send("/help")
    assert h.replies()[-1].startswith("Рад тебя видеть, Петр\n\n")


async def test_refresh_is_admin_only_and_reports_failures(dialog_bot):
    h = dialog_bot
    h.monitor.scan = AsyncMock(return_value={"updated": 2, "failed": ["История"], "notifications": 1})
    await h.send("/refresh", 123)
    h.monitor.scan.assert_not_called()
    await h.send("/help", 123)
    assert "/refresh" not in h.replies()[-1]
    await h.send("/refresh", 999)
    h.monitor.scan.assert_awaited_once()
    assert "Обновлено листов: 2" in h.replies()[-1]
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
    await h.send("https://docs.google.com/spreadsheets/d/id/edit", 999)
    await h.send("История", 999)
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
    await h.send("/subject", 123)
    await h.send("не число", 123)
    assert "числовой ID" in h.replies()[-1]
    await h.send("9999", 123)
    assert "Лист больше недоступен" in h.replies()[-1]
    await h.send(str(sheet_id), 123)
    assert await h.state(123) is None
    view = h.db.all("SELECT * FROM views ORDER BY created_at DESC")[0]
    pages = json.loads(view["pages"])
    assert view["owner"] == 123 and len(pages) == 3
    assert "- С23: 5" in pages[0] and "- С13: 5" in pages[1] and "- С1: 5" in pages[2]
    assert all("Количество: 23" in page for page in pages)
    await h.callback(f"page:{view['id']}:1", 123)
    assert pages[1] in h.replies()
    h.db.execute("DELETE FROM sheets WHERE id=?", (sheet_id,))
    await h.callback(f"subject:{sheet_id}", 123)
    assert h.bot.session.await_args.args[1].text == "Лист больше недоступен"


async def setup_data(h):
    return await h.dispatcher.fsm.get_context(bot=h.bot, chat_id=999, user_id=999).get_data()


async def test_saved_columns_buttons_save_cancel_and_reject_stale_callbacks(dialog_bot):
    import json

    h = dialog_bot
    h.db.activate(123)
    h.db.add_sheet("Матан", "https://docs.google.com/spreadsheets/d/id/edit", 1, (1,), 999, "approved",
                   parsed=Sheet([["ФИО", "Квиз", "Экзамен"], ["Иванов Иван", "4", "5"]]))
    await h.send("Столбцы", 999)
    assert "Отправь ID листа" in h.replies()[-1]
    await h.send("не число", 999)
    assert "числовой ID" in h.replies()[-1]
    await h.send("9999", 999)
    assert "Лист больше недоступен" in h.replies()[-1]
    await h.send("1", 999)
    token = (await setup_data(h))["column_token"]
    await h.callback(f"colpick:{token}:3")
    assert "C: Экзамен" in h.replies()[-1]
    assert "✅ C: Экзамен" not in h.replies()[-1]
    assert json.loads(h.db.all("SELECT parser_options FROM sheets")[0][0])["grade_columns"] is None
    await h.callback(f"colpick:{token}:done")
    assert json.loads(h.db.all("SELECT parser_options FROM sheets")[0][0])["grade_columns"] == [2]
    assert await h.state(999) is None
    await h.callback(f"colpick:{token}:all")
    assert h.replies()[-1] == "Настройка устарела"
    await h.callback("pick_columns:1")
    token = (await setup_data(h))["column_token"]
    assert (await setup_data(h))["column_selected"] == [2]
    await h.callback(f"colpick:{token}:all")
    await h.callback(f"colpick:{token}:cancel")
    assert json.loads(h.db.all("SELECT parser_options FROM sheets")[0][0])["grade_columns"] == [2]
    await h.send("/grades", 123)
    assert "Экзамен" not in h.replies()[-1] and "Квиз: 4" in h.replies()[-1]


async def test_columns_are_admin_only_and_require_cached_approved_sheet(dialog_bot):
    h = dialog_bot
    h.db.add_sheet("Матан", "https://docs.google.com/spreadsheets/d/id/edit", 1, (1,), 999, "approved")
    await h.send("/columns", 123)
    assert h.replies()[-1] == "Эта команда недоступна"
    await h.callback("pick_columns:1", 123)
    assert h.replies()[-1] == "Только для админа"
    await h.callback("pick_columns:1")
    assert "Кэш ещё не загружен" in h.replies()[-1]
    assert not (await setup_data(h)).get("column_token")
    with h.db.conn:
        h.db.cache(1, Sheet([["ФИО", "Квиз"], ["Иванов Иван", "5"]]))
    await h.callback("pick_columns:1")
    token = (await setup_data(h))["column_token"]
    await h.callback(f"colpick:{token}:2", 123)
    assert h.replies()[-1] == "Только для админа"
    await h.callback(f"colpick:{token}:2")
    assert "хотя бы один" in h.replies()[-1]
    assert (await setup_data(h))["column_selected"] == [2]
    h.db.execute("DELETE FROM sheets WHERE id=1")
    await h.callback(f"colpick:{token}:done")
    assert "больше недоступны" in h.replies()[-1]
    await h.callback("pick_columns:1")
    assert h.replies()[-1] == "Лист больше недоступен"


async def test_setup_column_picker_paging_and_save(dialog_bot, monkeypatch):
    import json

    h = dialog_bot
    rows = [["ФИО", *[f"С{i}" for i in range(1, 13)]], ["Иванов Иван", *["5"] * 12]]
    monkeypatch.setattr("gradebot.bot.fetch_sheet", AsyncMock(return_value=rows))
    await h.send("/add", 999)
    await h.send("https://docs.google.com/spreadsheets/d/id/edit", 999)
    await h.send("Матан", 999)
    setup_token = (await setup_data(h))["setup_token"]
    await h.callback(f"tabs:{setup_token}:next")
    await h.callback(f"sheetcfg:{setup_token}:0:details")
    token = (await setup_data(h))["column_token"]
    await h.callback(f"colpick:{token}:2")
    await h.callback(f"colpick:{token}:page1")
    assert "С9" in h.replies()[-1] and "С1\n" not in h.replies()[-1]
    await h.callback(f"colpick:{token}:13")
    await h.callback(f"colpick:{token}:done")
    assert (await setup_data(h))["grade_columns"] == list(range(3, 13))
    await h.callback(f"colpick:{token}:all")
    assert h.replies()[-1] == "Настройка устарела"
    await h.callback(f"sheetcfg:{setup_token}:0:next")
    await h.callback(f"worksave:{setup_token}")
    assert json.loads(h.db.all("SELECT parser_options FROM sheets")[0][0])["grade_columns"] == list(range(3, 13))


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
    await h.send(base + "10", 999)
    await h.send("Матан", 999)
    data = await setup_data(h)
    token = data["setup_token"]
    assert data["selected"] == [0, 1]
    picker = h.bot.session.await_args.args[1]
    assert picker.text == "Выбери листы для парсинга"
    assert picker.reply_markup.inline_keyboard[0][0].text == "✅ Квизы"
    assert picker.reply_markup.inline_keyboard[-1][0].text == "Далее"
    await h.callback(f"tabs:{token}:all", 123)
    assert (await setup_data(h))["selected"] == [0, 1]
    await h.callback(f"tabs:{token}:all")
    assert (await setup_data(h))["selected"] == []
    await h.callback(f"tabs:{token}:next")
    assert await h.state(999) == Dialog.sheet_selection.state
    await h.callback(f"tabs:{token}:all")
    await h.callback(f"tabs:{token}:1")
    picker = h.bot.session.await_args.args[1].reply_markup
    assert picker.inline_keyboard[0][0].text == "✅ Квизы"
    assert picker.inline_keyboard[1][0].text == "Семинары"
    await h.callback(f"tabs:{token}:next")
    await h.send("1-2 A", 999)
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


async def test_add_tab_to_existing_workbook_uses_only_new_tabs(dialog_bot, monkeypatch):
    from gradebot.bot import Dialog

    h = dialog_bot
    base = "https://docs.google.com/spreadsheets/d/existing/edit#gid="
    tabs = [{"name": "Первый", "url": base + "1"}, {"name": "Второй", "url": base + "2"}]
    original = h.db.add_sheet("Матан", tabs[0]["url"], 1, (1,), 999, "approved")
    monkeypatch.setattr("gradebot.bot.discover_sheets", AsyncMock(return_value=tabs))
    await h.send("/addtab", 999)
    assert await h.state(999) == Dialog.addtab_id.state
    assert f"#{original}: Матан" in h.replies()[-1]
    assert h.bot.session.await_args.args[1].reply_markup is None
    await h.send(str(original), 999)
    data = await setup_data(h)
    assert data["tabs"] == [tabs[1]]
    assert data["selected"] == [0]
    await h.callback(f"tabs:{data['setup_token']}:next")
    assert await h.state(999) == Dialog.sheet_columns.state
    await h.send("готово", 999)
    await h.callback(f"worksave:{data['setup_token']}")
    rows = h.db.all("SELECT id,title,url FROM sheets ORDER BY id")
    assert [(row["title"], row["url"]) for row in rows] == [
        ("Матан", tabs[0]["url"]), ("Матан · Второй", tabs[1]["url"])
    ]
    assert rows[0]["id"] == original
    stored = h.db.all("SELECT workbook_id,tab_name FROM sheets ORDER BY id")
    assert stored[0]["workbook_id"] == stored[1]["workbook_id"]
    assert [row["tab_name"] for row in stored] == ["Первый", "Второй"]
    assert len(h.db.all("SELECT * FROM sheet_cache")) == 1
    assert json.loads(h.db.all("SELECT tabs FROM workbook_watch")[0][0]) == tabs
    await h.send("/tables", 999)
    assert ">Первый</a>" in h.replies()[-1] and ">Второй</a>" in h.replies()[-1]
    await h.send("/addtab", 999)
    await h.send(str(original), 999)
    assert h.replies()[-1] == "Новых листов в этой таблице пока нет"
    assert await h.state(999) is None


async def test_add_tab_rechecks_duplicate_before_saving(dialog_bot, monkeypatch):
    h = dialog_bot
    base = "https://docs.google.com/spreadsheets/d/existing/edit#gid="
    tabs = [{"name": "Первый", "url": base + "1"}, {"name": "Второй", "url": base + "2"}]
    original = h.db.add_sheet("Матан", tabs[0]["url"], 1, (1,), 999, "approved")
    monkeypatch.setattr("gradebot.bot.discover_sheets", AsyncMock(return_value=tabs))
    await h.send("/addtab", 999)
    await h.send(str(original), 999)
    token = (await setup_data(h))["setup_token"]
    await h.callback(f"tabs:{token}:next")
    await h.send("готово", 999)
    h.db.add_sheet("Другое имя", tabs[1]["url"], 1, (1,), 999, "approved")
    await h.callback(f"worksave:{token}")
    assert len(h.db.all("SELECT * FROM sheets")) == 2
    assert not h.db.all("SELECT * FROM sheet_cache")


async def test_workbook_save_rolls_back_every_sheet_on_conflict(dialog_bot, monkeypatch):
    h = dialog_bot
    h.db.register(123, "Иванов Иван")
    tabs = [{"name": "Первый", "url": "https://docs.google.com/spreadsheets/d/book/edit#gid=1"},
            {"name": "Второй", "url": "https://docs.google.com/spreadsheets/d/book/edit#gid=2"}]
    monkeypatch.setattr("gradebot.bot.discover_sheets", AsyncMock(return_value=tabs))
    await h.send("/add", 999)
    await h.send(tabs[0]["url"], 999)
    await h.send("Матан", 999)
    token = (await setup_data(h))["setup_token"]
    await h.callback(f"tabs:{token}:next")
    for _ in tabs:
        await h.send("1 A", 999)
        await h.send("готово", 999)
    h.db.add_sheet("Матан · Второй", "https://docs.google.com/spreadsheets/d/conflict/edit", 1, (1,),
                   999, "approved")
    await h.callback(f"worksave:{token}")
    assert [row["title"] for row in h.db.all("SELECT * FROM sheets")] == ["Матан · Второй"]
    assert not h.db.all("SELECT * FROM sheet_cache")
    assert not h.db.all("SELECT * FROM outbox")


async def test_proposal_picker_selection_survives_until_admin_configuration(dialog_bot, monkeypatch):
    import json

    from aiogram.methods import DeleteMessage

    h = dialog_bot
    h.db.register(123, "Иванов Иван")
    tabs = [{"name": "Первый", "url": "https://docs.google.com/spreadsheets/d/book/edit#gid=1"},
            {"name": "Второй", "url": "https://docs.google.com/spreadsheets/d/book/edit#gid=2"}]
    monkeypatch.setattr("gradebot.bot.discover_sheets", AsyncMock(return_value=tabs))
    await h.send("/suggest", 123)
    await h.send(tabs[0]["url"], 123)
    await h.send("Матан", 123)
    row = h.db.all("SELECT * FROM sheets")[0]
    assert json.loads(row["parser_options"])["selected"] == [0, 1]
    await h.callback(f"ptabs:{row['id']}:0")
    picker = h.bot.session.await_args.args[1].reply_markup
    assert picker.inline_keyboard[0][0].text == "Первый"
    assert picker.inline_keyboard[1][0].text == "✅ Второй"
    assert picker.inline_keyboard[-1][0].text == "Далее"
    await h.callback(f"configure:{row['id']}")
    assert any(isinstance(call.args[1], DeleteMessage) for call in h.bot.session.await_args_list)
    data = await setup_data(h)
    assert data["selected"] == [1]
    await h.send("1 A", 999)
    await h.send("готово", 999)
    await h.callback(f"worksave:{data['setup_token']}")
    saved = h.db.all("SELECT * FROM sheets")[0]
    assert saved["title"] == "Матан · Второй" and saved["status"] == "approved"
    assert saved["url"] == tabs[1]["url"]


async def test_compact_auto_setup_buttons_and_stale_sheet_callback(dialog_bot, monkeypatch):
    from aiogram.methods import DeleteMessage

    from gradebot.bot import Dialog

    h = dialog_bot
    h.db.register(999, "Иванов Иван")
    tabs = [{"name": "Семинары", "url": "https://docs.google.com/spreadsheets/d/id/edit#gid=0"},
            {"name": "Квизы", "url": "https://docs.google.com/spreadsheets/d/id/edit#gid=1"}]
    rows = [["№", "ФИО", "03.09.2026", "", "Итоги"],
            ["", "", "Семинар", "", "Текущая оценка"],
            ["", "", "Отсутствие", "Выход к доске", ""],
            ["1", "Боженко Станислав Павлович", "0", "2", "0,0"]]
    monkeypatch.setattr("gradebot.bot.discover_sheets", AsyncMock(return_value=tabs))
    monkeypatch.setattr("gradebot.bot.fetch_sheet", AsyncMock(return_value=rows))
    await h.send("/add", 999)
    await h.send(tabs[0]["url"], 999)
    await h.send("Матан", 999)
    token = (await setup_data(h))["setup_token"]
    h.bot.session.reset_mock()
    await h.callback(f"tabs:{token}:next")
    assert await h.state(999) == Dialog.sheet_columns.state
    assert any(isinstance(call.args[1], DeleteMessage) for call in h.bot.session.await_args_list)
    replies = [reply for reply in h.replies() if reply]
    assert replies[0] == "Секундочку"
    assert len(replies[-1]) < 650
    assert "Столбец ФИО: B\nЗаголовки: строки 1–3" in replies[-1]
    assert "Боженко" not in replies[-1]
    assert "- 03.09.2026 → Семинар → Отсутствие: 0" in replies[-1]
    assert "- 03.09.2026 → Семинар → Выход к доске: 2" in replies[-1]
    assert "- Итоги → Текущая оценка: 0,0" in replies[-1]
    assert (await setup_data(h))["grade_columns"] == (3, 4, 5)
    await h.callback(f"sheetcfg:{token}:0:details")
    assert any("C:" in reply for reply in h.replies() if reply)
    column_token = (await setup_data(h))["column_token"]
    await h.callback(f"colpick:{column_token}:3")
    await h.callback(f"colpick:{column_token}:4")
    await h.callback(f"colpick:{column_token}:done")
    assert sum(isinstance(call.args[1], DeleteMessage) for call in h.bot.session.await_args_list) >= 3
    assert (await setup_data(h))["grade_columns"] == [5]
    assert "- Итоги → Текущая оценка: 0,0" in h.replies()[-1]
    assert "Выход к доске" not in h.replies()[-1]
    await h.callback(f"sheetcfg:{token}:0:next")
    assert (await setup_data(h))["current_index"] == 1
    await h.callback(f"sheetcfg:{token}:0:skip")
    assert (await setup_data(h))["selected"] == [0, 1]
    await h.callback(f"sheetcfg:{token}:1:layout")
    assert await h.state(999) == Dialog.sheet_layout.state
    await h.send("1-3 B", 999)
    await h.callback(f"sheetcfg:{token}:1:next")
    assert await h.state(999) == Dialog.workbook_confirm.state
    ready = h.bot.session.await_args.args[1]
    assert ready.text.startswith("Готово к сохранению:")
    assert "Матан. Семинары:" in ready.text and "Матан. Квизы:" in ready.text
    assert " · " not in ready.text
    assert ready.reply_markup.inline_keyboard[0][0].text == "Сохранить"
    await h.callback(f"worksave:{token}")
    assert len(h.db.all("SELECT * FROM sheets WHERE status='approved'")) == 2
    alerts = h.db.all("SELECT text FROM outbox WHERE kind='table'")
    assert len(alerts) == 1
    assert alerts[0]["text"].count("<a href=") == 2


async def test_auto_setup_falls_back_to_short_manual_prompt(dialog_bot, monkeypatch):
    from gradebot.bot import Dialog

    h = dialog_bot
    rows = [["Студент", "Семинар"], ["Иванов Иван", "5"]]
    monkeypatch.setattr("gradebot.bot.fetch_sheet", AsyncMock(return_value=rows))
    await h.send("/add", 999)
    await h.send("https://docs.google.com/spreadsheets/d/id/edit", 999)
    await h.send("Матан", 999)
    token = (await setup_data(h))["setup_token"]
    await h.callback(f"tabs:{token}:next")
    assert await h.state(999) == Dialog.sheet_layout.state
    assert len(h.replies()[-1]) < 600
    assert "Иванов Иван" not in h.replies()[-1]
    assert "Для листа «Лист 1» не удалось определить структуру автоматически" in h.replies()[-1]
    assert "<code>1-3 A</code>" in h.replies()[-1]
    assert "Строка 1:" not in h.replies()[-1]
    for old_format in ("1\nA", "1 | A"):
        await h.send(old_format, 999)
        assert await h.state(999) == Dialog.sheet_layout.state
        assert "<code>1-3 A</code>" in h.replies()[-1]
    await h.send("1 A", 999)
    await h.callback(f"sheetcfg:{token}:0:next")
    assert await h.state(999) == Dialog.workbook_confirm.state


@pytest.mark.parametrize("student_values,expected", [
    (["", "0"], ["- С1: пусто", "- С2: 0", "- С3: пусто"]),
    (["5\n баллов", "x" * 100, "4"], ["- С1: 5 баллов", "- С2: " + "x" * 60 + "…", "- С3: 4"]),
])
async def test_setup_preview_uses_one_student_row_and_bounds_values(
    dialog_bot, monkeypatch, student_values, expected,
):
    h = dialog_bot
    rows = [["Фамилия", "Имя", "С1", "С2", "С3"],
            ["", "", "служебное значение"], ["Иванов", "Иван", *student_values]]
    monkeypatch.setattr("gradebot.bot.fetch_sheet", AsyncMock(return_value=rows))
    await h.send("/add", 999)
    await h.send("https://docs.google.com/spreadsheets/d/id/edit", 999)
    await h.send("Матан", 999)
    token = (await setup_data(h))["setup_token"]
    await h.callback(f"tabs:{token}:next")
    await h.send("1 A:B", 999)
    preview = h.replies()[-1]
    assert all(line in preview for line in expected)
    assert "Иванов" not in preview and "служебное значение" not in preview

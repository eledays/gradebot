import json
import sqlite3
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from gradebot.bot import create_router
from gradebot.db import Database
from gradebot.monitor import Monitor
from gradebot.presentation import create_view, view_markup
from gradebot.sheets import Sheet


@pytest.fixture
def app(tmp_path):
    db = Database(str(tmp_path / "access.sqlite3"))
    db.execute("INSERT INTO settings VALUES('admin','999')")
    bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk")
    bot.session = AsyncMock()
    monitor = Monitor(db, None, bot)
    dispatcher = Dispatcher(events_isolation=SimpleEventIsolation())
    dispatcher.include_router(create_router(db, monitor, "group_admin"))

    class App:
        async def send(self, text=None, user_id=999, username=None, shared=None, contact=None):
            message = Message(
                message_id=1,
                date=datetime.now(UTC),
                chat=Chat(id=user_id, type="private"),
                from_user=User(id=user_id, is_bot=False, first_name="Test", username=username),
                text=text,
                users_shared=shared,
                contact=contact,
            )
            await dispatcher.feed_update(bot, Update(update_id=1, message=message))

        async def click(self, value, user_id=999):
            callback = CallbackQuery(
                id="test",
                from_user=User(id=user_id, is_bot=False, first_name="Test"),
                chat_instance="test",
                data=value,
                message=Message(
                    message_id=1,
                    date=datetime.now(UTC),
                    chat=Chat(id=user_id, type="private"),
                    text="Подтверждение",
                ),
            )
            await dispatcher.feed_update(bot, Update(update_id=1, callback_query=callback))

        async def state(self, user_id=999):
            return await self.context(user_id).get_state()

        def context(self, user_id=999):
            return dispatcher.fsm.get_context(bot=bot, chat_id=user_id, user_id=user_id)

        def replies(self):
            return [
                call.args[1].text for call in bot.session.await_args_list if hasattr(call.args[1], "text")
            ]

    instance = App()
    instance.db, instance.bot, instance.monitor = db, bot, monitor
    yield instance
    db.close()


@pytest.mark.parametrize("text", ["/grades", "/tables", "/help", "/suggest", "/members", "/name", "Мои оценки"])
async def test_outsider_cannot_read_private_data(app, text):
    await app.send(text, 123, "ivanov")
    assert "Доступ закрыт" in app.replies()[-1]
    assert not app.db.all("SELECT * FROM users")


async def test_invitation_registration_and_safe_welcome(app):
    token = app.db.create_invitation()
    await app.send(f"/start {token}", 123, "ivanov")
    assert "отправь ФИО" in app.replies()[-1]
    await app.send("Иванов <Иван>", 123)
    assert "Нужно 2–3 полных слова" in app.replies()[-1]
    await app.send("Иванов Иван", 123)
    assert app.db.authorized(123)
    assert app.db.user_name(123) == "Иванов Иван"
    assert any("Добро пожаловать, Иван!" in reply for reply in app.replies())


async def test_invalid_invitation_and_cancel(app):
    await app.send("/start invalid", 123)
    assert "Ссылка недействительна" in app.replies()[-1]
    token = app.db.create_invitation()
    await app.send(f"/start {token}", 123)
    assert app.db.has_join_draft(123)
    await app.send("/cancel", 123)
    assert not app.db.has_join_draft(123)
    assert not app.db.authorized(123)


async def test_start_without_invitation_explains_restricted_access(app):
    await app.send("/start", 123)
    assert "Доступ к боту ограничен" in app.replies()[-1]
    assert "@group_admin" in app.replies()[-1]
    assert not app.db.authorized(123)
    assert not app.db.has_join_draft(123)


async def test_name_request_shows_admin_profile_and_both_names(app):
    app.db.register(123, "Иванов Иван")
    await app.send("/name", 123, username="ivanov")
    await app.send("Петров Петр", 123, username="ivanov")
    method = app.bot.session.await_args.args[1]
    assert method.chat_id == 999
    assert method.parse_mode == "HTML"
    assert "Иванов Иван" in method.text
    assert "Петров Петр" in method.text
    assert "@ivanov" in method.text
    assert "tg://user?id=123" in method.text
    assert method.reply_markup.inline_keyboard[0][0].callback_data == "name_approve:123"


async def test_revocation_clears_private_data_and_blocks_old_pages(app):
    app.db.register(123, "Иванов Иван")
    with app.db.conn:
        view, _ = create_view(app.db, 123, ["old grades", "x" * 3000, "y" * 3000])
        app.db.queue(123, ["pending grades"])
    await app.send("/revoke")
    await app.send("123")
    data = await app.context().get_data()
    assert app.db.authorized(123)
    await app.click(f"revoke:{data['token']}")
    assert not app.db.authorized(123)
    assert not app.db.all("SELECT * FROM users")
    assert not app.db.all("SELECT * FROM views WHERE owner=123")
    assert not app.db.all("SELECT * FROM outbox WHERE user_id=123")
    app.bot.session.reset_mock()
    await app.click(f"page:{view}:1", 123)
    assert not any(
        call.args[1].__class__.__name__ == "EditMessageText" for call in app.bot.session.await_args_list
    )


async def test_notification_pause_does_not_send_accumulated_changes(app):
    app.db.register(123, "Иванов Иван")
    app.db.add_sheet(
        "Матан",
        "https://docs.google.com/spreadsheets/d/id/edit",
        1,
        (1,),
        999,
        "approved",
        parsed=Sheet([["ФИО", "С1"], ["Иванов Иван", "5"]]),
    )
    app.db.snapshot(123, 1, {"С1": "5"}, [])
    app.monitor.bot.send_message = AsyncMock()
    app.monitor.fetch = AsyncMock(return_value=Sheet([["ФИО", "С1"], ["Иванов Иван", "4"]]))
    await app.send("Уведомления", 123)
    await app.monitor.scan()
    app.monitor.bot.send_message.assert_not_called()
    assert json.loads(app.db.all("SELECT grades FROM snapshots")[0][0]) == {"С1": "4"}
    await app.send("Уведомления", 123)
    await app.monitor.scan()
    app.monitor.bot.send_message.assert_not_called()


async def test_repeat_start_does_not_consume_new_grade_notifications(app):
    app.db.grant_members([(123, "ivanov", "Иванов Иван")])
    app.db.add_sheet(
        "Матан",
        "https://docs.google.com/spreadsheets/d/id/edit",
        1,
        (1,),
        999,
        "approved",
        parsed=Sheet([["ФИО", "С1"], ["Иванов Иван", "5"]]),
    )
    await app.send("/start", 123)
    with app.db.conn:
        app.db.cache(1, Sheet([["ФИО", "С1"], ["Иванов Иван", "4"]]))
    await app.send("/start", 123)
    assert json.loads(app.db.all("SELECT grades FROM snapshots")[0][0]) == {"С1": "5"}


def test_old_moderation_buttons_never_target_new_sheet_after_restart(tmp_path):
    path = str(tmp_path / "ids.sqlite3")
    db = Database(path)
    db.execute("INSERT INTO settings VALUES('admin','999')")
    first = db.add_sheet(
        "Старая", "https://docs.google.com/spreadsheets/d/first/edit", 1, (1,), 123, "pending", notify=True
    )
    view = db.all("SELECT view_id FROM outbox")[0][0]
    db.execute("DELETE FROM sheets WHERE id=?", (first,))
    db.close()
    db = Database(path)
    try:
        second = db.add_sheet(
            "Новая", "https://docs.google.com/spreadsheets/d/second/edit", 1, (1,), 123, "pending"
        )
        assert second > first
        assert view_markup(db, view) is None
    finally:
        db.close()


def test_legacy_self_registered_users_do_not_receive_access(tmp_path):
    path = str(tmp_path / "legacy.sqlite3")
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE users(id INTEGER PRIMARY KEY,name TEXT NOT NULL); "
        "INSERT INTO users VALUES(123,'Иванов Иван');"
    )
    connection.close()
    db = Database(path)
    try:
        assert not db.authorized(123)
        assert not db.activate(123)
        assert db.all("SELECT name FROM users")[0][0] == "Иванов Иван"
    finally:
        db.close()


async def test_deleting_last_sheet_offers_to_remove_or_keep_workbook(app):
    sheet_id = app.db.add_sheet(
        "Матан", "https://docs.google.com/spreadsheets/d/id/edit", 1, (1,), 999, "approved"
    )
    await app.send("Удалить лист")
    method = app.bot.session.await_args.args[1]
    assert f"#{sheet_id}: Матан" in method.text
    assert method.reply_markup is None
    await app.send(str(sheet_id))
    assert app.db.all("SELECT * FROM sheets")
    data = await app.context().get_data()
    await app.click(f"remove:{data['token']}")
    assert not app.db.all("SELECT * FROM sheets")
    assert app.db.all("SELECT * FROM workbooks")
    assert "Последний лист удалён" in app.replies()[-1]
    data = await app.context().get_data()
    await app.click(f"dismiss:{data['token']}")
    assert "Таблица оставлена" in app.replies()[-1]
    assert app.db.all("SELECT * FROM workbooks")
    await app.send("/addtab")
    assert "#1: Матан" in app.replies()[-1]
    await app.send("Удалить лист")
    assert app.replies()[-1] == "Листов пока нет"


async def test_last_sheet_offer_can_remove_empty_workbook(app):
    sheet_id = app.db.add_sheet(
        "Матан", "https://docs.google.com/spreadsheets/d/id/edit", 1, (1,), 999, "approved"
    )
    await app.send("/remove")
    await app.send(str(sheet_id))
    data = await app.context().get_data()
    await app.click(f"remove:{data['token']}")
    data = await app.context().get_data()
    await app.click(f"remove_table:{data['token']}")
    assert not app.db.all("SELECT * FROM sheets")
    assert not app.db.all("SELECT * FROM workbooks")
    assert "Таблица удалена" in app.replies()[-1]


async def test_remove_table_deletes_all_its_sheets_and_related_rows(app):
    app.db.register(123, "Иванов Иван")
    base = "https://docs.google.com/spreadsheets/d/algebra/edit#gid="
    first = app.db.add_sheet("Алгебра & КР - Первый", base + "1", 1, (1,), 999, "approved",
                             parsed=Sheet([["ФИО", "Оценка"], ["Иванов Иван", "5"]]))
    second = app.db.add_sheet("Алгебра & КР - Второй", base + "2", 1, (1,), 999, "approved")
    other = app.db.add_sheet("История", "https://docs.google.com/spreadsheets/d/history/edit",
                             1, (1,), 999, "approved")
    workbook_id = app.db.all("SELECT workbook_id FROM sheets WHERE id=?", (first,))[0][0]
    app.db.snapshot(123, first, {"Оценка": "5"}, [])
    with app.db.conn:
        app.db.remember_workbook(base + "1", [
            {"name": "Первый", "url": base + "1"}, {"name": "Второй", "url": base + "2"}
        ])
    await app.send("/removetable")
    preview = app.replies()[-1]
    assert f"#{workbook_id}: Алгебра &amp; КР" in preview
    assert "Первый" not in preview and "Второй" not in preview
    await app.send(str(workbook_id))
    prompt = app.bot.session.await_args.args[1]
    assert prompt.parse_mode == "HTML"
    assert "Алгебра &amp; КР" in prompt.text and "листов: 2" in prompt.text
    data = await app.context().get_data()
    await app.click(f"remove_table:{data['token']}")
    assert not app.db.all("SELECT * FROM workbooks WHERE id=?", (workbook_id,))
    assert not app.db.all("SELECT * FROM sheets WHERE id IN (?,?)", (first, second))
    assert app.db.all("SELECT id FROM sheets WHERE id=?", (other,))
    assert not app.db.all("SELECT * FROM snapshots WHERE sheet_id=?", (first,))
    assert not app.db.all("SELECT * FROM sheet_cache WHERE sheet_id=?", (first,))
    assert not app.db.all("SELECT * FROM workbook_watch WHERE url LIKE '%algebra%'")
    assert app.db.all("PRAGMA foreign_key_check") == []
    assert app.replies()[-1] == "Таблица удалена. Удалено листов: 2"


async def test_remove_table_confirmation_rejects_changed_sheet_set(app):
    first = app.db.add_sheet("Матан", "https://docs.google.com/spreadsheets/d/book/edit#gid=1",
                             1, (1,), 999, "approved")
    workbook_id = app.db.all("SELECT workbook_id FROM sheets WHERE id=?", (first,))[0][0]
    await app.send("/removetable")
    await app.send(str(workbook_id))
    data = await app.context().get_data()
    app.db.add_sheet("Матан - Второй", "https://docs.google.com/spreadsheets/d/book/edit#gid=2",
                     1, (1,), 999, "approved")
    await app.click(f"remove_table:{data['token']}")
    assert "Состав таблицы изменился" in app.replies()[-1]
    assert len(app.db.all("SELECT id FROM sheets WHERE workbook_id=?", (workbook_id,))) == 2
    assert app.db.all("SELECT id FROM workbooks WHERE id=?", (workbook_id,))


async def test_removing_one_of_two_sheets_keeps_workbook_without_offer(app):
    first = app.db.add_sheet("Алгебра - Первый",
                             "https://docs.google.com/spreadsheets/d/book/edit#gid=1",
                             1, (1,), 999, "approved")
    second = app.db.add_sheet("Алгебра - Второй",
                              "https://docs.google.com/spreadsheets/d/book/edit#gid=2",
                              1, (1,), 999, "approved")
    await app.send("/remove")
    await app.send(str(first))
    data = await app.context().get_data()
    await app.click(f"remove:{data['token']}")
    assert app.replies()[-1] == "Лист удалён"
    assert await app.state() is None
    assert app.db.all("SELECT id FROM sheets WHERE id=?", (second,))
    assert app.db.all("SELECT id FROM workbooks")


async def test_admin_can_revoke_by_id_and_outsider_cannot(app):
    app.db.register(123, "Иванов Иван")
    await app.send("Пользователи")
    method = app.bot.session.await_args.args[1]
    assert method.reply_markup is None
    await app.send("/revoke", 123)
    assert app.db.authorized(123)
    assert app.replies()[-1] == "Эта команда недоступна"
    await app.send("/revoke")
    assert "Отправь ID пользователя" in app.replies()[-1]
    await app.send("не число")
    assert "числовой ID" in app.replies()[-1]
    await app.send("777")
    assert "Пользователь не найден" in app.replies()[-1]
    await app.send("123")
    assert app.db.authorized(123)
    data = await app.context().get_data()
    await app.click(f"revoke:{data['token']}")
    assert not app.db.authorized(123)

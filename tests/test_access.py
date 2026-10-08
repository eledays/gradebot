import json
import sqlite3
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import CallbackQuery, Chat, Contact, Message, SharedUser, Update, User, UsersShared

from gradebot.bot import Dialog, create_router
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

        async def add_contact(self, user_id=123, name="Иванов Иван"):
            await self.send(contact=Contact(user_id=user_id, phone_number="+70000000000", first_name="Test"))
            await self.send(name)

        async def confirm(self):
            await self.send("Сохранить всех")

    instance = App()
    instance.db, instance.bot, instance.monitor = db, bot, monitor
    yield instance
    db.close()


@pytest.mark.parametrize(
    "text",
    ["/start", "/grades", "/tables", "/help", "/suggest", "/access", "/members", "/name", "Мои оценки"],
)
async def test_outsider_cannot_read_or_register(app, text):
    await app.send(text, 123, "ivanov")
    assert "Доступ закрыт" in app.replies()[-1]
    assert not app.db.all("SELECT * FROM users")
    assert not app.db.all("SELECT * FROM members")


async def test_contacts_accumulate_and_access_is_pinned_to_id(app):
    await app.send("/access")
    await app.add_contact(123, "Иванов Иван")
    await app.add_contact(456, "Петров Петр")
    assert await app.state() == Dialog.access_input.state
    assert not app.db.all("SELECT * FROM members")
    await app.confirm()
    assert len(app.db.all("SELECT * FROM members")) == 2
    assert not app.db.all("SELECT * FROM users")
    await app.send("/start", 123, "newusername")
    assert "Привет, Иванов Иван!" in app.replies()[-2]
    await app.send("/start", 777, "ivanov")
    assert "Доступ закрыт" in app.replies()[-1]
    await app.send("/start", 123)
    assert "Привет, Иванов Иван!" in app.replies()[-2]


async def test_cancel_discards_entire_draft(app):
    await app.send("/access")
    await app.add_contact()
    await app.send("Отмена")
    await app.confirm()
    assert not app.db.authorized(123)
    assert await app.state() is None


async def test_repeated_contact_updates_single_draft_row(app):
    await app.send("/access")
    await app.add_contact(123, "Иванов Иван")
    await app.add_contact(123, "Иванов Иван Иванович")
    await app.confirm()
    rows = app.db.all("SELECT * FROM members")
    assert len(rows) == 1
    assert rows[0]["name"] == "Иванов Иван Иванович"


async def test_non_admin_cannot_save_draft(app):
    app.db.grant_members([(456, "petrov", "Петров Петр")])
    await app.send("/access")
    await app.add_contact()
    await app.send("Сохранить всех", 456)
    assert not app.db.authorized(123)
    await app.confirm()
    assert app.db.authorized(123)


async def test_telegram_user_picker_saves_id_before_first_start(app):
    await app.send("Добавить пользователей")
    data = await app.context().get_data()
    await app.send(shared=UsersShared(request_id=data["request_id"], users=[SharedUser(user_id=123)]))
    assert await app.state() == Dialog.member_name.state
    await app.send("Иванов Иван")
    await app.confirm()
    assert app.db.authorized(123)
    assert not app.db.all("SELECT * FROM users")
    await app.send("/start", 123)
    assert app.db.all("SELECT name FROM users")[0]["name"] == "Иванов Иван"


async def test_old_picker_request_is_rejected(app):
    await app.send("/access")
    data = await app.context().get_data()
    await app.send(shared=UsersShared(request_id=data["request_id"] + 1, users=[SharedUser(user_id=123)]))
    assert await app.state() == Dialog.access_input.state
    assert not app.db.authorized(123)


async def test_admin_can_switch_from_table_input_using_menu(app):
    await app.send("Добавить таблицу")
    assert await app.state() == Dialog.title.state
    await app.send("Добавить пользователей")
    assert await app.state() == Dialog.access_input.state
    assert not app.db.all("SELECT * FROM sheets")


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


async def test_admin_can_delete_table_by_button_with_confirmation(app):
    sheet_id = app.db.add_sheet(
        "Матан", "https://docs.google.com/spreadsheets/d/id/edit", 1, (1,), 999, "approved"
    )
    await app.send("Удалить таблицу")
    method = app.bot.session.await_args.args[1]
    assert method.reply_markup.inline_keyboard[0][0].callback_data == f"pick_remove:{sheet_id}"
    await app.click(f"pick_remove:{sheet_id}")
    assert app.db.all("SELECT * FROM sheets")
    data = await app.context().get_data()
    await app.click(f"remove:{data['token']}")
    assert not app.db.all("SELECT * FROM sheets")
    await app.click(f"pick_remove:{sheet_id}")
    assert "не найдена" in app.replies()[-1]


async def test_admin_can_revoke_by_button_and_outsider_cannot(app):
    app.db.register(123, "Иванов Иван")
    await app.send("Пользователи")
    method = app.bot.session.await_args.args[1]
    assert method.reply_markup.inline_keyboard[0][0].callback_data == "pick_revoke:123"
    await app.click("pick_revoke:123", 123)
    assert app.db.authorized(123)
    await app.click("pick_revoke:123")
    assert app.db.authorized(123)
    data = await app.context().get_data()
    await app.click(f"revoke:{data['token']}")
    assert not app.db.authorized(123)


async def test_username_import_is_not_supported(app):
    await app.send("/access")
    await app.send("@ivanov | Иванов Иван")
    assert app.replies()[-1] == "Отправь контакт"
    assert not app.db.all("SELECT * FROM members")


async def test_contact_without_id_preserves_draft(app):
    await app.send("/access")
    await app.add_contact()
    await app.send(contact=Contact(phone_number="+70000000000", first_name="Unknown"))
    assert app.replies()[-1] == "В контакте нет Telegram ID"
    assert len((await app.context().get_data())["entries"]) == 1
    await app.confirm()
    assert app.db.authorized(123)


async def test_second_contact_does_not_overwrite_unfinished_name(app):
    await app.send("/access")
    await app.send(contact=Contact(user_id=123, phone_number="+70000000000", first_name="First"))
    await app.send(contact=Contact(user_id=456, phone_number="+70000000001", first_name="Second"))
    assert app.replies()[-1] == "Контактов в очереди: 2"
    await app.send("Иванов Иван")
    assert await app.state() == Dialog.member_name.state
    await app.send("Петров Петр")
    await app.confirm()
    assert app.db.authorized(123)
    assert app.db.authorized(456)


async def test_save_does_not_drop_unfinished_contact(app):
    await app.send("/access")
    await app.add_contact()
    await app.send(contact=Contact(user_id=456, phone_number="+70000000000", first_name="Test"))
    await app.confirm()
    assert app.replies()[-1] == "Сначала отправь ФИО"
    assert not app.db.all("SELECT * FROM members")
    await app.send("Петров Петр")
    await app.confirm()
    assert len(app.db.all("SELECT * FROM members")) == 2


async def test_invalid_name_keeps_contact_and_draft(app):
    await app.send("/access")
    await app.add_contact()
    await app.send(contact=Contact(user_id=456, phone_number="+70000000000", first_name="Test"))
    await app.send("Петр")
    assert await app.state() == Dialog.member_name.state
    await app.send("Петров Петр")
    await app.confirm()
    assert len(app.db.all("SELECT * FROM members")) == 2


async def test_contact_from_non_admin_is_not_added(app):
    app.db.grant_members([(456, "", "Петров Петр")])
    await app.send("/access")
    await app.send(contact=Contact(user_id=123, phone_number="+70000000000", first_name="Test"), user_id=456)
    assert not app.db.authorized(123)
    assert not (await app.context().get_data())["entries"]


async def test_contacts_can_arrive_as_batch_before_names(app):
    await app.send("/access")
    for user_id in (123, 456, 789):
        await app.send(
            contact=Contact(user_id=user_id, phone_number="+70000000000", first_name=f"User{user_id}")
        )
    assert not app.db.all("SELECT * FROM members")
    assert len((await app.context().get_data())["queue"]) == 2
    for name in ("Иванов Иван", "Петров Петр", "Сидоров Сидор"):
        await app.send(name)
    assert await app.state() == Dialog.access_input.state
    await app.confirm()
    assert [(row["id"], row["name"]) for row in app.db.all("SELECT * FROM members ORDER BY id")] == [
        (123, "Иванов Иван"),
        (456, "Петров Петр"),
        (789, "Сидоров Сидор"),
    ]


async def test_duplicate_queued_contact_does_not_request_name_twice(app):
    await app.send("/access")
    for user_id in (123, 456, 456):
        await app.send(contact=Contact(user_id=user_id, phone_number="+70000000000", first_name="User"))
    assert app.replies()[-1] == "Контакт уже в очереди"
    await app.send("Иванов Иван")
    await app.send("Петров Петр")
    await app.confirm()
    assert len(app.db.all("SELECT * FROM members")) == 2


async def test_empty_list_is_not_saved(app):
    await app.send("/access")
    await app.confirm()
    assert app.replies()[-1] == "Список пуст"
    assert await app.state() == Dialog.access_input.state
    assert not app.db.all("SELECT * FROM members")

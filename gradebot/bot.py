import asyncio
import contextlib
import json
import logging
import secrets
import sqlite3
import time
from html import escape

import aiohttp
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestUsers,
    LinkPreviewOptions,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from .db import DuplicateTitleError
from .logging_setup import audit
from .monitor import Monitor, cache_caption, format_grades, subject_pages
from .network import network_sessions
from .presentation import bold, create_view, safe_lines, send_view, table_link, view_markup
from .sheets import (
    Sheet,
    SheetError,
    column_letter,
    detect_layout,
    discover_sheets,
    export_url,
    fetch_sheet,
    parse_columns,
    sheet_url,
    valid_name,
)


def help_message(interval, contact_username, *, admin=False, name=None, fetched_at=None):
    if interval % 3600 == 0:
        period = f"{interval // 3600} ч"
    elif interval % 60 == 0:
        period = f"{interval // 60} мин"
    else:
        period = f"{interval} сек"
    greeting = f"Рад тебя видеть, {escape(name)}" if name else "Рад тебя видеть"
    text = (
        f"{greeting}\n\n"
        "<b>Описание</b>\n"
        "Бот для получения уведомлений об оценках из таблиц ведомостей\n"
        f"Информация обновляется каждые {period}\n"
        "Доступ и ФИО задаёт администратор. Уведомления приходят автоматически\n\n"
        "<b>Управление</b>\n"
        "/notifications — включить или выключить уведомления\n"
        "/grades — посмотреть свои оценки\n"
        "/tables — список таблиц\n"
        "/suggest — предложить таблицу\n\n"
        "/cancel — отменить текущий ввод\n"
        "/help — помощь"
    )
    if admin:
        text += (
            "\n\n<b>Админ</b>\n"
            "/add — добавить таблицу\n"
            "/pending — предложения на проверку\n"
            "/refresh — обновить кэш и проверить изменения\n"
            "/remove — удалить таблицу\n"
            "/members — список пользователей\n"
            "/access — добавить пользователей или исправить ФИО\n"
            "/revoke — закрыть доступ пользователю"
        )
    contact = contact_username.strip().lstrip("@")
    if contact:
        text += f"\n\nДля связи: @{escape(contact)}"
    text += "\n\n" + cache_caption(
        {"fetched_at": fetched_at, "stale": fetched_at is None or time.time() - fetched_at > interval * 2},
        label="Последнее обновление кэша",
    )
    return text


class Dialog(StatesGroup):
    access_input = State()
    member_name = State()
    revoke = State()
    revoke_confirm = State()
    remove_confirm = State()
    title = State()
    link = State()
    sheet_selection = State()
    sheet_layout = State()
    sheet_columns = State()
    sheet_retry = State()
    workbook_confirm = State()
    remove = State()


def registration_summary(reports):
    if not reports:
        return "Таблиц пока нет"
    missing = [r["title"] for r in reports if r["status"] == "missing"]
    ambiguous = [r["title"] for r in reports if r["status"] == "ambiguous"]
    unavailable = [r["title"] for r in reports if r["status"] == "unavailable"]
    if not missing and not ambiguous and not unavailable:
        return ""
    lines = []
    if missing or ambiguous:
        lines.append("Не смог тебя найти в таблицах:")
        lines.extend(f"- {title}" for title in missing)
        lines.extend(f"- {title} — несколько совпадений ФИО" for title in ambiguous)
    if unavailable:
        lines.append("Не удалось проверить таблицы:")
        lines.extend(f"• {title}" for title in unavailable)
    return "\n".join(lines)


def parse_spec(text):
    pieces = [part.strip() for part in text.split("|")]
    if len(pieces) not in (2, 3, 4) or not pieces[0] or len(pieces[0]) > 200:
        raise SheetError("Формат: Предмет | ссылка | строка заголовков | столбцы ФИО")
    url = export_url(pieces[1])
    try:
        header = int(pieces[2]) if len(pieces) > 2 else 1
        columns = tuple(int(v.strip()) for v in pieces[3].split(",")) if len(pieces) > 3 else (1,)
    except ValueError as exc:
        raise SheetError("Строка и столбцы должны быть числами") from exc
    if not 1 <= header <= 100 or not columns or min(columns) < 1 or len(set(columns)) != len(columns):
        raise SheetError("Проверьте номера строки и столбцов (начинаются с 1)")
    return pieces[0], url, header, columns


def confirmation(action, token, label):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=label, callback_data=f"{action}:{token}"),
                InlineKeyboardButton(text="Отмена", callback_data=f"dismiss:{token}"),
            ]
        ]
    )


def menu(admin=False):
    rows = [["Мои оценки", "Таблицы"], ["Уведомления", "Помощь"], ["Предложить таблицу"]]
    if admin:
        rows.extend(
            [
                ["Пользователи", "Добавить пользователей"],
                ["Добавить таблицу", "Предложения"],
                ["Обновить", "Удалить таблицу"],
                ["Закрыть доступ"],
            ]
        )
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=text) for text in row] for row in rows],
        resize_keyboard=True,
        input_field_placeholder="Выбери действие",
    )


class AccessMiddleware(BaseMiddleware):
    def __init__(self, db, contact):
        self.db, self.contact = db, contact.strip().lstrip("@")

    async def __call__(self, handler, event, data):
        user = event.from_user
        if not user or not self.db.authorized(user.id):
            audit("access_denied", level=logging.DEBUG)
            text = "Доступ закрыт"
            if self.contact:
                text += f"\nДля связи: @{self.contact}"
            if isinstance(event, CallbackQuery):
                await event.answer("Доступ закрыт", show_alert=True)
            elif isinstance(event, Message) and event.chat.type == "private":
                await data["state"].clear()
                await event.answer(text, reply_markup=ReplyKeyboardRemove())
            return
        if isinstance(event, CallbackQuery) and isinstance(event.message, Message):
            if event.message.chat.type != "private":
                await event.answer("Открой личный чат с ботом", show_alert=True)
                return
        try:
            return await handler(event, data)
        except Exception as exc:
            audit("handler_failed", level=logging.ERROR, error=exc)
            raise


def create_router(db, monitor, contact_username=""):
    router = Router()
    guard = AccessMiddleware(db, contact_username)
    router.message.outer_middleware(guard)
    router.callback_query.outer_middleware(guard)

    def help_text(user_id):
        users = db.all("SELECT name FROM users WHERE id=?", (user_id,))
        timestamp = db.all(
            "SELECT MAX(c.fetched_at) AS timestamp FROM sheet_cache c "
            "JOIN sheets s ON s.id=c.sheet_id WHERE s.status='approved'"
        )[0]["timestamp"]
        return help_message(
            monitor.interval,
            contact_username,
            admin=user_id == db.admin_id(),
            name=users[0]["name"] if users else None,
            fetched_at=timestamp,
        )

    def registered(user_id):
        return bool(db.all("SELECT id FROM users WHERE id=?", (user_id,)))

    @router.message(F.chat.type != "private")
    async def private_only(message: Message):
        await message.answer("Напиши боту в личные сообщения")

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext):
        await state.clear()
        user_id = message.from_user.id
        first = not registered(user_id)
        if not db.activate(user_id):
            await message.answer(
                "Панель администратора",
                reply_markup=menu(True),
            )
            return
        name = db.all("SELECT name FROM members WHERE id=?", (user_id,))[0]["name"]
        reports = await monitor.inspect_user(user_id, baseline=first)
        await message.answer(
            f"Привет, {name}!\nОценки и уведомления",
            reply_markup=menu(user_id == db.admin_id()),
        )
        summary = registration_summary(reports)
        lines = safe_lines(summary) if summary else []
        for report in reports:
            if report["stale"] and report["fetched_at"] is not None:
                lines.append(cache_caption(report) + " " + escape(report["title"]))
        if lines:
            await send_view(message, db, lines)

    @router.message(F.text == "Помощь")
    @router.message(Command("help"))
    async def help_command(message: Message):
        await message.answer(help_text(message.from_user.id), parse_mode="HTML")

    @router.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext):
        await state.clear()
        await message.answer("Отменил", reply_markup=menu(message.from_user.id == db.admin_id()))

    @router.message(Command("name"))
    async def name_command(message: Message):
        await message.answer("ФИО меняет администратор")

    @router.message(F.text == "Уведомления")
    @router.message(Command("notifications"))
    async def notifications(message: Message):
        if not registered(message.from_user.id):
            await message.answer("Открой /start, чтобы начать получать свои оценки")
            return
        enabled = db.toggle_notifications(message.from_user.id)
        await message.answer(
            "Уведомления включены" if enabled else "Уведомления выключены",
            reply_markup=menu(message.from_user.id == db.admin_id()),
        )

    @router.message(F.text == "Мои оценки")
    @router.message(Command("grades"))
    async def grades(message: Message):
        if not registered(message.from_user.id):
            await message.answer("Открой /start, чтобы начать пользоваться ботом")
            return
        reports = await monitor.inspect_user(message.from_user.id)
        lines = [bold("Текущие оценки"), ""]
        for report in reports:
            lines.extend(format_grades(report, include_caption=False).splitlines())
            lines.append("")
        actions = [[(f"Все оценки: {r['title']}", f"subject:{r['sheet_id']}")] for r in reports]
        if reports:
            timestamps = [r["fetched_at"] for r in reports if r["fetched_at"] is not None]
            lines.append(cache_caption({
                "fetched_at": min(timestamps) if len(timestamps) == len(reports) else None,
                "stale": any(r["stale"] for r in reports),
            }))
        await send_view(message, db, lines if reports else ["Таблиц пока нет"], actions=actions)

    @router.callback_query(F.data.regexp(r"^subject:\d+$"))
    async def subject(callback: CallbackQuery):
        if not registered(callback.from_user.id):
            await callback.answer("Открой /start", show_alert=True)
            return
        reports = await monitor.inspect_user(callback.from_user.id)
        report = next((r for r in reports if r["sheet_id"] == int(callback.data.split(":")[1])), None)
        if report is None:
            await callback.answer("Таблица больше недоступна", show_alert=True)
            return
        await callback.answer()
        if isinstance(callback.message, Message):
            with db.conn:
                view_id, pages = create_view(db, callback.from_user.id, [], pages=subject_pages(report))
            await callback.message.answer(
                pages[0], parse_mode="HTML", reply_markup=view_markup(db, view_id),
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )

    @router.message(F.text == "Таблицы")
    @router.message(Command("tables"))
    async def tables(message: Message):
        rows = db.all("SELECT * FROM sheets WHERE status='approved'")
        lines = [bold("Таблицы группы"), ""]
        for row in rows:
            lines.append(f"#{row['id']}: {table_link(row['title'], row['url'])}")
            cache = db.all("SELECT fetched_at FROM sheet_cache WHERE sheet_id=?", (row["id"],))
            timestamp = cache[0]["fetched_at"] if cache else None
            lines.extend(
                [
                    cache_caption(
                        {
                            "fetched_at": timestamp,
                            "stale": timestamp is None or time.time() - timestamp > monitor.interval * 2,
                        }
                    ),
                    "",
                ]
            )
        await send_view(message, db, lines if rows else ["Таблиц пока нет"])

    @router.message(F.text.in_({"Добавить таблицу", "Предложить таблицу"}))
    @router.message(Command("add", "suggest"))
    async def add(message: Message, state: FSMContext):
        user_id = message.from_user.id
        direct = message.text == "Добавить таблицу" or message.text.split()[0].split("@")[0] == "/add"
        if direct and user_id != db.admin_id():
            await message.answer("Эта команда недоступна")
            return
        if not registered(user_id) and user_id != db.admin_id():
            await message.answer("Открой /start, чтобы начать пользоваться ботом")
            return
        await state.clear()
        await state.update_data(direct=direct, header=1, columns=(1,))
        await state.set_state(Dialog.title)
        await message.answer("Отправь название предмета")

    @router.message(Command("layout"))
    async def layout(message: Message, state: FSMContext):
        # Optional advanced configuration; the normal flow only asks for title and link.
        if await state.get_state() != Dialog.link.state:
            await message.answer("Сначала начни добавление таблицы")
            return
        try:
            _, _, header, columns = parse_spec(
                "Настройка | https://docs.google.com/spreadsheets/d/placeholder/edit | "
                + message.text.partition(" ")[2]
            )
        except SheetError:
            await message.answer("Укажи строку заголовков и столбцы ФИО: /layout 2 | 1,2,3")
            return
        await state.update_data(header=header, columns=columns)
        await message.answer("Настройки сохранил — отправь ссылку. На шаге настройки листа отправь «по умолчанию»")

    @router.message(F.text == "Обновить")
    @router.message(Command("refresh"))
    async def refresh(message: Message):
        if message.from_user.id != db.admin_id():
            await message.answer("Команда доступна админу")
            return
        await message.answer("Секундочку")
        try:
            result = await monitor.scan()
        except Exception:
            await message.answer("Не удалось обновить кэш")
            return
        lines = [bold("Обновление кэша завершено"), f"Обновлено таблиц: {result['updated']}"]
        if result["failed"]:
            lines.extend(["", "Не удалось обновить:"])
            for title in result["failed"]:
                lines.extend(safe_lines(f"- {title}"))
        if result.get("structure_notifications"):
            lines.append(f"Уведомлений админу о структуре: {result['structure_notifications']}")
        if result["notifications"]:
            lines.append(f"Уведомлений об изменениях: {result['notifications']}")
        elif not result.get("structure_notifications"):
            lines.append("Новых изменений нет")
        await send_view(message, db, lines)

    @router.message(F.text == "Предложения")
    @router.message(Command("pending"))
    async def pending(message: Message):
        if message.from_user.id != db.admin_id():
            await message.answer("Команда доступна админу")
            return
        rows = db.all("SELECT * FROM sheets WHERE status='pending'")
        lines, actions = [bold("Предложения таблиц"), ""], {}
        for row in rows:
            anchor = f"#{row['id']}:"
            lines.extend(
                [
                    f"{anchor} {table_link(row['title'], row['url'])}",
                    f"<i>Заголовок: {row['header_row']} · ФИО: {escape(row['name_columns'])}</i>",
                    "",
                ]
            )
            actions[anchor] = [
                (f"Настроить #{row['id']}", f"configure:{row['id']}"),
                (f"Отклонить #{row['id']}", f"reject:{row['id']}"),
            ]
        await send_view(message, db, lines if rows else ["Нет предложений"], admin=True, actions=actions)

    @router.callback_query(F.data.regexp(r"^page:[a-f0-9]{16}:\d+$"))
    async def page(callback: CallbackQuery):
        _, view_id, index = callback.data.split(":")
        rows = db.all("SELECT * FROM views WHERE id=?", (view_id,))
        if (
            not rows
            or rows[0]["owner"] != callback.from_user.id
            or (rows[0]["admin"] and callback.from_user.id != db.admin_id())
        ):
            await callback.answer("Этот список недоступен", show_alert=True)
            return
        pages, index = json.loads(rows[0]["pages"]), int(index)
        if index >= len(pages):
            await callback.answer("Страница не найдена")
            return
        await callback.answer()
        if isinstance(callback.message, Message) and callback.message.text != pages[index]:
            try:
                await callback.message.edit_text(
                    pages[index],
                    parse_mode="HTML",
                    reply_markup=view_markup(db, view_id, index),
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            except TelegramBadRequest as exc:
                if "message is not modified" not in str(exc):
                    raise

    @router.callback_query(F.data.regexp(r"^(approve|reject):\d+$"))
    async def moderate(callback: CallbackQuery, state: FSMContext):
        if callback.from_user.id != db.admin_id():
            await callback.answer("Только для админа", show_alert=True)
            return
        action, sheet_id = callback.data.split(":")
        row = db.all("SELECT * FROM sheets WHERE id=? AND status='pending'", (int(sheet_id),))
        if not row:
            await callback.answer("Уже обработано")
            return
        if action == "approve" and "tabs" in json.loads(row[0]["parser_options"]):
            await configure_proposal(callback, state)
            return
        parsed = None
        if action == "approve":
            await callback.answer("Секундочку")
            if isinstance(callback.message, Message):
                await callback.message.answer("Секундочку")
            try:
                parsed = await monitor.fetch(row[0])
            except Exception as exc:
                audit("moderation_failed", level=logging.WARNING, sheet_id=int(sheet_id), error=exc)
                if isinstance(callback.message, Message):
                    await callback.message.answer("Таблица недоступна или формат неверен")
                return
        async with monitor.lock:
            if callback.from_user.id != db.admin_id():
                if isinstance(callback.message, Message):
                    await callback.message.answer("Эта команда больше недоступна")
                return
            with db.conn:
                current = db.all("SELECT * FROM sheets WHERE id=? AND status='pending'", (int(sheet_id),))
                if not current:
                    if action == "approve" and isinstance(callback.message, Message):
                        await callback.message.answer("Уже обработано")
                    elif action != "approve":
                        await callback.answer("Уже обработано")
                    return
                sheet = current[0]
                if action == "approve":
                    db.conn.execute("UPDATE sheets SET status='approved' WHERE id=?", (int(sheet_id),))
                    db.cache(sheet["id"], parsed)
                    db.broadcast_table(sheet["title"], sheet["url"])
                else:
                    db.conn.execute("DELETE FROM sheets WHERE id=?", (int(sheet_id),))
                db.queue(
                    sheet["proposer"],
                    [
                        bold("Таблица одобрена" if action == "approve" else "Таблица отклонена"),
                        "",
                        table_link(sheet["title"], sheet["url"]),
                    ],
                    kind="moderation",
                )
        audit(
            "moderation_finished",
            sheet_id=int(sheet_id),
            result="approved" if action == "approve" else "rejected",
        )
        if action == "approve":
            if isinstance(callback.message, Message):
                await callback.message.answer("Таблица одобрена")
        else:
            await callback.answer("Отклонено")
        if isinstance(callback.message, Message) and callback.message.reply_markup:
            remaining = [
                [
                    button
                    for button in buttons
                    if button.callback_data not in (f"approve:{sheet_id}", f"reject:{sheet_id}")
                ]
                for buttons in callback.message.reply_markup.inline_keyboard
            ]
            await callback.message.edit_reply_markup(
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[buttons for buttons in remaining if buttons]
                )
            )
        await monitor.deliver()

    @router.message(F.text == "Удалить таблицу")
    @router.message(Command("remove"))
    async def remove(message: Message, state: FSMContext):
        if message.from_user.id != db.admin_id():
            await message.answer("Эта команда недоступна")
            return
        await state.clear()
        await state.set_state(Dialog.remove)
        rows = db.all("SELECT * FROM sheets WHERE status='approved'")
        lines, actions = [bold("Выбери таблицу для удаления"), ""], {}
        for row in rows:
            anchor = f"#{row['id']}:"
            lines.append(f"{anchor} {table_link(row['title'], row['url'])}")
            actions[anchor] = [(f"Удалить #{row['id']}", f"pick_remove:{row['id']}")]
        if not rows:
            await state.clear()
        await send_view(message, db, lines if rows else ["Таблиц пока нет"], admin=True, actions=actions)

    @router.message(F.text == "Пользователи")
    @router.message(Command("members"))
    async def members(message: Message):
        if message.from_user.id != db.admin_id():
            await message.answer("Команда доступна админу")
            return
        rows = db.all(
            "SELECT m.*,u.id AS active FROM members m LEFT JOIN users u ON u.id=m.id ORDER BY m.name"
        )
        lines, actions = [bold("Пользователи группы"), ""], {}
        for row in rows:
            label = f"@{row['username']}" if row["username"] else "без юзернейма"
            anchor = f"#{row['id']}:"
            lines.extend(safe_lines(f"{anchor} {row['name']} · {label} · ID {row['id']}"))
            if row["id"] != db.admin_id():
                actions[anchor] = [(f"Закрыть доступ #{row['id']}", f"pick_revoke:{row['id']}")]
            lines.append("Вход выполнен" if row["active"] else "Ещё не заходил")
            lines.append("")
        await send_view(
            message,
            db,
            lines if rows else ["Список пуст"],
            admin=True,
            actions=actions,
        )

    def access_keyboard(request_id, count=0):
        rows = [
            [
                KeyboardButton(
                    text="Выбрать пользователя",
                    request_users=KeyboardButtonRequestUsers(
                        request_id=request_id,
                        user_is_bot=False,
                        max_quantity=1,
                        request_username=True,
                        request_name=True,
                    ),
                )
            ]
        ]
        if count:
            rows.append([KeyboardButton(text="Сохранить всех")])
        rows.append([KeyboardButton(text="Отмена")])
        return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

    @router.message(F.text == "Добавить пользователей")
    @router.message(Command("access"))
    async def access(message: Message, state: FSMContext):
        if message.from_user.id != db.admin_id():
            await message.answer("Команда доступна админу")
            return
        await state.clear()
        request_id = secrets.randbelow(2**30)
        await state.update_data(request_id=request_id, entries=[], queue=[])
        await state.set_state(Dialog.access_input)
        await message.answer("Отправь контакт", reply_markup=access_keyboard(request_id))

    async def request_member_name(message, state, member_id, username="", label=""):
        data = await state.get_data()
        queue = data.get("queue", [])
        ids = {entry[0] for entry in data.get("entries", [])} | {entry[0] for entry in queue}
        if await state.get_state() == Dialog.member_name.state:
            ids.add(data["member_id"])
        if len(ids) >= 50 and member_id not in ids:
            await message.answer("В списке уже 50 пользователей")
            return
        if await state.get_state() == Dialog.member_name.state:
            if member_id == data["member_id"] or any(entry[0] == member_id for entry in queue):
                await message.answer("Контакт уже в очереди")
                return
            queue.append((member_id, username, label))
            await state.update_data(queue=queue)
            await message.answer(f"Контактов в очереди: {len(queue) + 1}")
            return
        await state.update_data(member_id=member_id, username=username, member_label=label)
        await state.set_state(Dialog.member_name)
        await message.answer(
            f"ФИО — {label}" if label else "ФИО",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text="Отмена")]],
                resize_keyboard=True,
            ),
        )

    @router.message(Dialog.access_input, F.contact)
    @router.message(Dialog.member_name, F.contact)
    async def contact_member(message: Message, state: FSMContext):
        if message.from_user.id != db.admin_id():
            await state.clear()
            return
        if not message.contact.user_id or message.contact.user_id <= 0:
            data = await state.get_data()
            await message.answer(
                "В контакте нет Telegram ID",
                reply_markup=access_keyboard(
                    data["request_id"],
                    len(data.get("entries", [])),
                ),
            )
            return
        await request_member_name(
            message,
            state,
            message.contact.user_id,
            label=" ".join(part for part in (message.contact.first_name, message.contact.last_name) if part),
        )

    @router.message(Dialog.access_input, F.users_shared)
    @router.message(Dialog.member_name, F.users_shared)
    async def selected_user(message: Message, state: FSMContext):
        data = await state.get_data()
        shared = message.users_shared
        if message.from_user.id != db.admin_id() or shared.request_id != data.get("request_id"):
            await message.answer("Выбор устарел")
            return
        if len(shared.users) != 1 or shared.users[0].user_id <= 0:
            await message.answer("Выбери одного пользователя")
            return
        user = shared.users[0]
        await request_member_name(
            message,
            state,
            user.user_id,
            (user.username or "").lower(),
            " ".join(part for part in (user.first_name, user.last_name) if part),
        )

    @router.message(F.text == "Сохранить всех")
    @router.message(Command("save"))
    async def save_members(message: Message, state: FSMContext):
        if message.from_user.id != db.admin_id():
            await message.answer("Команда доступна админу")
            return
        current = await state.get_state()
        if current != Dialog.access_input.state:
            await message.answer(
                "Сначала отправь ФИО"
                if current == Dialog.member_name.state
                else "Начни добавление пользователей"
            )
            return
        entries = (await state.get_data()).get("entries", [])
        if not entries:
            await message.answer("Список пуст")
            return
        async with monitor.lock:
            db.grant_members(entries)
        await state.clear()
        await message.answer(f"Сохранил пользователей: {len(entries)}", reply_markup=menu(True))

    @router.message(F.text == "Отмена")
    async def cancel_button(message: Message, state: FSMContext):
        await cancel(message, state)

    @router.message(Dialog.member_name, F.text, ~F.text.startswith("/"))
    async def selected_name(message: Message, state: FSMContext):
        if message.from_user.id != db.admin_id():
            await state.clear()
            return
        name = " ".join(message.text.split())
        if not valid_name(name):
            await message.answer("Нужно 2–3 полных слова")
            return
        data = await state.get_data()
        entries = [entry for entry in data.get("entries", []) if entry[0] != data["member_id"]]
        entries.append((data["member_id"], data["username"], name))
        queue = data.get("queue", [])
        await state.update_data(entries=entries)
        if queue:
            member_id, username, label = queue.pop(0)
            await state.update_data(queue=queue, member_id=member_id, username=username, member_label=label)
            await message.answer(
                f"{name} · В списке: {len(entries)}\n" + (f"ФИО — {label}" if label else "ФИО")
            )
        else:
            await state.set_state(Dialog.access_input)
            await message.answer(
                f"{name} · В списке: {len(entries)}",
                reply_markup=access_keyboard(
                    data["request_id"],
                    len(entries),
                ),
            )

    @router.message(Dialog.access_input, F.text, ~F.text.startswith("/"))
    async def expect_contact(message: Message):
        await message.answer("Отправь контакт")

    @router.message(F.text == "Закрыть доступ")
    @router.message(Command("revoke"))
    async def revoke(message: Message, state: FSMContext):
        if message.from_user.id != db.admin_id():
            await message.answer("Команда доступна админу")
            return
        await state.clear()
        await state.set_state(Dialog.revoke)
        await members(message)
        if not db.all("SELECT id FROM members WHERE id!=?", (db.admin_id(),)):
            await state.clear()

    @router.message(Dialog.revoke, F.text, ~F.text.startswith("/"))
    async def revoke_id(message: Message, state: FSMContext):
        if message.from_user.id != db.admin_id():
            await state.clear()
            return
        try:
            user_id = int(message.text.strip())
        except ValueError:
            await message.answer("Нужен числовой ID из /members")
            return
        await ask_revoke(message, state, user_id)

    async def ask_revoke(message, state, user_id):
        rows = db.all("SELECT name FROM members WHERE id=?", (user_id,))
        if not rows or user_id == db.admin_id():
            await message.answer("Пользователь не найден или это текущий администратор")
            return
        token = secrets.token_hex(8)
        await state.update_data(member_id=user_id, token=token)
        await state.set_state(Dialog.revoke_confirm)
        await message.answer(
            f"Закрыть доступ для {rows[0]['name']}?",
            reply_markup=confirmation("revoke", token, "Закрыть доступ"),
        )

    @router.callback_query(F.data.regexp(r"^(remove|revoke|dismiss):[a-f0-9]{16}$"))
    async def confirm(callback: CallbackQuery, state: FSMContext):
        if callback.from_user.id != db.admin_id():
            await callback.answer("Только для админа", show_alert=True)
            return
        action, token = callback.data.split(":")
        data = await state.get_data()
        states = {
            "remove": Dialog.remove_confirm.state,
            "revoke": Dialog.revoke_confirm.state,
        }
        current = await state.get_state()
        if data.get("token") != token or (action != "dismiss" and states[action] != current):
            await callback.answer("Подтверждение устарело", show_alert=True)
            return
        if action == "dismiss":
            result = "Отменил"
        else:
            async with monitor.lock:
                if action == "remove":
                    deleted = db.execute(
                        "DELETE FROM sheets WHERE id=? AND status='approved'", (data["sheet_id"],)
                    )
                    result = "Таблица удалена" if deleted.rowcount else "Таблица уже удалена"
                    audit("sheet_removed", sheet_id=data["sheet_id"], count=deleted.rowcount)
                else:
                    if data["member_id"] == db.admin_id():
                        await callback.answer("Нельзя закрыть доступ текущему админу", show_alert=True)
                        return
                    db.revoke(data["member_id"])
                    result = "Доступ закрыт"
        await state.clear()
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer(result, reply_markup=menu(True))

    async def setup_view(message, lines):
        with db.conn:
            view_id, pages = create_view(db, db.admin_id(), safe_lines("\n".join(lines)), admin=True)
        await message.answer(pages[0], parse_mode="HTML", reply_markup=view_markup(db, view_id),
                             link_preview_options=LinkPreviewOptions(is_disabled=True))

    def proposal_picker(identifier, data, page=0):
        tabs, selected = data["tabs"], set(data["selected"])
        rows = [[InlineKeyboardButton(text=("✓ " if i in selected else "○ ") + tab["name"][:50],
                                      callback_data=f"ptabs:{identifier}:{i}")]
                for i, tab in enumerate(tabs) if page * 15 <= i < (page + 1) * 15]
        pages = (len(tabs) + 14) // 15
        if pages > 1:
            rows.append([InlineKeyboardButton(text=str(p + 1), callback_data=f"ppage:{identifier}:{p}")
                         for p in range(max(0, page-2), min(pages, page+3))])
        rows.extend([
            [InlineKeyboardButton(text="Снять выделение" if len(selected) == len(tabs) else "Выбрать все",
                                  callback_data=f"ptabs:{identifier}:all")],
            [InlineKeyboardButton(text="Настроить выбранные листы", callback_data=f"configure:{identifier}"),
             InlineKeyboardButton(text="Отклонить", callback_data=f"reject:{identifier}")],
        ])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @router.callback_query(F.data.regexp(r"^(ptabs|ppage):\d+:(\d+|all)$"))
    async def proposal_select(callback: CallbackQuery):
        if callback.from_user.id != db.admin_id():
            await callback.answer("Только для админа", show_alert=True)
            return
        action, identifier, value = callback.data.split(":")
        rows = db.all("SELECT parser_options FROM sheets WHERE id=? AND status='pending'", (int(identifier),))
        if not rows:
            await callback.answer("Уже обработано")
            return
        data = json.loads(rows[0]["parser_options"])
        if "tabs" not in data:
            await callback.answer("Выбор устарел")
            return
        selected = set(data["selected"])
        page = data.get("page", 0)
        if action == "ppage":
            page = int(value)
            if not 0 <= page < (len(data["tabs"]) + 14) // 15:
                await callback.answer("Страница не найдена")
                return
        elif value == "all":
            selected = set() if len(selected) == len(data["tabs"]) else set(range(len(data["tabs"])))
        else:
            index = int(value)
            if index >= len(data["tabs"]):
                await callback.answer("Лист не найден")
                return
            selected.symmetric_difference_update({index})
        data.update(selected=sorted(selected), page=page)
        db.execute("UPDATE sheets SET parser_options=? WHERE id=?", (json.dumps(data), int(identifier)))
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.edit_reply_markup(reply_markup=proposal_picker(identifier, data, page))

    def sheet_picker(data, page=0):
        tabs, selected, token = data["tabs"], set(data["selected"]), data["setup_token"]
        rows = [[InlineKeyboardButton(
            text=("✓ " if i in selected else "○ ") + tab["name"][:50],
            callback_data=f"tabs:{token}:{i}",
        )] for i, tab in enumerate(tabs) if page * 15 <= i < (page + 1) * 15]
        pages = (len(tabs) + 14) // 15
        if pages > 1:
            rows.append([InlineKeyboardButton(text=f"{p + 1}", callback_data=f"tabpage:{token}:{p}")
                         for p in range(max(0, page - 2), min(pages, page + 3))])
        rows.extend([
            [InlineKeyboardButton(text="Снять выделение" if len(selected) == len(tabs) else "Выбрать все",
                                  callback_data=f"tabs:{token}:all")],
            [InlineKeyboardButton(text="Настроить выбранные листы", callback_data=f"tabs:{token}:next")],
        ])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def begin_workbook(message, state, url):
        tabs = await discover_sheets(monitor.session, url)
        await state.update_data(tabs=tabs, selected=list(range(len(tabs))), setup_token=secrets.token_hex(8),
                                tab_page=0, configured=[])
        await state.set_state(Dialog.sheet_selection)
        await message.answer("Выбери листы для парсинга (по умолчанию выбраны все)",
                             reply_markup=sheet_picker(await state.get_data()))

    async def configure_next(message, state):
        data = await state.get_data()
        remaining = [i for i in data["selected"] if i not in [c["index"] for c in data["configured"]]]
        if not remaining:
            await state.set_state(Dialog.workbook_confirm)
            lines = ["Готово к сохранению:"]
            for item in data["configured"]:
                lines.append(f"{item['title']}: {len(item['grade_columns'])} столбцов оценок")
            await setup_view(message, lines)
            await message.answer("Сохранить все листы?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Сохранить", callback_data=f"worksave:{data['setup_token']}")
            ]]))
            return
        index = remaining[0]
        tab = data["tabs"][index]
        try:
            rows = await fetch_sheet(monitor.session, tab["url"], raw=True)
            if not rows or len(rows) < 2:
                raise SheetError("На листе нет строк данных")
        except (SheetError, aiohttp.ClientError, UnicodeError, TimeoutError) as exc:
            await message.answer(f"Не удалось прочитать лист «{tab['name']}»: {exc}\n"
                                 "Отправь «повторить» или «пропустить»")
            await state.update_data(current_index=index)
            await state.set_state(Dialog.sheet_retry)
            return
        await state.update_data(current_index=index, raw_rows=rows)
        try:
            layout = detect_layout(rows)
            await state.update_data(**layout)
            await state.set_state(Dialog.sheet_columns)
            await show_columns(message, state)
        except SheetError:
            await message.answer(f"Лист «{tab['name']}»: не удалось определить структуру автоматически")
            await request_layout(message, state)

    @router.callback_query(F.data.regexp(r"^(tabs|tabpage):[a-f0-9]{16}:(\d+|all|next)$"))
    async def select_tabs(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        action, token, value = callback.data.split(":")
        if callback.from_user.id != db.admin_id():
            await callback.answer("Только для админа", show_alert=True)
            return
        if await state.get_state() != Dialog.sheet_selection.state or token != data.get("setup_token"):
            await callback.answer("Выбор устарел")
            return
        selected = set(data["selected"])
        if action == "tabpage":
            page = int(value)
            if not 0 <= page < (len(data["tabs"]) + 14) // 15:
                await callback.answer("Страница не найдена")
                return
            await state.update_data(tab_page=page)
        elif value == "next":
            if not selected:
                await callback.answer("Выбери хотя бы один лист", show_alert=True)
                return
            await callback.answer()
            if isinstance(callback.message, Message):
                await callback.message.edit_reply_markup(reply_markup=None)
                await configure_next(callback.message, state)
            return
        elif value == "all":
            selected = set() if len(selected) == len(data["tabs"]) else set(range(len(data["tabs"])))
        else:
            index = int(value)
            if index >= len(data["tabs"]):
                await callback.answer("Лист не найден")
                return
            selected.symmetric_difference_update({index})
        await state.update_data(selected=sorted(selected))
        await callback.answer()
        if isinstance(callback.message, Message):
            data = await state.get_data()
            await callback.message.edit_reply_markup(reply_markup=sheet_picker(data, data["tab_page"]))

    async def skip_current(message, state):
        data = await state.get_data()
        await state.update_data(selected=[i for i in data["selected"] if i != data["current_index"]])
        if not (await state.get_data())["selected"]:
            await state.clear()
            await message.answer("Все листы пропущены. Ничего не сохранено")
            return
        await configure_next(message, state)

    @router.message(Dialog.sheet_retry, F.text, ~F.text.startswith("/"))
    async def retry_sheet(message: Message, state: FSMContext):
        if message.from_user.id != db.admin_id():
            await state.clear()
            return
        if message.text.strip().casefold() == "пропустить":
            await skip_current(message, state)
        else:
            await configure_next(message, state)

    def setup_keyboard(data):
        prefix = f"sheetcfg:{data['setup_token']}:{data['current_index']}"
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Далее →", callback_data=prefix + ":next")],
            [InlineKeyboardButton(text="Только итоги", callback_data=prefix + ":totals"),
             InlineKeyboardButton(text="Все оценки", callback_data=prefix + ":all")],
            [InlineKeyboardButton(text="Список столбцов", callback_data=prefix + ":details"),
             InlineKeyboardButton(text="Исправить структуру", callback_data=prefix + ":layout")],
            [InlineKeyboardButton(text="Пропустить лист", callback_data=prefix + ":skip")],
        ])

    async def request_layout(message, state):
        data = await state.get_data()
        await state.set_state(Dialog.sheet_layout)
        # A compact header-only sample is enough for manual correction.
        lines = ["Укажи строки заголовков и ФИО, например: 1-3 | B"]
        for n, row in enumerate(data["raw_rows"][:min(4, data.get("header", 3))], 1):
            cells = [f"{column_letter(i)}: {value[:45]}" for i, value in enumerate(row, 1) if value.strip()]
            lines.append(f"Строка {n}: " + "; ".join(cells[:5]) + ("; …" if len(cells) > 5 else ""))
        await message.answer("\n".join(lines))

    @router.callback_query(F.data.regexp(r"^sheetcfg:[a-f0-9]{16}:\d+:(next|totals|all|details|layout|skip)$"))
    async def sheet_setup_action(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        _, token, index, action = callback.data.split(":")
        if callback.from_user.id != db.admin_id():
            await callback.answer("Только для админа", show_alert=True)
            return
        if (token != data.get("setup_token") or int(index) != data.get("current_index")
                or await state.get_state() != Dialog.sheet_columns.state):
            await callback.answer("Настройка устарела")
            return
        await callback.answer()
        if not isinstance(callback.message, Message):
            return
        if action in {"next", "skip", "layout"}:
            await callback.message.edit_reply_markup(reply_markup=None)
        if action == "next":
            await accept_current(callback.message, state)
        elif action == "skip":
            await skip_current(callback.message, state)
        elif action == "layout":
            await request_layout(callback.message, state)
        elif action == "details":
            await show_columns(callback.message, state, detailed=True)
        else:
            parsed = Sheet(data["raw_rows"], data["header"], tuple(data["columns"]), data["header_start"])
            selected = [i for i, label in parsed.labels.items()
                        if label.strip().casefold() not in {"№", "номер", "п/п", "№ п/п"}
                        and (action == "all" or "итог" in label.casefold())]
            if not selected:
                await callback.message.answer("Итоговых столбцов не найдено. Выбери их текстом или открой список столбцов")
                return
            await state.update_data(grade_columns=selected)
            await show_columns(callback.message, state)

    async def accept_current(message, state):
        data = await state.get_data()
        tab = data["tabs"][data["current_index"]]
        title = data["title"] if len(data["tabs"]) == 1 else f"{data['title']} · {tab['name']}"
        configured = data["configured"] + [{
            "index": data["current_index"], "title": title, "url": tab["url"],
            "header": data["header"], "columns": data["columns"],
            "header_start": data["header_start"], "grade_columns": data["grade_columns"],
            "rows": data["raw_rows"],
        }]
        await state.update_data(configured=configured)
        await configure_next(message, state)

    async def show_columns(message, state, *, detailed=False):
        data = await state.get_data()
        try:
            parsed = Sheet(data["raw_rows"], data["header"], tuple(data["columns"]),
                           data["header_start"], tuple(data["grade_columns"]))
        except SheetError as exc:
            await message.answer(str(exc))
            return False
        if detailed:
            all_columns = Sheet(data["raw_rows"], data["header"], tuple(data["columns"]), data["header_start"])
            lines = ["Столбцы оценок (✓ — выбраны):"] + [
                f"{'✓' if i in parsed.labels else '○'} {column_letter(i)}: {label}"
                for i, label in all_columns.labels.items()
                if label.strip().casefold() not in {"№", "номер", "п/п", "№ п/п"}
            ]
            await setup_view(message, lines)
            await message.answer("Выбери текстом: «только C,E», «исключить C:F» или «исключить по названию округление»")
            return True
        tab = data["tabs"][data["current_index"]]
        headers = str(data["header"]) if data["header_start"] == data["header"] else (
            f"{data['header_start']}–{data['header']}")
        names = ", ".join(column_letter(i) for i in data["columns"])
        lines = [f"Лист {len(data['configured']) + 1}/{len(data['selected'])}: {tab['name']}",
                 f"ФИО: {names} · Заголовки: строки {headers}",
                 f"Выбрано столбцов оценок: {len(parsed.labels)}", "", "Примеры:"]
        sample = next((row for row in parsed.rows[parsed.header_row:]
                       if valid_name(" ".join(row[i - 1] if i <= len(row) else ""
                                              for i in parsed.name_columns))), None)
        for column, label in list(parsed.labels.items())[:3]:
            value = " ".join(sample[column - 1].split()) if sample is not None and column <= len(sample) else ""
            preview = (value[:60] + ("…" if len(value) > 60 else "")) if value else "пусто"
            lines.append(f"• {label[:170]}: {preview}" if sample is not None else f"• {label[:170]}")
        if sample is None:
            lines.append("Нет строки с ФИО для примера значений")
        if len(parsed.labels) > 3:
            lines.append(f"… и ещё {len(parsed.labels) - 3}")
        await message.answer("\n".join(lines), reply_markup=setup_keyboard(data))
        return True

    @router.message(Dialog.sheet_columns, F.text.contains("|"), ~F.text.startswith("/"))
    @router.message(Dialog.sheet_layout, F.text, ~F.text.startswith("/"))
    async def workbook_layout(message: Message, state: FSMContext):
        if message.from_user.id != db.admin_id():
            await state.clear()
            return
        if message.text.strip().casefold() == "пропустить":
            await skip_current(message, state)
            return
        data = await state.get_data()
        try:
            if message.text.strip().casefold() == "по умолчанию":
                headers, names = str(data["header"]), ",".join(str(i) for i in data["columns"])
            else:
                headers, names = message.text.split("|")
            bounds = headers.strip().split("-")
            if len(bounds) > 2:
                raise ValueError
            start, end = int(bounds[0]), int(bounds[-1])
            if not 1 <= start <= end <= min(100, len(data["raw_rows"]) - 1):
                raise ValueError
            width = max(len(row) for row in data["raw_rows"][start-1:end])
            columns = parse_columns(names, width)
            grades = tuple(i for i in range(1, width+1) if i not in columns)
            await state.update_data(header_start=start, header=end, columns=columns, grade_columns=grades)
            if not await show_columns(message, state):
                return
        except (ValueError, SheetError):
            await message.answer("Неверный формат. Пример: 1-3 | B. Номера строк начинаются с 1")
            return
        await state.set_state(Dialog.sheet_columns)

    @router.message(Dialog.sheet_columns, F.text, ~F.text.startswith("/"))
    async def workbook_columns(message: Message, state: FSMContext):
        if message.from_user.id != db.admin_id():
            await state.clear()
            return
        data = await state.get_data()
        text = message.text.strip().casefold()
        if text == "назад":
            await state.set_state(Dialog.sheet_layout)
            await message.answer("Укажи строки заголовков и ФИО: 1-3 | B")
            return
        if text == "пропустить":
            await skip_current(message, state)
            return
        if text == "готово":
            await accept_current(message, state)
            return
        width = max(len(row) for row in data["raw_rows"][data["header_start"]-1:data["header"]])
        available = set(range(1, width+1)) - set(data["columns"])
        try:
            if text == "все":
                selected = available
            else:
                action, _, spec = text.partition(" ")
                if spec.startswith("по названию "):
                    terms = [term.strip() for term in spec.removeprefix("по названию ").split(",")]
                    if not all(terms):
                        raise SheetError("Укажи часть названия: только по названию итог")
                    parsed = Sheet(data["raw_rows"], data["header"], tuple(data["columns"]),
                                   data["header_start"], tuple(sorted(available)))
                    chosen = {i for i, label in parsed.labels.items()
                              if any(term.replace("ё", "е") in label.casefold().replace("ё", "е")
                                     for term in terms)}
                    if not chosen:
                        raise SheetError("Столбцов с таким названием не найдено")
                else:
                    chosen = set(parse_columns(spec, width))
                if action == "только":
                    selected = chosen
                elif action == "исключить":
                    selected = set(data["grade_columns"]) - chosen
                else:
                    raise SheetError("Используй «только D,H», «исключить C:F» или «готово»")
            if not selected or not selected <= available:
                raise SheetError("Нужен хотя бы один столбец оценок; столбцы ФИО выбирать нельзя")
            Sheet(data["raw_rows"], data["header"], tuple(data["columns"]),
                  data["header_start"], tuple(sorted(selected)))
            await state.update_data(grade_columns=sorted(selected))
            await show_columns(message, state)
        except SheetError as exc:
            await message.answer(str(exc))

    @router.callback_query(F.data.regexp(r"^worksave:[a-f0-9]{16}$"))
    async def save_workbook(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        if callback.from_user.id != db.admin_id():
            await callback.answer("Только для админа", show_alert=True)
            return
        if (await state.get_state() != Dialog.workbook_confirm.state
                or callback.data.split(":")[1] != data.get("setup_token")):
            await callback.answer("Подтверждение устарело")
            return
        try:
            async with monitor.lock:
                with db.conn:
                    if not db.conn.in_transaction:
                        db.conn.execute("BEGIN")
                    proposal = data.get("proposal_id")
                    if proposal:
                        if not db.all("SELECT id FROM sheets WHERE id=? AND status='pending'", (proposal,)):
                            raise SheetError("Предложение уже обработано")
                        db.conn.execute("DELETE FROM sheets WHERE id=?", (proposal,))
                    for item in data["configured"]:
                        parsed = Sheet(item["rows"], item["header"], tuple(item["columns"]),
                                       item["header_start"], tuple(item["grade_columns"]))
                        db.add_sheet(item["title"], item["url"], item["header"], item["columns"],
                                     data.get("proposer", callback.from_user.id), "approved", parsed=parsed)
                    self_url = data["tabs"][0]["url"]
                    db.remember_workbook(self_url, data["tabs"])
                    # Queue announcements only after every sheet has passed validation.
                    for item in data["configured"]:
                        db.broadcast_table(item["title"], item["url"])
                    if proposal:
                        db.queue(data["proposer"], [bold("Таблица одобрена"), table_link(data["title"], data["url"])],
                                 kind="moderation")
        except (DuplicateTitleError, sqlite3.IntegrityError, SheetError) as exc:
            await callback.answer("Не удалось сохранить", show_alert=True)
            if isinstance(callback.message, Message):
                await callback.message.answer(f"Название или ссылка уже добавлены, либо предложение обработано: {exc}\n"
                                               "Ничего не сохранено. /cancel — отменить и начать заново")
            return
        await state.clear()
        await callback.answer("Сохранено")
        if isinstance(callback.message, Message):
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer(f"Добавлено листов: {len(data['configured'])}", reply_markup=menu(True))
        await monitor.deliver()

    @router.callback_query(F.data.regexp(r"^configure:\d+$"))
    async def configure_proposal(callback: CallbackQuery, state: FSMContext):
        if callback.from_user.id != db.admin_id():
            await callback.answer("Только для админа", show_alert=True)
            return
        rows = db.all("SELECT * FROM sheets WHERE id=? AND status='pending'", (int(callback.data.split(":")[1]),))
        if not rows:
            await callback.answer("Уже обработано")
            return
        await callback.answer()
        if isinstance(callback.message, Message):
            row = rows[0]
            await state.clear()
            await state.update_data(title=row["title"], url=row["url"], direct=True,
                                    proposal_id=row["id"], proposer=row["proposer"])
            try:
                selection = json.loads(row["parser_options"])
                if "tabs" in selection:
                    if not selection["selected"]:
                        await callback.message.answer("Выбери хотя бы один лист в сообщении предложения")
                        return
                    await state.update_data(tabs=selection["tabs"], selected=selection["selected"],
                                            configured=[], setup_token=secrets.token_hex(8), tab_page=0)
                    await configure_next(callback.message, state)
                else:
                    await begin_workbook(callback.message, state, row["url"])
            except (SheetError, aiohttp.ClientError, UnicodeError, TimeoutError) as exc:
                await callback.message.answer(f"Не удалось найти листы: {exc}. Нажми «Настроить» ещё раз")

    @router.message(Dialog.title, F.text, ~F.text.startswith("/"))
    async def save_title(message: Message, state: FSMContext):
        title = message.text.strip()
        if not title or len(title) > 200 or "\n" in title:
            await message.answer("Название должно быть от 1 до 200 символов")
            return
        if db.title_exists(title):
            await message.answer(
                "Таблица с таким названием уже есть или ждёт проверки\nВведи другое название таблицы"
            )
            return
        await state.update_data(title=title)
        await state.set_state(Dialog.link)
        await message.answer("Отправь ссылку на таблицу")

    @router.message(Dialog.link, F.text, ~F.text.startswith("/"))
    async def save_link(message: Message, state: FSMContext):
        data = await state.get_data()
        direct = data["direct"]
        if direct and message.from_user.id != db.admin_id():
            await state.clear()
            await message.answer("Эта команда больше недоступна")
            return
        try:
            url = sheet_url(message.text.strip())
            await message.answer("Секундочку")
            if direct:
                if db.title_exists(data["title"]):
                    raise DuplicateTitleError(data["title"])
                await begin_workbook(message, state, url)
                return
            tabs = await discover_sheets(monitor.session, url)
            parsed = None
            if not db.authorized(message.from_user.id) or (direct and message.from_user.id != db.admin_id()):
                await state.clear()
                await message.answer("Доступ изменился")
                return
            proposal_id = db.add_sheet(
                data["title"],
                url,
                data["header"],
                data["columns"],
                message.from_user.id,
                "approved" if direct else "pending",
                parsed=parsed,
                notify=False,
            )
            selection = {"tabs": tabs, "selected": list(range(len(tabs))), "page": 0}
            with db.conn:
                db.conn.execute("UPDATE sheets SET parser_options=? WHERE id=?",
                                (json.dumps(selection), proposal_id))
                markup = proposal_picker(proposal_id, selection)
                actions = [[(button.text, button.callback_data) for button in row]
                           for row in markup.inline_keyboard]
                db.queue(db.admin_id(), [bold(f"Предложение #{proposal_id}"),
                         table_link(data["title"], url), f"От пользователя {message.from_user.id}",
                         "Выбери листы для парсинга (по умолчанию выбраны все)"],
                         kind="proposal", admin=True, actions=actions)
        except (SheetError, aiohttp.ClientError, UnicodeError, TimeoutError) as exc:
            audit("sheet_input_failed", level=logging.WARNING, error=exc)
            await message.answer(f"Не удалось прочитать таблицу: {exc}")
            return
        except DuplicateTitleError:
            await state.set_state(Dialog.title)
            await message.answer("Таблица с таким названием уже появилась\nВведи другое название таблицы")
            return
        except sqlite3.IntegrityError:
            await message.answer("Таблица уже добавлена или ждёт проверки")
            return
        await state.clear()
        if direct:
            await message.answer("Таблицу добавил")
        else:
            await message.answer("Отправил таблицу админу на проверку")
        await monitor.deliver()

    @router.message(Dialog.remove, F.text, ~F.text.startswith("/"))
    async def remove_id(message: Message, state: FSMContext):
        if message.from_user.id != db.admin_id():
            await state.clear()
            await message.answer("Эта команда больше недоступна")
            return
        try:
            sheet_id = int(message.text.strip())
        except ValueError:
            await message.answer("Отправь номер таблицы числом")
            return
        await ask_remove(message, state, sheet_id)

    async def ask_remove(message, state, sheet_id):
        rows = db.all("SELECT * FROM sheets WHERE id=? AND status='approved'", (sheet_id,))
        if not rows:
            await message.answer("Таблица не найдена")
            return
        token = secrets.token_hex(8)
        await state.update_data(sheet_id=sheet_id, token=token)
        await state.set_state(Dialog.remove_confirm)
        await message.answer(
            f"Удалить таблицу «{rows[0]['title']}»?",
            reply_markup=confirmation("remove", token, "Удалить"),
        )

    @router.callback_query(F.data.regexp(r"^pick_(remove|revoke):\d+$"))
    async def pick_action(callback: CallbackQuery, state: FSMContext):
        if callback.from_user.id != db.admin_id():
            await callback.answer("Только для админа", show_alert=True)
            return
        action, identifier = callback.data.split(":")
        await callback.answer()
        if not isinstance(callback.message, Message):
            return
        await state.clear()
        if action == "pick_remove":
            await ask_remove(callback.message, state, int(identifier))
        else:
            await ask_revoke(callback.message, state, int(identifier))

    @router.message(F.text)
    async def text_message(message: Message, state: FSMContext):
        current = await state.get_state()
        prompts = {
            Dialog.title.state: "Жду название предмета",
            Dialog.link.state: "Жду ссылку на таблицу",
            Dialog.remove.state: "Жду номер таблицы",
            Dialog.access_input.state: "Отправь контакт",
            Dialog.member_name.state: "Отправь ФИО выбранного пользователя",
            Dialog.remove_confirm.state: "Жду подтверждение",
            Dialog.revoke.state: "Отправь ID пользователя из /members",
            Dialog.revoke_confirm.state: "Жду подтверждение",
        }
        await message.answer(prompts.get(current, help_text(message.from_user.id)), parse_mode="HTML")

    @router.message()
    async def unsupported(message: Message):
        await message.answer("Отправь текстовое сообщение")

    return router


async def run_bot(db, token, interval, contact_username="", proxy_url=None):
    if db.admin_id() is None:
        raise ValueError("Назначьте админа: gradebot admin TELEGRAM_ID")
    async with network_sessions(proxy_url) as (telegram, session), Bot(token=token, session=telegram) as bot:
        monitor = Monitor(db, session, bot, interval)
        dispatcher = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
        dispatcher.include_router(create_router(db, monitor, contact_username))
        task = asyncio.create_task(monitor.run(interval))
        delivery = asyncio.create_task(monitor.run_delivery())
        try:
            await dispatcher.start_polling(bot)
        finally:
            task.cancel()
            delivery.cancel()
            for background in (task, delivery):
                with contextlib.suppress(asyncio.CancelledError):
                    await background

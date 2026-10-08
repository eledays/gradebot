import asyncio
import contextlib
import json
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

from .access import AccessError, UsernameResolver, parse_members
from .db import DuplicateTitleError
from .monitor import Monitor, cache_caption, format_grades
from .presentation import bold, safe_lines, send_view, table_link, view_markup
from .sheets import SheetError, export_url, fetch_sheet, sheet_url, valid_name


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
    access_confirm = State()
    revoke = State()
    revoke_confirm = State()
    remove_confirm = State()
    title = State()
    link = State()
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
            text = "Доступ закрыт. Попроси администратора добавить тебя в список группы."
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
        return await handler(event, data)


def create_router(db, monitor, contact_username="", resolver=None):
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
                "Панель администратора. Добавь пользователей, затем таблицы с оценками.\n"
                "Чтобы получать свои оценки, добавь себя в список пользователей.",
                reply_markup=menu(True),
            )
            return
        name = db.all("SELECT name FROM members WHERE id=?", (user_id,))[0]["name"]
        reports = await monitor.inspect_user(user_id, baseline=first)
        await message.answer(
            f"Привет, {name}!\nДоступ открыт. Здесь твои оценки и уведомления об изменениях.",
            reply_markup=menu(user_id == db.admin_id()),
        )
        summary = registration_summary(reports)
        lines = safe_lines(summary) if summary else []
        if any(report["status"] in {"missing", "ambiguous"} for report in reports):
            lines.extend(safe_lines("Если ФИО указано неверно, обратись к администратору."))
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
        await message.answer("ФИО задаёт администратор. Обратись к нему, если нужна правка.")

    @router.message(F.text == "Уведомления")
    @router.message(Command("notifications"))
    async def notifications(message: Message):
        if not registered(message.from_user.id):
            await message.answer("Открой /start, чтобы начать получать свои оценки")
            return
        enabled = db.toggle_notifications(message.from_user.id)
        await message.answer(
            "Уведомления включены" if enabled else "Уведомления выключены. Оценки по-прежнему доступны.",
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
            lines.extend(format_grades(report).splitlines())
            lines.append("")
        await send_view(message, db, lines if reports else ["Таблиц пока нет"])

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
        await message.answer("Настройки сохранил — отправь ссылку")

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
        if result["notifications"]:
            lines.append(f"Уведомлений об изменениях: {result['notifications']}")
        else:
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
                (f"Принять #{row['id']}", f"approve:{row['id']}"),
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
    async def moderate(callback: CallbackQuery):
        if callback.from_user.id != db.admin_id():
            await callback.answer("Только для админа", show_alert=True)
            return
        action, sheet_id = callback.data.split(":")
        row = db.all("SELECT * FROM sheets WHERE id=? AND status='pending'", (int(sheet_id),))
        if not row:
            await callback.answer("Уже обработано")
            return
        parsed = None
        if action == "approve":
            await callback.answer("Секундочку")
            if isinstance(callback.message, Message):
                await callback.message.answer("Секундочку")
            try:
                parsed = await monitor.fetch(row[0])
            except Exception:
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
        lines.extend(
            [
                "Добавить или исправить ФИО: /access",
                "Закрыть доступ: /revoke",
                "Юзернейм показан на момент добавления. Доступ определяется по ID.",
            ]
        )
        await send_view(
            message,
            db,
            lines if rows else ["Список пуст. Добавь пользователей через /access"],
            admin=True,
            actions=actions,
        )

    @router.message(F.text == "Добавить пользователей")
    @router.message(Command("access"))
    async def access(message: Message, state: FSMContext):
        if message.from_user.id != db.admin_id():
            await message.answer("Команда доступна админу")
            return
        await state.clear()
        request_id = secrets.randbelow(2**30)
        await state.update_data(request_id=request_id)
        await state.set_state(Dialog.access_input)
        await message.answer(
            "Выбери пользователя кнопкой ниже, затем укажи его ФИО.\n\n"
            "Или отправь список (до 50 строк):\n@username | Иванов Иван Иванович\n\n"
            "ID определится сразу. Пользователь не вводит ФИО и не обязан заходить заранее.\n"
            "Чтобы исправить ФИО, добавь того же человека с новым именем. Отмена — /cancel.",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[
                    [
                        KeyboardButton(
                            text="Выбрать пользователя",
                            request_users=KeyboardButtonRequestUsers(
                                request_id=request_id,
                                user_is_bot=False,
                                max_quantity=1,
                                request_username=True,
                            ),
                        ),
                        KeyboardButton(text="/cancel"),
                    ]
                ],
                resize_keyboard=True,
                one_time_keyboard=True,
            ),
        )

    async def preview_members(message, state, entries):
        token = secrets.token_hex(8)
        await state.update_data(entries=entries, token=token)
        await state.set_state(Dialog.access_confirm)
        await message.answer("ID проверены. Проверь ФИО и сохрани список.", reply_markup=menu(True))
        lines = [bold("Открыть доступ этим пользователям?"), ""]
        for user_id, username, name in entries:
            old = db.all("SELECT name FROM members WHERE id=?", (user_id,))
            label = f"@{username}" if username else "без юзернейма"
            lines.extend(safe_lines(f"{name} · {label} · ID {user_id}"))
            if old:
                lines.extend(safe_lines(f"Уже в списке: {old[0]['name']} → {name}"))
            lines.append("")
        await send_view(
            message,
            db,
            lines,
            admin=True,
            actions=[
                [
                    ("Сохранить", f"grant:{token}"),
                    ("Отмена", f"dismiss:{token}"),
                ]
            ],
        )

    @router.message(Dialog.access_input, F.users_shared)
    async def selected_user(message: Message, state: FSMContext):
        data = await state.get_data()
        shared = message.users_shared
        if message.from_user.id != db.admin_id() or shared.request_id != data.get("request_id"):
            await message.answer("Выбор устарел. Начни заново через /access")
            return
        if len(shared.users) != 1 or shared.users[0].user_id <= 0:
            await message.answer("Выбери одного пользователя")
            return
        user = shared.users[0]
        await state.update_data(member_id=user.user_id, username=(user.username or "").lower())
        await state.set_state(Dialog.member_name)
        await message.answer(
            "Отправь ФИО этого пользователя, как в таблицах с оценками (2–3 полных слова)",
            reply_markup=ReplyKeyboardRemove(),
        )

    @router.message(Dialog.member_name, F.text, ~F.text.startswith("/"))
    async def selected_name(message: Message, state: FSMContext):
        if message.from_user.id != db.admin_id():
            await state.clear()
            return
        name = " ".join(message.text.split())
        if not valid_name(name):
            await message.answer("Нужно ФИО из 2–3 полных слов, как в таблице с оценками")
            return
        data = await state.get_data()
        await preview_members(message, state, [(data["member_id"], data["username"], name)])

    @router.message(Dialog.access_input, F.text, ~F.text.startswith("/"))
    async def import_members(message: Message, state: FSMContext):
        if message.from_user.id != db.admin_id():
            await state.clear()
            return
        try:
            parsed = parse_members(message.text)
            if resolver is None:
                raise AccessError("Поиск по @username не настроен. Используй кнопку «Выбрать пользователя».")
            await message.answer("Проверяю юзернеймы в Telegram…")
            entries, seen = [], set()
            async with asyncio.timeout(90):
                for username, name in parsed:
                    user_id = await resolver.resolve(username)
                    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
                        raise AccessError(f"Не удалось получить ID @{username}. Список не сохранён.")
                    if user_id in seen:
                        raise AccessError("Два юзернейма относятся к одному аккаунту. Оставь одну строку.")
                    seen.add(user_id)
                    entries.append((user_id, username, name))
        except (AccessError, TimeoutError) as exc:
            await message.answer(str(exc) or "Проверка заняла слишком много времени. Список не сохранён.")
            return
        await preview_members(message, state, entries)

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
            f"Закрыть доступ для {rows[0]['name']}? Регистрация и сохранённые личные сообщения в БД будут удалены.",
            reply_markup=confirmation("revoke", token, "Закрыть доступ"),
        )

    @router.callback_query(F.data.regexp(r"^(grant|remove|revoke|dismiss):[a-f0-9]{16}$"))
    async def confirm(callback: CallbackQuery, state: FSMContext):
        if callback.from_user.id != db.admin_id():
            await callback.answer("Только для админа", show_alert=True)
            return
        action, token = callback.data.split(":")
        data = await state.get_data()
        states = {
            "grant": Dialog.access_confirm.state,
            "remove": Dialog.remove_confirm.state,
            "revoke": Dialog.revoke_confirm.state,
        }
        current = await state.get_state()
        if data.get("token") != token or (action != "dismiss" and states[action] != current):
            await callback.answer("Подтверждение устарело. Начни действие заново.", show_alert=True)
            return
        if action == "dismiss":
            result = "Отменил"
        else:
            async with monitor.lock:
                if action == "grant":
                    db.grant_members(data["entries"])
                    result = f"Сохранил пользователей: {len(data['entries'])}. Теперь им достаточно открыть /start."
                elif action == "remove":
                    deleted = db.execute(
                        "DELETE FROM sheets WHERE id=? AND status='approved'", (data["sheet_id"],)
                    )
                    result = "Таблица удалена" if deleted.rowcount else "Таблица уже удалена"
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
        await message.answer(
            "Отправь ссылку на таблицу.\nПо умолчанию: заголовки в строке 1, ФИО в столбце A. "
            "Если иначе — /layout 3 | 2. Отмена — /cancel."
        )

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
            parsed = await fetch_sheet(monitor.session, url, data["header"], data["columns"])
            if not db.authorized(message.from_user.id) or (direct and message.from_user.id != db.admin_id()):
                await state.clear()
                await message.answer("Доступ изменился. Таблица не сохранена.")
                return
            db.add_sheet(
                data["title"],
                url,
                data["header"],
                data["columns"],
                message.from_user.id,
                "approved" if direct else "pending",
                parsed=parsed,
                notify=True,
            )
        except (SheetError, aiohttp.ClientError, UnicodeError, TimeoutError):
            await message.answer(
                "Не удалось прочитать таблицу. Проверь ссылку и доступ на чтение без входа "
                "в Google. Затем отправь ссылку ещё раз; отмена — /cancel."
            )
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
            await message.answer("Таблица с таким номером не найдена. Посмотри номера в /tables.")
            return
        token = secrets.token_hex(8)
        await state.update_data(sheet_id=sheet_id, token=token)
        await state.set_state(Dialog.remove_confirm)
        await message.answer(
            f"Удалить таблицу «{rows[0]['title']}»? Её кэш и история сравнения будут удалены.",
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
            Dialog.access_input.state: "Отправь строки @username | ФИО или выбери пользователя кнопкой",
            Dialog.member_name.state: "Отправь ФИО выбранного пользователя",
            Dialog.access_confirm.state: "Проверь список выше и нажми «Сохранить». Отмена — /cancel",
            Dialog.remove_confirm.state: "Подтверди удаление кнопкой выше. Отмена — /cancel",
            Dialog.revoke.state: "Отправь ID пользователя из /members",
            Dialog.revoke_confirm.state: "Подтверди закрытие доступа кнопкой выше. Отмена — /cancel",
        }
        await message.answer(prompts.get(current, help_text(message.from_user.id)), parse_mode="HTML")

    @router.message()
    async def unsupported(message: Message):
        await message.answer("Отправь текстовое сообщение")

    return router


async def run_bot(db, token, interval, contact_username=""):
    if db.admin_id() is None:
        raise ValueError("Назначьте админа: gradebot admin TELEGRAM_ID")
    resolver = UsernameResolver(token)
    async with Bot(token=token) as bot, aiohttp.ClientSession() as session:
        monitor = Monitor(db, session, bot, interval)
        dispatcher = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
        dispatcher.include_router(create_router(db, monitor, contact_username, resolver))
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
            await resolver.close()

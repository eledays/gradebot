import asyncio
import contextlib
import json
import sqlite3
import time
from html import escape

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)

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
        "Укажи ФИО, а я буду отправлять уведомления о новых оценках\n\n"
        "<b>Управление</b>\n"
        "/name — изменить ФИО\n"
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
            "/remove — удалить таблицу"
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
    registration = State()
    rename = State()
    title = State()
    link = State()
    remove = State()
    checking = State()


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


def create_router(db, monitor, contact_username=""):
    router = Router()

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
        await state.set_state(Dialog.registration)
        await message.answer(
            "Привет\nЯ слежу за оценками в таблицах группы, присылаю уведомления "
            "об изменениях и показываю текущие оценки\n\n"
            "Отправь своё ФИО, чтобы я мог найти тебя в таблицах"
        )

    @router.message(Command("help"))
    async def help_command(message: Message):
        await message.answer(help_text(message.from_user.id), parse_mode="HTML")

    @router.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext):
        await state.clear()
        await message.answer("Отменил")

    @router.message(Command("name"))
    async def name_command(message: Message, state: FSMContext):
        await state.clear()
        await state.set_state(Dialog.rename)
        await message.answer("Отправь новое ФИО")

    @router.message(Command("grades"))
    async def grades(message: Message):
        if not registered(message.from_user.id):
            await message.answer("Сначала зарегистрируйся через /start")
            return
        reports = await monitor.inspect_user(message.from_user.id)
        lines = [bold("Текущие оценки"), ""]
        for report in reports:
            lines.extend(format_grades(report).splitlines())
            lines.append("")
        await send_view(message, db, lines if reports else ["Таблиц пока нет"])

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

    @router.message(Command("add", "suggest"))
    async def add(message: Message, state: FSMContext):
        user_id = message.from_user.id
        direct = message.text.split()[0].split("@")[0] == "/add"
        if direct and user_id != db.admin_id():
            await message.answer("Эта команда недоступна")
            return
        if not registered(user_id):
            await message.answer("Сначала зарегистрируйся через /start")
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
            try:
                parsed = await monitor.fetch(row[0])
            except Exception:
                await callback.answer("Таблица недоступна или формат неверен", show_alert=True)
                return
        async with monitor.lock:
            with db.conn:
                current = db.all("SELECT * FROM sheets WHERE id=? AND status='pending'", (int(sheet_id),))
                if not current:
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
        await callback.answer("Принято" if action == "approve" else "Отклонено")
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

    @router.message(Command("remove"))
    async def remove(message: Message, state: FSMContext):
        if message.from_user.id != db.admin_id():
            await message.answer("Эта команда недоступна")
            return
        await state.clear()
        await state.set_state(Dialog.remove)
        await message.answer("Отправь номер таблицы")

    @router.message(Dialog.registration, F.text, ~F.text.startswith("/"))
    @router.message(Dialog.rename, F.text, ~F.text.startswith("/"))
    async def save_name(message: Message, state: FSMContext):
        name = message.text.strip()
        if not valid_name(name):
            await message.answer("Укажи 2–3 полных слова")
            return
        registering = await state.get_state() == Dialog.registration.state
        await state.set_state(Dialog.checking)
        try:
            async with monitor.lock:
                db.register(message.from_user.id, name)
            if not registering:
                await message.answer("Сохранил")
                return
            reports = await monitor.inspect_user(message.from_user.id, baseline=True)
            lines = [
                bold(f"Добро пожаловать, {name}"),
                "Подсказки по управлению можно посмотреть тут — /help",
            ]
            summary = registration_summary(reports)
            if summary:
                lines.extend(["", *safe_lines(summary)])
            stale = [
                cache_caption(report) + " " + escape(report["title"]) for report in reports if report["stale"]
            ]
            if stale:
                lines.extend(["", *stale])
            await send_view(message, db, lines)
        finally:
            if await state.get_state() == Dialog.checking.state:
                await state.clear()

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
            parsed = await fetch_sheet(monitor.session, url, data["header"], data["columns"])
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
            await message.answer("Не получилось спарсить таблицу — проверь доступ и попробуй ещё раз")
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
        async with monitor.lock:
            result = db.execute("DELETE FROM sheets WHERE id=?", (sheet_id,))
        if not result.rowcount:
            await message.answer("Таблица с таким номером не найдена")
            return
        await state.clear()
        await message.answer("Удалил таблицу")

    @router.message(F.text)
    async def text_message(message: Message, state: FSMContext):
        current = await state.get_state()
        prompts = {
            Dialog.registration.state: "Жду твоё ФИО",
            Dialog.rename.state: "Жду новое ФИО",
            Dialog.title.state: "Жду название предмета",
            Dialog.link.state: "Жду ссылку на таблицу",
            Dialog.remove.state: "Жду номер таблицы",
            Dialog.checking.state: "Проверяю таблицы",
        }
        await message.answer(prompts.get(current, help_text(message.from_user.id)), parse_mode="HTML")

    @router.message()
    async def unsupported(message: Message):
        await message.answer("Отправь текстовое сообщение")

    return router


async def run_bot(db, token, interval, contact_username=""):
    if db.admin_id() is None:
        raise ValueError("Назначьте админа: gradebot admin TELEGRAM_ID")
    async with Bot(token=token) as bot, aiohttp.ClientSession() as session:
        monitor = Monitor(db, session, bot, interval)
        dispatcher = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
        dispatcher.include_router(create_router(db, monitor, contact_username))
        task = asyncio.create_task(monitor.run(interval))
        try:
            await dispatcher.start_polling(bot)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

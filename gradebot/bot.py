import asyncio
import contextlib
import logging
import sqlite3

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .monitor import Monitor, format_grades
from .sheets import SheetError, export_url, fetch_sheet, valid_name

USER_HELP = """/start — зарегистрироваться
/name — изменить ФИО
/grades — посмотреть свои оценки
/tables — таблицы группы
/suggest — предложить таблицу
/cancel — отменить текущий ввод
/help — помощь

Когда оценки изменятся, я напишу вам сам."""
ADMIN_HELP = """

Управление таблицами:
/add — добавить таблицу
/pending — предложения на проверку
/remove — удалить таблицу"""


class Dialog(StatesGroup):
    registration = State()
    rename = State()
    title = State()
    link = State()
    remove = State()
    checking = State()


async def answer_long(message, text):
    for offset in range(0, len(text), 4000):
        await message.answer(text[offset : offset + 4000])


def registration_summary(reports):
    if not reports:
        return "Таблиц пока нет. Проверю вас, когда их добавят."
    missing = [r["title"] for r in reports if r["status"] == "missing"]
    ambiguous = [r["title"] for r in reports if r["status"] == "ambiguous"]
    unavailable = [r["title"] for r in reports if r["status"] == "unavailable"]
    if not missing and not ambiguous and not unavailable:
        return "Смог найти тебя во всех таблицах. Свои оценки можно посмотреть через /grades."
    lines = []
    if missing or ambiguous:
        lines.append("Смог найти тебя во всех таблицах, кроме:")
        lines.extend(f"• {title} — ФИО не найдено" for title in missing)
        lines.extend(f"• {title} — несколько совпадений; уточни ФИО через /name" for title in ambiguous)
    elif any(r["status"] == "found" for r in reports):
        lines.append("Нашёл тебя во всех таблицах, которые удалось проверить.")
    if unavailable:
        lines.append("Не удалось проверить таблицы:")
        lines.extend(f"• {title}" for title in unavailable)
        lines.append("Попробуй позже через /grades.")
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


def moderation_keyboard(sheet_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Принять", callback_data=f"approve:{sheet_id}"),
                InlineKeyboardButton(text="Отклонить", callback_data=f"reject:{sheet_id}"),
            ]
        ]
    )


def create_router(db, monitor):
    router = Router()

    def help_text(user_id):
        return USER_HELP + (ADMIN_HELP if user_id == db.admin_id() else "")

    def registered(user_id):
        return bool(db.all("SELECT id FROM users WHERE id=?", (user_id,)))

    @router.message(F.chat.type != "private")
    async def private_only(message: Message):
        await message.answer("Напишите боту в личные сообщения.")

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext):
        await state.clear()
        await state.set_state(Dialog.registration)
        await message.answer("Напиши своё ФИО. Например: Иванов Иван Иванович или Иванов Иван.")

    @router.message(Command("help"))
    async def help_command(message: Message):
        await message.answer(help_text(message.from_user.id))

    @router.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext):
        await state.clear()
        await message.answer("Отменил.")

    @router.message(Command("name"))
    async def name_command(message: Message, state: FSMContext):
        await state.clear()
        await state.set_state(Dialog.rename)
        await message.answer("Отправь новое имя, которое хочешь указать.")

    @router.message(Command("grades"))
    async def grades(message: Message):
        if not registered(message.from_user.id):
            await message.answer("Сначала зарегистрируйся через /start.")
            return
        await message.answer("Проверяю текущие оценки…")
        reports = await monitor.inspect_user(message.from_user.id)
        if not reports:
            await message.answer("Таблиц пока нет.")
        for report in reports:
            await answer_long(message, format_grades(report))

    @router.message(Command("tables"))
    async def tables(message: Message):
        rows = db.all("SELECT * FROM sheets WHERE status='approved'")
        if not rows:
            await message.answer("Таблиц пока нет.")
        for row in rows:
            await message.answer(f"{row['id']}. {row['title']}\n{row['url']}")

    @router.message(Command("add", "suggest"))
    async def add(message: Message, state: FSMContext):
        user_id = message.from_user.id
        direct = message.text.split()[0].split("@")[0] == "/add"
        if direct and user_id != db.admin_id():
            await message.answer("Эта команда недоступна. Предложить таблицу можно через /suggest.")
            return
        if not registered(user_id):
            await message.answer("Сначала зарегистрируйся через /start.")
            return
        await state.clear()
        await state.update_data(direct=direct, header=1, columns=(1,))
        await state.set_state(Dialog.title)
        await message.answer("Отправь название предмета.")

    @router.message(Command("layout"))
    async def layout(message: Message, state: FSMContext):
        # Optional advanced configuration; the normal flow only asks for title and link.
        if await state.get_state() != Dialog.link.state:
            await message.answer("Сначала начни добавление таблицы.")
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
        await message.answer("Настройки сохранил. Теперь отправь ссылку на таблицу.")

    @router.message(Command("pending"))
    async def pending(message: Message):
        if message.from_user.id != db.admin_id():
            await message.answer("Команда доступна админу.")
            return
        rows = db.all("SELECT * FROM sheets WHERE status='pending'")
        if not rows:
            await message.answer("Нет предложений.")
        for row in rows:
            await message.answer(
                f"#{row['id']}: {row['title']}\n{row['url']}\n"
                f"Заголовок: {row['header_row']}, ФИО: {row['name_columns']}",
                reply_markup=moderation_keyboard(row["id"]),
            )

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
        if action == "approve":
            try:
                await monitor.fetch(row[0])
            except Exception:
                await callback.answer("Таблица недоступна или формат неверен", show_alert=True)
                return
            db.execute(
                "UPDATE sheets SET status='approved' WHERE id=? AND status='pending'", (int(sheet_id),)
            )
        else:
            db.execute("DELETE FROM sheets WHERE id=? AND status='pending'", (int(sheet_id),))
        await callback.answer("Принято" if action == "approve" else "Отклонено")
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)

    @router.message(Command("remove"))
    async def remove(message: Message, state: FSMContext):
        if message.from_user.id != db.admin_id():
            await message.answer("Эта команда недоступна.")
            return
        await state.clear()
        await state.set_state(Dialog.remove)
        await message.answer("Отправь номер таблицы, которую хочешь удалить. Номера есть в /tables.")

    @router.message(Dialog.registration, F.text, ~F.text.startswith("/"))
    @router.message(Dialog.rename, F.text, ~F.text.startswith("/"))
    async def save_name(message: Message, state: FSMContext):
        name = message.text.strip()
        if not valid_name(name):
            await message.answer("Нужно 2–3 полных слова. Например: Иванов Иван. Попробуй ещё раз.")
            return
        registering = await state.get_state() == Dialog.registration.state
        await state.set_state(Dialog.checking)
        try:
            async with monitor.lock:
                db.register(message.from_user.id, name)
            if not registering:
                await message.answer("Сохранил.")
                return
            await message.answer("Сохранил, проверяю по таблицам…")
            reports = await monitor.inspect_user(message.from_user.id, baseline=True)
            await answer_long(message, registration_summary(reports))
        finally:
            if await state.get_state() == Dialog.checking.state:
                await state.clear()

    @router.message(Dialog.title, F.text, ~F.text.startswith("/"))
    async def save_title(message: Message, state: FSMContext):
        title = message.text.strip()
        if not title or len(title) > 200:
            await message.answer("Название должно быть от 1 до 200 символов. Попробуй ещё раз.")
            return
        await state.update_data(title=title)
        await state.set_state(Dialog.link)
        await message.answer("Отправь ссылку на Google Sheets с доступом на чтение.")

    @router.message(Dialog.link, F.text, ~F.text.startswith("/"))
    async def save_link(message: Message, state: FSMContext):
        data = await state.get_data()
        direct = data["direct"]
        if direct and message.from_user.id != db.admin_id():
            await state.clear()
            await message.answer("Эта команда больше недоступна.")
            return
        try:
            url = export_url(message.text.strip())
            await fetch_sheet(monitor.session, url, data["header"], data["columns"])
            sheet_id = db.add_sheet(
                data["title"],
                url,
                data["header"],
                data["columns"],
                message.from_user.id,
                "approved" if direct else "pending",
            )
        except (SheetError, aiohttp.ClientError, UnicodeError, TimeoutError):
            await message.answer(
                "Не получилось спарсить таблицу. Проверь доступ по ссылке и попробуй ещё раз. "
                "Отправь другую ссылку или /cancel."
            )
            return
        except sqlite3.IntegrityError:
            await message.answer(
                "Эта таблица уже добавлена или ждёт проверки. Отправь другую ссылку или /cancel."
            )
            return
        await state.clear()
        if direct:
            await message.answer("Таблицу добавил.")
        else:
            await message.answer("Отправил таблицу админу на проверку.")
            try:
                await monitor.bot.send_message(
                    db.admin_id(),
                    f"Предложение #{sheet_id} от {message.from_user.id}\n{data['title']}\n{url}\n"
                    f"Заголовок: {data['header']}, столбцы ФИО: {data['columns']}",
                    reply_markup=moderation_keyboard(sheet_id),
                )
            except Exception:
                logging.getLogger(__name__).exception("Could not notify administrator")

    @router.message(Dialog.remove, F.text, ~F.text.startswith("/"))
    async def remove_id(message: Message, state: FSMContext):
        if message.from_user.id != db.admin_id():
            await state.clear()
            await message.answer("Эта команда больше недоступна.")
            return
        try:
            sheet_id = int(message.text.strip())
        except ValueError:
            await message.answer("Отправь номер таблицы числом или /cancel.")
            return
        async with monitor.lock:
            result = db.execute("DELETE FROM sheets WHERE id=?", (sheet_id,))
        if not result.rowcount:
            await message.answer("Не нашёл таблицу с таким номером. Попробуй ещё раз или /cancel.")
            return
        await state.clear()
        await message.answer("Удалил таблицу.")

    @router.message(F.text)
    async def text_message(message: Message, state: FSMContext):
        current = await state.get_state()
        prompts = {
            Dialog.registration.state: "Жду твоё ФИО. Для отмены — /cancel.",
            Dialog.rename.state: "Жду новое ФИО. Для отмены — /cancel.",
            Dialog.title.state: "Жду название предмета. Для отмены — /cancel.",
            Dialog.link.state: "Жду ссылку на таблицу. Для отмены — /cancel.",
            Dialog.remove.state: "Жду номер таблицы. Для отмены — /cancel.",
            Dialog.checking.state: "Проверяю таблицы, немного подожди.",
        }
        await message.answer(prompts.get(current, help_text(message.from_user.id)))

    @router.message()
    async def unsupported(message: Message):
        await message.answer("Отправь текстовое сообщение. Для помощи — /help.")

    return router


async def run_bot(db, token, interval):
    if db.admin_id() is None:
        raise ValueError("Назначьте админа: gradebot admin TELEGRAM_ID")
    async with Bot(token=token) as bot, aiohttp.ClientSession() as session:
        monitor = Monitor(db, session, bot)
        dispatcher = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
        dispatcher.include_router(create_router(db, monitor))
        task = asyncio.create_task(monitor.run(interval))
        try:
            await dispatcher.start_polling(bot)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

import asyncio
import contextlib
import sqlite3

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .monitor import Monitor
from .sheets import SheetError, export_url, valid_name

HELP = """/name Фамилия Имя Отчество — регистрация или смена ФИО
/tables — таблицы группы
/suggest Предмет | ссылка | строка заголовков | столбцы ФИО
Пример: /suggest Математика | https://docs.google.com/spreadsheets/d/ID/edit#gid=0 | 1 | 1
Если имя разделено по столбцам: 1,2,3. По умолчанию: заголовок 1, ФИО 1.
/cancel — отмена ввода ФИО
Для админа: /add (тот же формат), /pending, /remove ID"""


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
    waiting = set()

    @router.message(F.chat.type != "private")
    async def private_only(message: Message):
        await message.answer("Напишите боту в личные сообщения.")

    @router.message(CommandStart())
    async def start(message: Message):
        waiting.add(message.from_user.id)
        await message.answer("Введите ваши фамилию, имя и отчество. Можно фамилию и имя.\n" + HELP)

    @router.message(Command("help"))
    async def help_command(message: Message):
        await message.answer(HELP)

    @router.message(Command("cancel"))
    async def cancel(message: Message):
        waiting.discard(message.from_user.id)
        await message.answer("Ввод отменён.")

    async def register(message, name):
        if not valid_name(name):
            await message.answer("Введите 2–3 полных слова: фамилия, имя и при наличии отчество.")
            return
        waiting.discard(message.from_user.id)
        await message.answer("Проверяю ФИО по всем таблицам группы…")
        reports = await monitor.check_user(message.from_user.id, name)
        for report in reports:
            for offset in range(0, len(report), 4000):
                await message.answer(report[offset : offset + 4000])
        if not reports:
            await message.answer("Таблиц пока нет. После их добавления проверка начнётся автоматически.")

    @router.message(Command("name"))
    async def name_command(message: Message):
        name = (message.text or "").partition(" ")[2].strip()
        if name:
            await register(message, name)
        else:
            waiting.add(message.from_user.id)
            await message.answer("Введите ФИО:")

    @router.message(Command("tables"))
    async def tables(message: Message):
        rows = db.all("SELECT * FROM sheets WHERE status='approved'")
        if not rows:
            await message.answer("Таблиц пока нет.")
        for row in rows:
            await message.answer(f"{row['id']}. {row['title']}\n{row['url']}")

    @router.message(Command("add", "suggest"))
    async def add(message: Message):
        user_id = message.from_user.id
        if not db.all("SELECT id FROM users WHERE id=?", (user_id,)):
            await message.answer("Сначала зарегистрируйтесь: /name Фамилия Имя Отчество")
            return
        admin = user_id == db.admin_id()
        if message.text.split()[0].split("@")[0] == "/add" and not admin:
            await message.answer("Добавлять таблицы может только админ. Используйте /suggest.")
            return
        try:
            title, url, header, columns = parse_spec(message.text.partition(" ")[2])
            # Check accessibility and layout before accepting a submission.
            from .sheets import fetch_sheet

            await fetch_sheet(monitor.session, url, header, columns)
            sheet_id = db.add_sheet(title, url, header, columns, user_id, "approved" if admin else "pending")
        except (SheetError, aiohttp.ClientError, UnicodeError, TimeoutError) as exc:
            await message.answer(f"Не удалось добавить таблицу: {exc}")
            return
        except sqlite3.IntegrityError:
            await message.answer("Этот лист уже добавлен или ожидает модерации.")
            return
        if admin:
            await message.answer(f"Таблица #{sheet_id} добавлена. Оценки придут при следующей проверке.")
        else:
            await message.answer(f"Предложение #{sheet_id} сохранено и доступно админу в /pending.")
            try:
                await monitor.bot.send_message(
                    db.admin_id(),
                    f"Предложение #{sheet_id} от {user_id}\n{title}\n{url}\n"
                    f"Заголовок: {header}, столбцы ФИО: {columns}",
                    reply_markup=moderation_keyboard(sheet_id),
                )
            except Exception:
                # Submission persists even if the administrator has not started the bot yet.
                import logging

                logging.getLogger(__name__).exception("Could not notify administrator")

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
    async def remove(message: Message):
        if message.from_user.id != db.admin_id():
            await message.answer("Команда доступна админу.")
            return
        try:
            sheet_id = int(message.text.partition(" ")[2])
        except ValueError:
            await message.answer("Формат: /remove ID (ID из /tables)")
            return
        async with monitor.lock:
            result = db.execute("DELETE FROM sheets WHERE id=?", (sheet_id,))
        await message.answer("Удалено." if result.rowcount else "Таблица не найдена.")

    @router.message(F.text)
    async def text_message(message: Message):
        if message.from_user.id in waiting and not message.text.startswith("/"):
            await register(message, message.text.strip())
        else:
            await message.answer(HELP)

    return router


async def run_bot(db, token, interval):
    if db.admin_id() is None:
        raise ValueError("Назначьте админа: gradebot admin TELEGRAM_ID")
    async with Bot(token=token) as bot, aiohttp.ClientSession() as session:
        monitor = Monitor(db, session, bot)
        dispatcher = Dispatcher()
        dispatcher.include_router(create_router(db, monitor))
        task = asyncio.create_task(monitor.run(interval))
        try:
            await dispatcher.start_polling(bot)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

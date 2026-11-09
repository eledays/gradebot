import asyncio
import contextlib
import json
import logging
import secrets
import sqlite3
import time
from collections.abc import Awaitable, Callable, Sequence
from html import escape
from typing import Any, cast

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
    LinkPreviewOptions,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from .columns import PAGE_SIZE, ColumnPickerData, column_picker
from .db import Database, DuplicateTitleError, ExtensionWorkbook, WorkbookDraft
from .logging_setup import audit
from .messages import TrustedHtml, messages
from .monitor import GradeReport, Monitor, cache_caption, format_grades, subject_pages
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
from .structure import group_sheets, header_signature, sheet_group_title


def given_name(full_name: str) -> str:
    """Return the given name from a surname-first Russian full name."""
    parts = full_name.split()
    return parts[1] if len(parts) >= 2 else parts[0] if parts else ""


def help_message(interval: int, contact_username: str, *, admin: bool = False,
                 name: str | None = None, fetched_at: float | None = None) -> str:
    """Render the user help page from editable HTML templates."""
    if interval % 3600 == 0:
        period = f"{interval // 3600} ч"
    elif interval % 60 == 0:
        period = f"{interval // 60} мин"
    else:
        period = f"{interval} сек"
    greeting = (
        messages.render("help.greeting", person=given_name(name))
        if name else messages.render("help.greeting_anonymous")
    )
    parts = [greeting, messages.render("help.body", period=period)]
    if admin:
        parts.append(messages.render("help.admin"))
    contact = contact_username.strip().lstrip("@")
    if contact:
        parts.append(messages.render("help.contact", username=contact))
    parts.append(cache_caption(
        {"fetched_at": fetched_at, "stale": fetched_at is None or time.time() - fetched_at > interval * 2},
        label="Последнее обновление кэша",
    ))
    return "\n\n".join(parts)


class Dialog(StatesGroup):
    requested_name = State()
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
    edit_columns = State()
    columns_id = State()
    subject_id = State()
    addtab_id = State()
    remove = State()


def registration_summary(reports: list[GradeReport]) -> str:
    """Summarize missing, ambiguous, and unavailable grade reports."""
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


def parse_spec(text: str) -> tuple[str, str, int, tuple[int, ...]]:
    """Parse a manually entered sheet specification."""
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


def confirmation(action: str, token: str, label: str) -> InlineKeyboardMarkup:
    """Build a confirmation keyboard with a scoped token."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=label, callback_data=f"{action}:{token}"),
                InlineKeyboardButton(text="Отмена", callback_data=f"dismiss:{token}"),
            ]
        ]
    )


def menu(admin: bool = False) -> ReplyKeyboardMarkup:
    """Build the role-specific reply keyboard."""
    rows = [["Мои оценки", "Таблицы"], ["Уведомления", "Помощь"], ["Предложить таблицу"]]
    if admin:
        rows.extend(
            [
                ["Пользователи", "Создать ссылку"],
                ["Добавить таблицу", "Добавить лист"],
                ["Предложения"],
                ["Обновить", "Удалить таблицу"],
                ["Столбцы", "Заблокировать"],
            ]
        )
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=text) for text in row] for row in rows],
        resize_keyboard=True,
        input_field_placeholder="Выбери действие",
    )


class AccessMiddleware(BaseMiddleware):
    def __init__(self, db: Database, contact: str) -> None:
        """Store the access repository and public contact name."""
        self.db, self.contact = db, contact.strip().lstrip("@")

    async def __call__(self, handler: Callable[[Message | CallbackQuery, dict[str, Any]], Awaitable[object]],
                       event: Message | CallbackQuery, data: dict[str, Any]) -> object:
        """Reject unauthorized updates before dispatching their handlers."""
        user = event.from_user
        start = isinstance(event, Message) and bool(event.text) and event.text.split(maxsplit=1)[0] == "/start"
        pending = user and isinstance(event, Message) and bool(event.text) and (
            not event.text.startswith("/") or event.text == "/cancel"
        ) and bool(self.db.has_join_draft(user.id))
        if not user or (not self.db.authorized(user.id) and not (start or pending)):
            audit("access_denied", level=logging.DEBUG)
            text = messages.render("access.denied")
            if self.contact:
                text += f"\nДля связи: @{self.contact}"
            if isinstance(event, CallbackQuery):
                await event.answer(messages.render("access.denied"), show_alert=True)
            elif isinstance(event, Message) and event.chat.type == "private":
                await data["state"].clear()
                await event.answer(text, reply_markup=ReplyKeyboardRemove())
            return
        if isinstance(event, CallbackQuery) and isinstance(event.message, Message):
            if event.message.chat.type != "private":
                await event.answer(messages.render("access.private"), show_alert=True)
                return
        try:
            return await handler(event, data)
        except Exception as exc:
            audit("handler_failed", level=logging.ERROR, error=exc)
            raise


def create_router(db: Database, monitor: Monitor | None, contact_username: str = "") -> Router:
    """Register Telegram handlers for access, grades, and administration."""
    router = Router()
    guard = AccessMiddleware(db, contact_username)
    router.message.outer_middleware(guard)
    router.callback_query.outer_middleware(guard)
    menu_labels = {button.text for row in menu(True).keyboard for button in row}

    def help_text(user_id: int) -> str:
        """Render help with the current cache time and user role."""
        name = db.user_name(user_id)
        timestamp = db.latest_cache_timestamp()
        return help_message(
            monitor.interval,
            contact_username,
            admin=user_id == db.admin_id(),
            name=name,
            fetched_at=timestamp,
        )

    def registered(user_id: int) -> bool:
        """Check whether a user has an active profile."""
        return db.user_name(user_id) is not None

    async def show_sheet_id_prompt(message: Message, prompt_key: str, rows: Sequence[sqlite3.Row]) -> None:
        """Show a compact, paginated list of approved sheet IDs."""
        lines = [messages.render(prompt_key), ""]
        for row in rows:
            lines.extend(safe_lines(f"#{row['id']}: {db.grade_display_title(row['title'], row['url'])}"))
        await send_view(message, db, lines)

    @router.message(F.chat.type != "private")
    async def private_only(message: Message) -> None:
        """Handle the private only interaction."""
        await message.answer(messages.render("replies.private_only_1"))

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext) -> None:
        """Handle the start interaction."""
        await state.clear()
        user_id = message.from_user.id
        if db.blocked(user_id):
            await message.answer(messages.render("access.blocked"))
            return
        if user_id == db.admin_id() and db.member_name(user_id) is None:
            await message.answer(messages.render("replies.start_1"), reply_markup=menu(True))
            return
        if not db.authorized(user_id):
            token = (message.text or "").partition(" ")[2].strip()
            contact = contact_username.strip().lstrip("@")
            suffix = f" @{contact}" if contact else ""
            if not token:
                await message.answer(messages.render("access.invitation_required", contact=suffix))
                return
            if not db.invitation_valid(token):
                await message.answer(messages.render("access.expired", contact=suffix))
                return
            db.begin_join(user_id, token)
            await message.answer(
                messages.render("registration.intro"), parse_mode="HTML")
            return
        first = not registered(user_id)
        db.activate(user_id)
        name = db.member_name(user_id)
        if name is None:
            raise RuntimeError("Authorized member has no registered name")
        reports = await monitor.inspect_user(user_id, baseline=first)
        await message.answer(
            messages.render("registration.returning", first_name=given_name(name)),
            parse_mode="HTML",
            reply_markup=menu(user_id == db.admin_id()),
        )
        summary = registration_summary(reports)
        lines = safe_lines(summary) if summary else []
        for report in reports:
            if report["stale"] and report["fetched_at"] is not None:
                lines.append(cache_caption(report) + " " + escape(report["title"]))
        if lines:
            await send_view(message, db, lines)

    async def has_join_draft(message: Message) -> bool:
        """Check whether a message belongs to an invitation signup."""
        return db.has_join_draft(message.from_user.id)

    @router.message(has_join_draft, F.text, ~F.text.startswith("/"))
    async def join_name(message: Message, state: FSMContext) -> None:
        """Handle the join name interaction."""
        name = " ".join(message.text.split())
        if not valid_name(name):
            await message.answer(messages.render("registration.invalid_name"))
            return
        token = db.join_token(message.from_user.id)
        if token is None:
            return
        if not db.join(message.from_user.id, message.from_user.username, name, token):
            db.discard_join_draft(message.from_user.id)
            await state.clear()
            contact = contact_username.strip().lstrip("@")
            suffix = f" @{contact}" if contact else ""
            await message.answer(messages.render("access.expired", contact=suffix))
            return
        await state.clear()
        reports = await monitor.inspect_user(message.from_user.id, baseline=True)
        await message.answer(
            messages.render("registration.welcome", first_name=given_name(name)),
            parse_mode="HTML",
            reply_markup=menu(),
        )
        summary = registration_summary(reports)
        if summary:
            await send_view(message, db, safe_lines(summary))

    @router.message(F.text == "Помощь")
    @router.message(Command("help"))
    async def help_command(message: Message) -> None:
        """Handle the help command interaction."""
        await message.answer(help_text(message.from_user.id), parse_mode="HTML")

    @router.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext) -> None:
        """Handle the cancel interaction."""
        await state.clear()
        db.discard_join_draft(message.from_user.id)
        reply_markup = menu(message.from_user.id == db.admin_id()) if db.authorized(message.from_user.id) else ReplyKeyboardRemove()
        await message.answer(messages.render("replies.cancel_1"), reply_markup=reply_markup)

    @router.message(Command("name"))
    async def name_command(message: Message, state: FSMContext) -> None:
        """Handle the name command interaction."""
        if db.member_name(message.from_user.id) is None:
            await message.answer(messages.render("replies.name_command_1"))
            return
        await state.set_state(Dialog.requested_name)
        await message.answer(messages.render("replies.name_command_2"))

    @router.message(Dialog.requested_name, F.text, ~F.text.startswith("/"))
    async def requested_name(message: Message, state: FSMContext) -> None:
        """Handle the requested name interaction."""
        name = " ".join(message.text.split())
        if not valid_name(name):
            await message.answer(messages.render("registration.invalid_name"))
            return
        db.request_name(message.from_user.id, name)
        await state.clear()
        await message.answer(messages.render("replies.requested_name_1"))
        user_id = message.from_user.id
        profile = TrustedHtml(f'<a href="tg://user?id={user_id}">Открыть профиль</a>')
        await message.bot.send_message(
            db.admin_id(),
            messages.render(
                "replies.name_request_admin",
                old_name=db.member_name(user_id) or "не указано",
                new_name=name,
                telegram_id=user_id,
                username=f"@{message.from_user.username}" if message.from_user.username else "не указан",
                profile=profile,
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Одобрить", callback_data=f"name_approve:{message.from_user.id}"),
                InlineKeyboardButton(text="Отклонить", callback_data=f"name_reject:{message.from_user.id}"),
            ]]),
        )

    @router.callback_query(F.data.regexp(r"^name_(approve|reject):[0-9]+$"))
    async def approve_name(callback: CallbackQuery) -> None:
        """Handle the approve name interaction."""
        if callback.from_user.id != db.admin_id():
            await callback.answer(messages.render("replies.approve_name_1"), show_alert=True)
            return
        action, raw_id = callback.data.split(":")
        user_id = int(raw_id)
        requested = db.name_request(user_id)
        if requested is None or not db.authorized(user_id):
            await callback.answer(messages.render("replies.approve_name_2"), show_alert=True)
            return
        if action == "name_approve":
            db.change_name(user_id, requested)
            result = "Смена ФИО одобрена"
        else:
            db.discard_name_request(user_id)
            result = "Смена ФИО отклонена"
        await callback.answer(result)
        await callback.bot.send_message(user_id, result)
        if isinstance(callback.message, Message):
            await callback.message.edit_reply_markup(reply_markup=None)

    @router.message(F.text == "Уведомления")
    @router.message(Command("notifications"))
    async def notifications(message: Message) -> None:
        """Handle the notifications interaction."""
        if not registered(message.from_user.id):
            await message.answer(messages.render("replies.notifications_1"))
            return
        enabled = db.toggle_notifications(message.from_user.id)
        await message.answer(
            messages.render("notifications.enabled" if enabled else "notifications.disabled"),
            reply_markup=menu(message.from_user.id == db.admin_id()),
        )

    @router.message(F.text == "Мои оценки")
    @router.message(Command("grades"))
    async def grades(message: Message) -> None:
        """Handle the grades interaction."""
        if not registered(message.from_user.id):
            await message.answer(messages.render("replies.grades_1"))
            return
        reports = await monitor.inspect_user(message.from_user.id)
        lines = [bold("Текущие оценки"), ""]
        for group in group_sheets(reports):
            for report in group:
                lines.extend(format_grades(report, include_caption=False).splitlines())
                lines.append("")
        if reports:
            timestamps = [r["fetched_at"] for r in reports if r["fetched_at"] is not None]
            lines.append(cache_caption({
                "fetched_at": min(timestamps) if len(timestamps) == len(reports) else None,
                "stale": any(r["stale"] for r in reports),
            }))
        await send_view(message, db, lines if reports else ["Таблиц пока нет"])

    @router.message(Command("subject"))
    async def subject_command(message: Message, state: FSMContext) -> None:
        """Handle the subject command interaction."""
        if not registered(message.from_user.id):
            await message.answer(messages.render("replies.subject_command_1"))
            return
        await state.clear()
        rows = db.sheets_by_status("approved")
        if not rows:
            await message.answer(messages.render("replies.subject_command_2"))
            return
        await state.set_state(Dialog.subject_id)
        await show_sheet_id_prompt(message, "replies.subject_command_3", rows)

    @router.message(Dialog.subject_id, F.text, ~F.text.startswith("/"), ~F.text.in_(menu_labels))
    async def subject_id(message: Message, state: FSMContext) -> None:
        """Handle the subject id interaction."""
        if not registered(message.from_user.id):
            await state.clear()
            await message.answer(messages.render("replies.subject_id_1"))
            return
        try:
            sheet_id = int(message.text.strip())
            if not 0 < sheet_id < 2**63:
                raise ValueError
        except ValueError:
            await message.answer(messages.render("replies.subject_id_2"))
            return
        if await show_subject(message, message.from_user.id, sheet_id):
            await state.clear()

    async def show_subject(message: Message, user_id: int, sheet_id: int) -> bool:
        """Send one user-owned subject view when the sheet exists."""
        reports = await monitor.inspect_user(user_id)
        report = next((r for r in reports if r["sheet_id"] == sheet_id), None)
        if report is None:
            await message.answer(messages.render("replies.show_subject_1"))
            return False
        view_id, pages = create_view(db, user_id, [], pages=subject_pages(report))
        await message.answer(pages[0], parse_mode="HTML", reply_markup=view_markup(db, view_id),
                             link_preview_options=LinkPreviewOptions(is_disabled=True))
        return True

    @router.callback_query(F.data.regexp(r"^subject:\d+$"))
    async def subject(callback: CallbackQuery) -> None:
        """Handle the subject interaction."""
        if not registered(callback.from_user.id):
            await callback.answer(messages.render("replies.subject_1"), show_alert=True)
            return
        reports = await monitor.inspect_user(callback.from_user.id)
        report = next((r for r in reports if r["sheet_id"] == int(callback.data.split(":")[1])), None)
        if report is None:
            await callback.answer(messages.render("replies.subject_2"), show_alert=True)
            return
        await callback.answer()
        if isinstance(callback.message, Message):
            view_id, pages = create_view(db, callback.from_user.id, [], pages=subject_pages(report))
            await callback.message.answer(
                pages[0], parse_mode="HTML", reply_markup=view_markup(db, view_id),
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )

    @router.message(F.text == "Таблицы")
    @router.message(Command("tables"))
    async def tables(message: Message) -> None:
        """Handle the tables interaction."""
        rows = db.approved_sheets_with_cache_time()
        lines = [bold("Таблицы группы"), ""]
        now = time.time()
        for group in group_sheets(rows):
            prefix = ""
            if len(group) > 1:
                title = sheet_group_title(group)
                lines.append(bold(title or "Листы одной таблицы"))
                prefix = title + " · " if title else ""
            for row in group:
                title = row["title"].removeprefix(prefix)
                lines.append(f"#{row['id']}: {table_link(title, row['url'])}")
            timestamps = [row["fetched_at"] for row in group if row["fetched_at"] is not None]
            lines.append(cache_caption({
                "fetched_at": max(timestamps, default=None),
                "stale": any(row["fetched_at"] is None or now - row["fetched_at"] > monitor.interval * 2
                             for row in group),
            }))
            if timestamps and len(timestamps) != len(group):
                lines.append("Кэш загружен не для всех листов")
            lines.append("")
        await send_view(message, db, lines if rows else ["Таблиц пока нет"])

    @router.message(F.text == "Столбцы")
    @router.message(Command("columns"))
    async def columns_command(message: Message, state: FSMContext) -> None:
        """Handle the columns command interaction."""
        if message.from_user.id != db.admin_id():
            await message.answer(messages.render("replies.columns_command_1"))
            return
        await state.clear()
        rows = db.sheets_by_status("approved")
        if not rows:
            await message.answer(messages.render("replies.columns_command_2"))
            return
        await state.set_state(Dialog.columns_id)
        await show_sheet_id_prompt(message, "replies.columns_command_3", rows)

    @router.message(Dialog.columns_id, F.text, ~F.text.startswith("/"), ~F.text.in_(menu_labels))
    async def columns_id(message: Message, state: FSMContext) -> None:
        """Handle the columns id interaction."""
        if message.from_user.id != db.admin_id():
            await state.clear()
            await message.answer(messages.render("replies.columns_id_1"))
            return
        try:
            sheet_id = int(message.text.strip())
            if not 0 < sheet_id < 2**63:
                raise ValueError
        except ValueError:
            await message.answer(messages.render("replies.columns_id_2"))
            return
        await open_saved_columns(message, state, sheet_id)

    @router.callback_query(F.data.regexp(r"^pick_columns:\d+$"))
    async def edit_columns(callback: CallbackQuery, state: FSMContext) -> None:
        """Handle the edit columns interaction."""
        if callback.from_user.id != db.admin_id():
            await callback.answer(messages.render("replies.edit_columns_1"), show_alert=True)
            return
        await callback.answer()
        if isinstance(callback.message, Message):
            await open_saved_columns(callback.message, state, int(callback.data.split(":")[1]))

    async def open_saved_columns(message: Message, state: FSMContext, sheet_id: int) -> None:
        """Load cached column choices for an approved sheet."""
        sheet = db.sheet(sheet_id, "approved")
        if sheet is None:
            await message.answer(messages.render("replies.open_saved_columns_1"))
            return
        cached = db.cached_rows(sheet_id)
        if cached is None:
            await message.answer(messages.render("replies.open_saved_columns_2"))
            return
        options = json.loads(sheet["parser_options"])
        raw_rows = json.loads(cached)
        try:
            parsed = Sheet(raw_rows, sheet["header_row"], tuple(json.loads(sheet["name_columns"])), **options)
            all_columns = Sheet(raw_rows, sheet["header_row"], parsed.name_columns,
                                options.get("header_start") or sheet["header_row"])
        except SheetError as exc:
            await message.answer(str(exc))
            return
        await state.clear()
        await state.set_state(Dialog.edit_columns)
        await state.update_data(edit_sheet_id=sheet_id, edit_options=sheet["parser_options"],
                                edit_signature=header_signature(raw_rows, sheet["header_row"],
                                                                options.get("header_start")))
        await open_column_picker(message, state, all_columns.labels, parsed.labels, sheet["title"])

    async def open_column_picker(message: Message, state: FSMContext, labels: dict[int, str],
                                 selected: dict[int, str], title: str) -> None:
        """Show selectable grade columns."""
        await state.update_data(column_labels=labels, column_selected=list(selected), column_page=0,
                                column_title=title, column_token=secrets.token_hex(8))
        text, markup = column_picker(cast(ColumnPickerData, await state.get_data()))
        await message.answer(text, parse_mode="HTML", reply_markup=markup)

    @router.callback_query(F.data.regexp(r"^colpick:[a-f0-9]{16}:(\d+|page\d+|all|done|cancel)$"))
    async def select_column(callback: CallbackQuery, state: FSMContext) -> None:
        """Handle the select column interaction."""
        if callback.from_user.id != db.admin_id():
            await callback.answer(messages.render("replies.select_column_1"), show_alert=True)
            return
        _, token, action = callback.data.split(":")
        data = await state.get_data()
        if (token != data.get("column_token") or await state.get_state() not in {
            Dialog.sheet_columns.state, Dialog.edit_columns.state,
        }):
            await callback.answer(messages.render("replies.select_column_2"))
            return
        if not isinstance(callback.message, Message):
            await callback.answer()
            return
        selected = set(data["column_selected"])
        labels = {int(i): label for i, label in data["column_labels"].items()}
        if action in {"done", "cancel"}:
            if action == "done":
                try:
                    if data.get("edit_sheet_id"):
                        async with monitor.lock:
                            db.update_grade_columns(data["edit_sheet_id"], selected,
                                                    expected_options=data["edit_options"],
                                                    expected_signature=data["edit_signature"])
                    else:
                        await state.update_data(grade_columns=sorted(selected))
                except SheetError as exc:
                    await callback.answer(str(exc)[:180], show_alert=True)
                    return
            await callback.answer("Сохранено" if action == "done" else "Отменено")
            await callback.message.edit_reply_markup(reply_markup=None)
            if data.get("edit_sheet_id"):
                await state.clear()
                await callback.message.answer(
                    f"Столбцы «{data['column_title']}» сохранены. Отслеживается: {len(selected)}"
                    if action == "done" else "Изменения отменены", reply_markup=menu(True))
            else:
                await state.update_data(column_token=None)
                await show_columns(callback.message, state)
            return
        if action == "all":
            selected = set(labels)
        elif action.startswith("page"):
            page = int(action[4:])
            if page * PAGE_SIZE >= len(labels):
                await callback.answer(messages.render("replies.select_column_3"))
                return
            await state.update_data(column_page=page)
        else:
            index = int(action)
            if index not in labels:
                await callback.answer(messages.render("replies.select_column_4"))
                return
            selected.symmetric_difference_update({index})
            if not selected:
                await callback.answer(messages.render("replies.select_column_5"), show_alert=True)
                return
        await state.update_data(column_selected=sorted(selected))
        await callback.answer()
        text, markup = column_picker(cast(ColumnPickerData, await state.get_data()))
        try:
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc):
                raise

    @router.message(F.text.in_({"Добавить таблицу", "Предложить таблицу"}))
    @router.message(Command("add", "suggest"))
    async def add(message: Message, state: FSMContext) -> None:
        """Handle the add interaction."""
        user_id = message.from_user.id
        direct = message.text == "Добавить таблицу" or message.text.split()[0].split("@")[0] == "/add"
        if direct and user_id != db.admin_id():
            await message.answer(messages.render("replies.add_1"))
            return
        if not registered(user_id) and user_id != db.admin_id():
            await message.answer(messages.render("replies.add_2"))
            return
        await state.clear()
        await state.update_data(direct=direct, header=1, columns=(1,))
        await state.set_state(Dialog.title)
        await message.answer(messages.render("replies.add_3"))

    @router.message(F.text == "Добавить лист")
    @router.message(Command("addtab"))
    async def add_tab(message: Message, state: FSMContext) -> None:
        """Ask for a sheet ID to add new tabs to its workbook."""
        if message.from_user.id != db.admin_id():
            await message.answer(messages.render("replies.add_1"))
            return
        await state.clear()
        rows = db.sheets_by_status("approved")
        if not rows:
            await message.answer(messages.render("replies.add_tab_empty"))
            return
        await state.set_state(Dialog.addtab_id)
        await show_sheet_id_prompt(message, "replies.add_tab_prompt", rows)

    @router.message(Dialog.addtab_id, F.text, ~F.text.startswith("/"), ~F.text.in_(menu_labels))
    async def add_tab_id(message: Message, state: FSMContext) -> None:
        """Load the workbook identified by an approved sheet ID."""
        if message.from_user.id != db.admin_id():
            await state.clear()
            await message.answer(messages.render("replies.add_1"))
            return
        try:
            sheet_id = int(message.text.strip())
            if not 0 < sheet_id < 2**63:
                raise ValueError
        except ValueError:
            await message.answer(messages.render("replies.add_tab_id_invalid"))
            return
        source = db.extension_source(sheet_id)
        if source is None:
            await message.answer(messages.render("replies.add_tab_id_missing"))
            return
        await begin_extension(message, state, source)

    async def begin_extension(message: Message, state: FSMContext, source: ExtensionWorkbook) -> None:
        """Start discovering tabs of an existing approved workbook."""
        await state.clear()
        await state.update_data(title=source["title"], direct=True, extension_source=source["id"])
        try:
            await begin_workbook(message, state, source["url"], existing_urls=set(source["existing_urls"]))
        except (SheetError, aiohttp.ClientError, UnicodeError, TimeoutError) as exc:
            await state.clear()
            await message.answer(messages.render("replies.add_tab_failed", reason=str(exc)))

    @router.callback_query(F.data.regexp(r"^extend:\d+$"))
    async def extend_workbook(callback: CallbackQuery, state: FSMContext) -> None:
        """Discover unregistered tabs in the selected approved workbook."""
        if callback.from_user.id != db.admin_id():
            await callback.answer(messages.render("replies.add_1"), show_alert=True)
            return
        source = db.extension_source(int(callback.data.split(":")[1]))
        if source is None:
            await callback.answer(messages.render("replies.add_tab_missing"), show_alert=True)
            return
        await callback.answer()
        if not isinstance(callback.message, Message):
            return
        await begin_extension(callback.message, state, source)

    @router.message(Command("layout"))
    async def layout(message: Message, state: FSMContext) -> None:
        # Optional advanced configuration; the normal flow only asks for title and link.
        """Handle the layout interaction."""
        if await state.get_state() != Dialog.link.state:
            await message.answer(messages.render("replies.layout_1"))
            return
        try:
            _, _, header, columns = parse_spec(
                "Настройка | https://docs.google.com/spreadsheets/d/placeholder/edit | "
                + message.text.partition(" ")[2]
            )
        except SheetError:
            await message.answer(messages.render("replies.layout_2"))
            return
        await state.update_data(header=header, columns=columns)
        await message.answer(messages.render("replies.layout_3"))

    @router.message(F.text == "Обновить")
    @router.message(Command("refresh"))
    async def refresh(message: Message) -> None:
        """Handle the refresh interaction."""
        if message.from_user.id != db.admin_id():
            await message.answer(messages.render("replies.refresh_1"))
            return
        await message.answer(messages.render("replies.refresh_2"))
        try:
            result = await monitor.scan()
        except Exception:
            await message.answer(messages.render("replies.refresh_3"))
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
    async def pending(message: Message) -> None:
        """Handle the pending interaction."""
        if message.from_user.id != db.admin_id():
            await message.answer(messages.render("replies.pending_1"))
            return
        rows = db.sheets_by_status("pending")
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
    async def page(callback: CallbackQuery) -> None:
        """Handle the page interaction."""
        _, view_id, index = callback.data.split(":")
        view = db.view(view_id)
        if (
            view is None
            or view["owner"] != callback.from_user.id
            or (view["admin"] and callback.from_user.id != db.admin_id())
        ):
            await callback.answer(messages.render("replies.page_1"), show_alert=True)
            return
        pages, index = json.loads(view["pages"]), int(index)
        if index >= len(pages):
            await callback.answer(messages.render("replies.page_2"))
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
    async def moderate(callback: CallbackQuery, state: FSMContext) -> None:
        """Handle the moderate interaction."""
        if callback.from_user.id != db.admin_id():
            await callback.answer(messages.render("replies.moderate_1"), show_alert=True)
            return
        action, sheet_id = callback.data.split(":")
        sheet = db.sheet(int(sheet_id), "pending")
        if sheet is None:
            await callback.answer(messages.render("replies.moderate_2"))
            return
        if action == "approve" and "tabs" in json.loads(sheet["parser_options"]):
            await configure_proposal(callback, state)
            return
        parsed = None
        if action == "approve":
            await callback.answer(messages.render("replies.moderate_3"))
            if isinstance(callback.message, Message):
                await callback.message.answer(messages.render("replies.moderate_4"))
            try:
                parsed = await monitor.fetch(sheet)
            except Exception as exc:
                audit("moderation_failed", level=logging.WARNING, sheet_id=int(sheet_id), error=exc)
                if isinstance(callback.message, Message):
                    await callback.message.answer(messages.render("replies.moderate_5"))
                return
        async with monitor.lock:
            if callback.from_user.id != db.admin_id():
                if isinstance(callback.message, Message):
                    await callback.message.answer(messages.render("replies.moderate_6"))
                return
            if not db.moderate_sheet(int(sheet_id), action == "approve", parsed):
                if action == "approve" and isinstance(callback.message, Message):
                    await callback.message.answer(messages.render("replies.moderate_7"))
                elif action != "approve":
                    await callback.answer(messages.render("replies.moderate_8"))
                return
        audit(
            "moderation_finished",
            sheet_id=int(sheet_id),
            result="approved" if action == "approve" else "rejected",
        )
        if action == "approve":
            if isinstance(callback.message, Message):
                await callback.message.answer(messages.render("replies.moderate_9"))
        else:
            await callback.answer(messages.render("replies.moderate_10"))
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
    async def remove(message: Message, state: FSMContext) -> None:
        """Handle the remove interaction."""
        if message.from_user.id != db.admin_id():
            await message.answer(messages.render("replies.remove_1"))
            return
        await state.clear()
        rows = db.sheets_by_status("approved")
        if not rows:
            await message.answer(messages.render("replies.columns_command_2"))
            return
        await state.set_state(Dialog.remove)
        await show_sheet_id_prompt(message, "replies.remove_prompt", rows)

    @router.message(F.text == "Пользователи")
    @router.message(Command("members"))
    async def members(message: Message) -> None:
        """Handle the members interaction."""
        if message.from_user.id != db.admin_id():
            await message.answer(messages.render("replies.members_1"))
            return
        rows = db.member_rows()
        lines = [bold("Пользователи группы"), ""]
        for row in rows:
            label = f"@{row['username']}" if row["username"] else "без юзернейма"
            anchor = f"#{row['id']}:"
            lines.extend(safe_lines(f"{anchor} {row['name']} · {label} · ID {row['id']}"))
            lines.append("Вход выполнен" if row["active"] else "Ещё не заходил")
            lines.append("")
        await send_view(
            message,
            db,
            lines if rows else ["Список пуст"],
            admin=True,
        )

    @router.message(F.text == "Создать ссылку")
    @router.message(Command("invite"))
    async def invite(message: Message) -> None:
        """Handle the invite interaction."""
        if message.from_user.id != db.admin_id():
            await message.answer(messages.render("replies.invite_1"))
            return
        argument = (message.text or "").partition(" ")[2].strip()
        try:
            hours = int(argument) if argument else 24
            if not 1 <= hours <= 720:
                raise ValueError
        except ValueError:
            await message.answer(messages.render("replies.invite_2"))
            return
        username = (await message.bot.get_me()).username
        token = db.create_invitation(hours * 3600)

        await message.answer(
            f"Ссылка действует {hours} ч:\nhttps://t.me/{username}?start={token}\n\nЕё можно отправить в чат"
        )

    @router.message(Command("rename"))
    async def rename_member(message: Message) -> None:
        """Handle the rename member interaction."""
        if message.from_user.id != db.admin_id():
            await message.answer(messages.render("replies.rename_member_1"))
            return
        parts = (message.text or "").split(maxsplit=2)
        if len(parts) != 3 or not parts[1].isdigit() or not valid_name(" ".join(parts[2].split())):
            await message.answer(messages.render("replies.rename_member_2"))
            return
        user_id, name = int(parts[1]), " ".join(parts[2].split())
        if not db.change_name(user_id, name):
            await message.answer(messages.render("replies.rename_member_3"))
            return
        await message.answer(messages.render("replies.rename_member_4"))
        await message.bot.send_message(user_id, f"Администратор изменил ФИО на {name}")

    @router.message(F.text == "Заблокировать")
    @router.message(Command("revoke"))
    async def revoke(message: Message, state: FSMContext) -> None:
        """Remove a member, their active state, and queued messages."""
        if message.from_user.id != db.admin_id():
            await message.answer(messages.render("replies.revoke_1"))
            return
        await state.clear()
        await state.set_state(Dialog.revoke)
        if not db.has_other_members():
            await state.clear()
            await message.answer(messages.render("replies.revoke_2"))
            return
        await message.answer(messages.render("replies.revoke_3"))

    @router.message(Dialog.revoke, F.text, ~F.text.startswith("/"))
    async def revoke_id(message: Message, state: FSMContext) -> None:
        """Handle the revoke id interaction."""
        if message.from_user.id != db.admin_id():
            await state.clear()
            return
        try:
            user_id = int(message.text.strip())
            if not 0 < user_id < 2**63:
                raise ValueError
        except ValueError:
            await message.answer(messages.render("replies.revoke_id_1"))
            return
        await ask_revoke(message, state, user_id)

    async def ask_revoke(message: Message, state: FSMContext, user_id: int) -> None:
        """Ask the administrator to confirm a member block."""
        name = db.member_name(user_id)
        if name is None or user_id == db.admin_id():
            await message.answer(messages.render("replies.ask_revoke_1"))
            return
        token = secrets.token_hex(8)
        await state.update_data(member_id=user_id, token=token)
        await state.set_state(Dialog.revoke_confirm)
        await message.answer(
            f"Заблокировать {name}?",
            reply_markup=confirmation("revoke", token, "Заблокировать"),
        )

    @router.callback_query(F.data.regexp(r"^(remove|revoke|dismiss):[a-f0-9]{16}$"))
    async def confirm(callback: CallbackQuery, state: FSMContext) -> None:
        """Handle the confirm interaction."""
        if callback.from_user.id != db.admin_id():
            await callback.answer(messages.render("replies.confirm_1"), show_alert=True)
            return
        action, token = callback.data.split(":")
        data = await state.get_data()
        states = {
            "remove": Dialog.remove_confirm.state,
            "revoke": Dialog.revoke_confirm.state,
        }
        current = await state.get_state()
        if data.get("token") != token or (action != "dismiss" and states[action] != current):
            await callback.answer(messages.render("replies.confirm_2"), show_alert=True)
            return
        if action == "dismiss":
            result = "Отменил"
        else:
            async with monitor.lock:
                if action == "remove":
                    deleted = db.remove_approved_sheet(data["sheet_id"])
                    result = "Таблица удалена" if deleted else "Таблица уже удалена"
                    audit("sheet_removed", sheet_id=data["sheet_id"], count=deleted)
                else:
                    if data["member_id"] == db.admin_id():
                        await callback.answer(messages.render("replies.confirm_3"), show_alert=True)
                        return
                    db.block(data["member_id"])
                    result = "Пользователь заблокирован"
        await state.clear()
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer(result, reply_markup=menu(True))

    async def setup_view(message: Message, lines: Sequence[str]) -> None:
        """Send a paginated administrator setup view."""
        view_id, pages = create_view(db, db.admin_id(), safe_lines("\n".join(lines)), admin=True)
        await message.answer(pages[0], parse_mode="HTML", reply_markup=view_markup(db, view_id),
                             link_preview_options=LinkPreviewOptions(is_disabled=True))

    def proposal_picker(identifier: int, data: dict[str, Any],
                        page: int = 0) -> InlineKeyboardMarkup:
        """Build the keyboard for selecting proposal tabs."""
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
    async def proposal_select(callback: CallbackQuery) -> None:
        """Handle the proposal select interaction."""
        if callback.from_user.id != db.admin_id():
            await callback.answer(messages.render("replies.proposal_select_1"), show_alert=True)
            return
        action, identifier, value = callback.data.split(":")
        options = db.parser_options(int(identifier), "pending")
        if options is None:
            await callback.answer(messages.render("replies.proposal_select_2"))
            return
        data = json.loads(options)
        if "tabs" not in data:
            await callback.answer(messages.render("replies.proposal_select_3"))
            return
        selected = set(data["selected"])
        page = data.get("page", 0)
        if action == "ppage":
            page = int(value)
            if not 0 <= page < (len(data["tabs"]) + 14) // 15:
                await callback.answer(messages.render("replies.proposal_select_4"))
                return
        elif value == "all":
            selected = set() if len(selected) == len(data["tabs"]) else set(range(len(data["tabs"])))
        else:
            index = int(value)
            if index >= len(data["tabs"]):
                await callback.answer(messages.render("replies.proposal_select_5"))
                return
            selected.symmetric_difference_update({index})
        data.update(selected=sorted(selected), page=page)
        db.set_parser_options(int(identifier), data)
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.edit_reply_markup(reply_markup=proposal_picker(identifier, data, page))

    def sheet_picker(data: dict[str, Any], page: int = 0) -> InlineKeyboardMarkup:
        """Build the keyboard for selecting workbook tabs."""
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

    async def begin_workbook(message: Message, state: FSMContext, url: str, *,
                             existing_urls: set[str] | None = None) -> None:
        """Start guided workbook setup from a sheet URL."""
        all_tabs = await discover_sheets(monitor.session, url)
        tabs = [tab for tab in all_tabs if sheet_url(tab["url"]) not in existing_urls] if existing_urls else all_tabs
        if not tabs:
            await state.clear()
            await message.answer(messages.render("replies.add_tab_none"))
            return
        data = await state.get_data()
        title = data.get("title", "")
        if existing_urls and len(existing_urls) == 1:
            old_tab = next((tab for tab in all_tabs if sheet_url(tab["url"]) in existing_urls), None)
            if old_tab and title.endswith(" · " + old_tab["name"]):
                title = title.removesuffix(" · " + old_tab["name"])
        await state.update_data(title=title, tabs=tabs, all_tabs=all_tabs, selected=list(range(len(tabs))),
                                setup_token=secrets.token_hex(8),
                                tab_page=0, configured=[])
        await state.set_state(Dialog.sheet_selection)
        await message.answer(messages.render("replies.begin_workbook_1"),
                             reply_markup=sheet_picker(await state.get_data()))

    async def configure_next(message: Message, state: FSMContext) -> None:
        """Advance to the next selected workbook tab."""
        data = await state.get_data()
        remaining = [i for i in data["selected"] if i not in [c["index"] for c in data["configured"]]]
        if not remaining:
            await state.set_state(Dialog.workbook_confirm)
            lines = ["Готово к сохранению:"]
            for item in data["configured"]:
                lines.append(f"{item['title']}: {len(item['grade_columns'])} столбцов оценок")
            await setup_view(message, lines)
            await message.answer(messages.render("replies.configure_next_1"), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
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
    async def select_tabs(callback: CallbackQuery, state: FSMContext) -> None:
        """Handle the select tabs interaction."""
        data = await state.get_data()
        action, token, value = callback.data.split(":")
        if callback.from_user.id != db.admin_id():
            await callback.answer(messages.render("replies.select_tabs_1"), show_alert=True)
            return
        if await state.get_state() != Dialog.sheet_selection.state or token != data.get("setup_token"):
            await callback.answer(messages.render("replies.select_tabs_2"))
            return
        selected = set(data["selected"])
        if action == "tabpage":
            page = int(value)
            if not 0 <= page < (len(data["tabs"]) + 14) // 15:
                await callback.answer(messages.render("replies.select_tabs_3"))
                return
            await state.update_data(tab_page=page)
        elif value == "next":
            if not selected:
                await callback.answer(messages.render("replies.select_tabs_4"), show_alert=True)
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
                await callback.answer(messages.render("replies.select_tabs_5"))
                return
            selected.symmetric_difference_update({index})
        await state.update_data(selected=sorted(selected))
        await callback.answer()
        if isinstance(callback.message, Message):
            data = await state.get_data()
            await callback.message.edit_reply_markup(reply_markup=sheet_picker(data, data["tab_page"]))

    async def skip_current(message: Message, state: FSMContext) -> None:
        """Skip the current workbook tab."""
        data = await state.get_data()
        await state.update_data(selected=[i for i in data["selected"] if i != data["current_index"]])
        if not (await state.get_data())["selected"]:
            await state.clear()
            await message.answer(messages.render("replies.skip_current_1"))
            return
        await configure_next(message, state)

    @router.message(Dialog.sheet_retry, F.text, ~F.text.startswith("/"))
    async def retry_sheet(message: Message, state: FSMContext) -> None:
        """Handle the retry sheet interaction."""
        if message.from_user.id != db.admin_id():
            await state.clear()
            return
        if message.text.strip().casefold() == "пропустить":
            await skip_current(message, state)
        else:
            await configure_next(message, state)

    def setup_keyboard(data: dict[str, Any]) -> InlineKeyboardMarkup:
        """Build the keyboard for parser setup."""
        prefix = f"sheetcfg:{data['setup_token']}:{data['current_index']}"
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Далее →", callback_data=prefix + ":next")],
            [InlineKeyboardButton(text="Только итоги", callback_data=prefix + ":totals"),
             InlineKeyboardButton(text="Все оценки", callback_data=prefix + ":all")],
            [InlineKeyboardButton(text="Исключить столбцы", callback_data=prefix + ":details"),
             InlineKeyboardButton(text="Исправить структуру", callback_data=prefix + ":layout")],
            [InlineKeyboardButton(text="Пропустить лист", callback_data=prefix + ":skip")],
        ])

    async def request_layout(message: Message, state: FSMContext) -> None:
        """Ask for header and name column layout."""
        data = await state.get_data()
        await state.set_state(Dialog.sheet_layout)
        # A compact header-only sample is enough for manual correction.
        lines = ["Укажи строки заголовков и ФИО, например: 1-3 | B"]
        for n, row in enumerate(data["raw_rows"][:min(4, data.get("header", 3))], 1):
            cells = [f"{column_letter(i)}: {value[:45]}" for i, value in enumerate(row, 1) if value.strip()]
            lines.append(f"Строка {n}: " + "; ".join(cells[:5]) + ("; …" if len(cells) > 5 else ""))
        await message.answer("\n".join(lines))

    @router.callback_query(F.data.regexp(r"^sheetcfg:[a-f0-9]{16}:\d+:(next|totals|all|details|layout|skip)$"))
    async def sheet_setup_action(callback: CallbackQuery, state: FSMContext) -> None:
        """Handle the sheet setup action interaction."""
        data = await state.get_data()
        _, token, index, action = callback.data.split(":")
        if callback.from_user.id != db.admin_id():
            await callback.answer(messages.render("replies.sheet_setup_action_1"), show_alert=True)
            return
        if (token != data.get("setup_token") or int(index) != data.get("current_index")
                or await state.get_state() != Dialog.sheet_columns.state):
            await callback.answer(messages.render("replies.sheet_setup_action_2"))
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
                await callback.message.answer(messages.render("replies.sheet_setup_action_3"))
                return
            await state.update_data(grade_columns=selected)
            await show_columns(callback.message, state)

    async def accept_current(message: Message, state: FSMContext) -> None:
        """Validate and store the current tab configuration."""
        data = await state.get_data()
        tab = data["tabs"][data["current_index"]]
        title = (data["title"] if len(data["tabs"]) == 1 and not data.get("extension_source")
                 else f"{data['title']} · {tab['name']}")
        configured = data["configured"] + [{
            "index": data["current_index"], "title": title, "url": tab["url"],
            "header": data["header"], "columns": data["columns"],
            "header_start": data["header_start"], "grade_columns": data["grade_columns"],
            "rows": data["raw_rows"],
        }]
        await state.update_data(configured=configured)
        await configure_next(message, state)

    async def show_columns(message: Message, state: FSMContext, *, detailed: bool = False) -> bool:
        """Display grade columns for the current tab."""
        data = await state.get_data()
        try:
            parsed = Sheet(data["raw_rows"], data["header"], tuple(data["columns"]),
                           data["header_start"], tuple(data["grade_columns"]))
        except SheetError as exc:
            await message.answer(str(exc))
            return False
        if detailed:
            all_columns = Sheet(data["raw_rows"], data["header"], tuple(data["columns"]), data["header_start"])
            await open_column_picker(message, state, all_columns.labels, parsed.labels,
                                     data["tabs"][data["current_index"]]["name"])
            return True
        await state.update_data(column_token=None)
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
    async def workbook_layout(message: Message, state: FSMContext) -> None:
        """Handle the workbook layout interaction."""
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
            await message.answer(messages.render("replies.workbook_layout_1"))
            return
        await state.set_state(Dialog.sheet_columns)

    @router.message(Dialog.sheet_columns, F.text, ~F.text.startswith("/"))
    async def workbook_columns(message: Message, state: FSMContext) -> None:
        """Handle the workbook columns interaction."""
        if message.from_user.id != db.admin_id():
            await state.clear()
            return
        data = await state.get_data()
        text = message.text.strip().casefold()
        if text == "назад":
            await state.set_state(Dialog.sheet_layout)
            await message.answer(messages.render("replies.workbook_columns_1"))
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
    async def save_workbook(callback: CallbackQuery, state: FSMContext) -> None:
        """Handle the save workbook interaction."""
        data = await state.get_data()
        if callback.from_user.id != db.admin_id():
            await callback.answer(messages.render("replies.save_workbook_1"), show_alert=True)
            return
        if (await state.get_state() != Dialog.workbook_confirm.state
                or callback.data.split(":")[1] != data.get("setup_token")):
            await callback.answer(messages.render("replies.save_workbook_2"))
            return
        try:
            async with monitor.lock:
                db.save_workbook(cast(WorkbookDraft, data), callback.from_user.id)
        except (DuplicateTitleError, sqlite3.IntegrityError, SheetError) as exc:
            await callback.answer(messages.render("replies.save_workbook_3"), show_alert=True)
            if isinstance(callback.message, Message):
                await callback.message.answer(f"Название или ссылка уже добавлены, либо предложение обработано: {exc}\n"
                                               "Ничего не сохранено. /cancel — отменить и начать заново")
            return
        await state.clear()
        await callback.answer(messages.render("replies.save_workbook_4"))
        if isinstance(callback.message, Message):
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer(f"Добавлено листов: {len(data['configured'])}", reply_markup=menu(True))
        await monitor.deliver()

    @router.callback_query(F.data.regexp(r"^configure:\d+$"))
    async def configure_proposal(callback: CallbackQuery, state: FSMContext) -> None:
        """Handle the configure proposal interaction."""
        if callback.from_user.id != db.admin_id():
            await callback.answer(messages.render("replies.configure_proposal_1"), show_alert=True)
            return
        row = db.sheet(int(callback.data.split(":")[1]), "pending")
        if row is None:
            await callback.answer(messages.render("replies.configure_proposal_2"))
            return
        await callback.answer()
        if isinstance(callback.message, Message):
            await state.clear()
            await state.update_data(title=row["title"], url=row["url"], direct=True,
                                    proposal_id=row["id"], proposer=row["proposer"])
            try:
                selection = json.loads(row["parser_options"])
                if "tabs" in selection:
                    if not selection["selected"]:
                        await callback.message.answer(messages.render("replies.configure_proposal_3"))
                        return
                    await state.update_data(tabs=selection["tabs"], selected=selection["selected"],
                                            configured=[], setup_token=secrets.token_hex(8), tab_page=0)
                    await configure_next(callback.message, state)
                else:
                    await begin_workbook(callback.message, state, row["url"])
            except (SheetError, aiohttp.ClientError, UnicodeError, TimeoutError) as exc:
                await callback.message.answer(f"Не удалось найти листы: {exc}. Нажми «Настроить» ещё раз")

    @router.message(Dialog.title, F.text, ~F.text.startswith("/"))
    async def save_title(message: Message, state: FSMContext) -> None:
        """Handle the save title interaction."""
        title = message.text.strip()
        if not title or len(title) > 200 or "\n" in title:
            await message.answer(messages.render("replies.save_title_1"))
            return
        if db.title_exists(title):
            await message.answer(
                messages.render("replies.save_title_2")
            )
            return
        await state.update_data(title=title)
        await state.set_state(Dialog.link)
        await message.answer(messages.render("replies.save_title_3"))

    @router.message(Dialog.link, F.text, ~F.text.startswith("/"))
    async def save_link(message: Message, state: FSMContext) -> None:
        """Handle the save link interaction."""
        data = await state.get_data()
        direct = data["direct"]
        if direct and message.from_user.id != db.admin_id():
            await state.clear()
            await message.answer(messages.render("replies.save_link_1"))
            return
        try:
            url = sheet_url(message.text.strip())
            await message.answer(messages.render("replies.save_link_2"))
            if direct:
                if db.title_exists(data["title"]):
                    raise DuplicateTitleError(data["title"])
                await begin_workbook(message, state, url)
                return
            tabs = await discover_sheets(monitor.session, url)
            parsed = None
            if not db.authorized(message.from_user.id) or (direct and message.from_user.id != db.admin_id()):
                await state.clear()
                await message.answer(messages.render("replies.save_link_3"))
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
            markup = proposal_picker(proposal_id, selection)
            actions = [[(button.text, button.callback_data) for button in row]
                       for row in markup.inline_keyboard]
            db.save_proposal_selection(proposal_id, selection, db.admin_id(),
                                       data["title"], url, message.from_user.id, actions)
        except (SheetError, aiohttp.ClientError, UnicodeError, TimeoutError) as exc:
            audit("sheet_input_failed", level=logging.WARNING, error=exc)
            await message.answer(f"Не удалось прочитать таблицу: {exc}")
            return
        except DuplicateTitleError:
            await state.set_state(Dialog.title)
            await message.answer(messages.render("replies.save_link_4"))
            return
        except sqlite3.IntegrityError:
            await message.answer(messages.render("replies.save_link_5"))
            return
        await state.clear()
        if direct:
            await message.answer(messages.render("replies.save_link_6"))
        else:
            await message.answer(messages.render("replies.save_link_7"))
        await monitor.deliver()

    @router.message(Dialog.remove, F.text, ~F.text.startswith("/"))
    async def remove_id(message: Message, state: FSMContext) -> None:
        """Handle the remove id interaction."""
        if message.from_user.id != db.admin_id():
            await state.clear()
            await message.answer(messages.render("replies.remove_id_1"))
            return
        try:
            sheet_id = int(message.text.strip())
            if not 0 < sheet_id < 2**63:
                raise ValueError
        except ValueError:
            await message.answer(messages.render("replies.remove_id_2"))
            return
        await ask_remove(message, state, sheet_id)

    async def ask_remove(message: Message, state: FSMContext, sheet_id: int) -> None:
        """Ask for confirmation before removing a sheet."""
        sheet = db.sheet(sheet_id, "approved")
        if sheet is None:
            await message.answer(messages.render("replies.ask_remove_1"))
            return
        token = secrets.token_hex(8)
        await state.update_data(sheet_id=sheet_id, token=token)
        await state.set_state(Dialog.remove_confirm)
        await message.answer(
            f"Удалить таблицу «{sheet['title']}»?",
            reply_markup=confirmation("remove", token, "Удалить"),
        )

    @router.callback_query(F.data.regexp(r"^pick_(remove|revoke):\d+$"))
    async def pick_action(callback: CallbackQuery, state: FSMContext) -> None:
        """Handle the pick action interaction."""
        if callback.from_user.id != db.admin_id():
            await callback.answer(messages.render("replies.pick_action_1"), show_alert=True)
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
    async def text_message(message: Message, state: FSMContext) -> None:
        """Handle the text message interaction."""
        current = await state.get_state()
        prompts = {
            Dialog.title.state: "Жду название предмета",
            Dialog.link.state: "Жду ссылку на таблицу",
            Dialog.remove.state: "Жду номер таблицы",
            Dialog.addtab_id.state: messages.render("replies.add_tab_id_waiting"),
            Dialog.requested_name.state: "Введи новое ФИО",
            Dialog.remove_confirm.state: "Жду подтверждение",
            Dialog.revoke.state: "Отправь ID пользователя из /members",
            Dialog.columns_id.state: "Отправь ID таблицы из /tables",
            Dialog.subject_id.state: "Отправь ID таблицы из /tables",
            Dialog.revoke_confirm.state: "Жду подтверждение",
        }
        await message.answer(prompts.get(current, help_text(message.from_user.id)), parse_mode="HTML")

    @router.message()
    async def unsupported(message: Message) -> None:
        """Handle the unsupported interaction."""
        await message.answer(messages.render("replies.unsupported_1"))

    return router


async def run_bot(db: Database, token: str, interval: int, contact_username: str = "",
                  proxy_url: str | None = None) -> None:
    """Run polling, cache refresh, and notification delivery."""
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

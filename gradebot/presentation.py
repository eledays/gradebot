"""HTML messages and persistent, owner-scoped pagination."""

from __future__ import annotations

import json
import secrets
from collections.abc import Sequence
from html import escape
from typing import TYPE_CHECKING

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message

if TYPE_CHECKING:
    from .db import Database


def bold(value: object) -> str:
    """Escape and wrap a value in a Telegram bold tag."""
    return f"<b>{escape(str(value))}</b>"


def table_link(title: str, url: str) -> str:
    """Build a safely escaped Telegram HTML link to a sheet."""
    title = " ".join(title.split())
    return f'<a href="{escape(url, quote=True)}">{escape(title)}</a>'


def safe_lines(value: object) -> list[str]:
    # Split before escaping so HTML entities and Telegram tags remain intact.
    """Escape and split untrusted text into short lines."""
    return [
        escape(line[offset : offset + 400])
        for line in str(value).split("\n")
        for offset in range(0, max(len(line), 1), 400)
    ]


def paginate(lines: Sequence[str], limit: int = 3500) -> list[str]:
    """Split HTML lines into pages within the Telegram message limit."""
    pages, current = [], []
    in_pre = False
    for line in lines:
        next_in_pre = False if line.endswith("</pre>") else True if line.startswith("<pre>") else in_pre
        suffix = "\n</pre>" if next_in_pre else ""
        if len(line) + len(suffix) > limit:
            raise ValueError("Message line exceeds page size")
        if current and len("\n".join([*current, line])) + len(suffix) > limit:
            if in_pre:
                current.append("</pre>")
            pages.append("\n".join(current).replace("<pre>\n", "<pre>").replace("\n</pre>", "</pre>"))
            current = ["<pre>"] if in_pre else []
        current.append(line)
        in_pre = next_in_pre
    if current:
        pages.append("\n".join(current).replace("<pre>\n", "<pre>").replace("\n</pre>", "</pre>"))
    return pages or ["Нет данных"]


def create_view(db: Database, owner: int, lines: Sequence[str], *, admin: bool = False,
                actions: list[list[tuple[str, str]]] | dict[str, list[tuple[str, str]]] | None = None,
                pages: list[str] | None = None) -> tuple[str, list[str]]:
    """Create an owner-scoped persistent pagination view."""
    pages = pages if pages is not None else paginate(lines)
    view_id = secrets.token_hex(8)
    if isinstance(actions, dict):
        actions = {
            str(index): [
                row
                for anchor, row in actions.items()
                if any(line.startswith(anchor + " ") for line in page.splitlines())
            ]
            for index, page in enumerate(pages)
        }
    db.store_view(view_id, owner, pages, admin, actions or [])
    return view_id, pages


def keyboard(view_id: str, page: int, total: int,
             actions: Sequence[Sequence[tuple[str, str]]] = ()) -> InlineKeyboardMarkup | None:
    """Build navigation and action buttons for a page."""
    rows = [[InlineKeyboardButton(text=label, callback_data=data) for label, data in row] for row in actions]
    if total > 1:
        navigation = []
        if page:
            navigation.append(InlineKeyboardButton(text="‹", callback_data=f"page:{view_id}:{page - 1}"))
        navigation.append(
            InlineKeyboardButton(text=f"{page + 1}/{total}", callback_data=f"page:{view_id}:{page}")
        )
        if page + 1 < total:
            navigation.append(InlineKeyboardButton(text="›", callback_data=f"page:{view_id}:{page + 1}"))
        rows.append(navigation)
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def view_markup(db: Database, view_id: str, page: int = 0) -> InlineKeyboardMarkup | None:
    """Load a view keyboard while hiding obsolete moderation actions."""
    row = db.view(view_id)
    if row is None:
        return None
    actions = json.loads(row["actions"])
    if isinstance(actions, dict):
        actions = actions.get(str(page), [])
    pending = db.pending_sheet_ids()
    actions = [
        [
            (label, data)
            for label, data in buttons
            if not data.startswith(("approve:", "reject:", "configure:")) or int(data.split(":")[1]) in pending
        ]
        for buttons in actions
    ]
    return keyboard(view_id, page, len(json.loads(row["pages"])), [buttons for buttons in actions if buttons])


async def send_view(message: Message, db: Database, lines: Sequence[str], *, admin: bool = False,
                    actions: list[list[tuple[str, str]]] | dict[str, list[tuple[str, str]]] | None = None) -> None:
    """Persist and send a paginated HTML message."""
    view_id, pages = create_view(db, message.from_user.id, lines, admin=admin, actions=actions)
    await message.answer(
        pages[0],
        parse_mode="HTML",
        reply_markup=view_markup(db, view_id),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )

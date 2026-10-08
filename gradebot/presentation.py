"""HTML messages and persistent, owner-scoped pagination."""

import json
import secrets
import time
from html import escape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions


def bold(value):
    return f"<b>{escape(str(value))}</b>"


def table_link(title, url):
    title = " ".join(title.split())
    return f'<a href="{escape(url, quote=True)}">{escape(title)}</a>'


def safe_lines(value):
    # Split before escaping so HTML entities and Telegram tags remain intact.
    return [
        escape(line[offset : offset + 400])
        for line in str(value).split("\n")
        for offset in range(0, max(len(line), 1), 400)
    ]


def paginate(lines, limit=3500):
    pages, current = [], []
    for line in lines:
        if len(line) > limit:
            raise ValueError("Message line exceeds page size")
        if current and len("\n".join([*current, line])) > limit:
            pages.append("\n".join(current))
            current = []
        current.append(line)
    if current:
        pages.append("\n".join(current))
    return pages or ["Нет данных"]


def create_view(db, owner, lines, *, admin=False, actions=None):
    pages = paginate(lines)
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
    db.conn.execute(
        "INSERT INTO views(id,owner,pages,admin,actions,created_at) VALUES(?,?,?,?,?,?)",
        (view_id, owner, json.dumps(pages), int(admin), json.dumps(actions or []), time.time()),
    )
    return view_id, pages


def keyboard(view_id, page, total, actions=()):
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


def view_markup(db, view_id, page=0):
    row = db.all("SELECT * FROM views WHERE id=?", (view_id,))[0]
    actions = json.loads(row["actions"])
    if isinstance(actions, dict):
        actions = actions.get(str(page), [])
    pending = {r["id"] for r in db.all("SELECT id FROM sheets WHERE status='pending'")}
    actions = [
        [
            (label, data)
            for label, data in buttons
            if not data.startswith(("approve:", "reject:")) or int(data.split(":")[1]) in pending
        ]
        for buttons in actions
    ]
    return keyboard(view_id, page, len(json.loads(row["pages"])), [buttons for buttons in actions if buttons])


async def send_view(message, db, lines, *, admin=False, actions=None):
    with db.conn:
        view_id, pages = create_view(db, message.from_user.id, lines, admin=admin, actions=actions)
    await message.answer(
        pages[0],
        parse_mode="HTML",
        reply_markup=view_markup(db, view_id),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )

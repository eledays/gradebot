"""Paginated column selection shared by setup and saved tables."""

from html import escape
from typing import NotRequired, TypedDict

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .messages import messages
from .selection import selection_button
from .sheets import column_letter


class ColumnPickerData(TypedDict):
    """Saved FSM state for one grade-column selection view."""

    column_title: str
    column_labels: dict[int, str]
    column_selected: list[int]
    column_token: str
    column_page: NotRequired[int]
    edit_sheet_id: NotRequired[int]


PAGE_SIZE = 8


def _escaped_caption(value: str) -> str:
    """Escape a column caption for Telegram HTML."""
    result = ""
    for char in value:
        encoded = escape(char)
        if len(result) + len(encoded) > 320:
            return result + "…"
        result += encoded
    return result


def column_picker(data: ColumnPickerData, *, check_emoji_id: str | None = None
                  ) -> tuple[str, InlineKeyboardMarkup]:
    """Build a paginated grade-column selection keyboard."""
    labels = [(int(i), label) for i, label in data["column_labels"].items()]
    selected = set(data["column_selected"])
    total = max(1, (len(labels) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(data.get("column_page", 0), total - 1)
    prefix = f"colpick:{data['column_token']}:"
    lines = [f"<b>{_escaped_caption(data['column_title'][:200])}</b>",
             messages.render("replies.columns_picker_hint"),
             messages.render("replies.columns_picker_legend"),
             messages.render("replies.columns_picker_count", selected=len(selected), total=len(labels)), ""]
    buttons = []
    for index, label in labels[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        caption = f"{'✅ ' if index in selected else ''}{column_letter(index)}: {label}"
        lines.append(_escaped_caption(caption[:220]))
        buttons.append([selection_button(f"{column_letter(index)}: {label}"[:60], prefix + str(index),
                                        index in selected, check_emoji_id=check_emoji_id)])
    if total > 1:
        navigation = []
        if page:
            navigation.append(InlineKeyboardButton(text="‹", callback_data=prefix + f"page{page - 1}"))
        navigation.append(InlineKeyboardButton(text=f"{page + 1}/{total}", callback_data=prefix + f"page{page}"))
        if page + 1 < total:
            navigation.append(InlineKeyboardButton(text="›", callback_data=prefix + f"page{page + 1}"))
        buttons.append(navigation)
    buttons.extend([
        [InlineKeyboardButton(text="Выбрать все", callback_data=prefix + "all")],
        [InlineKeyboardButton(text="Сохранить" if data.get("edit_sheet_id") else "Готово",
                              callback_data=prefix + "done"),
         InlineKeyboardButton(text="Отмена", callback_data=prefix + "cancel")],
    ])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)

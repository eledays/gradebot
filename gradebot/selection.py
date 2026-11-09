"""Inline selection buttons with an optional Telegram custom check icon."""

from aiogram.types import InlineKeyboardButton


def selection_button(label: str, callback_data: str, selected: bool, *,
                     check_emoji_id: str | None = None) -> InlineKeyboardButton:
    """Mark only selected entries, falling back to the standard check emoji."""
    custom_id = check_emoji_id if selected else None
    text = label if not selected or custom_id else f"✅ {label}"
    return InlineKeyboardButton(text=text, callback_data=callback_data,
                                icon_custom_emoji_id=custom_id)

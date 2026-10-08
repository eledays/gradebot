"""Administrator-managed identity list; usernames resolve before granting access."""

import asyncio
import os
import re

from .sheets import valid_name


class AccessError(ValueError):
    pass


def parse_members(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or len(lines) > 50:
        raise AccessError("Отправь от 1 до 50 строк: @username | ФИО")
    entries, seen = [], set()
    for number, line in enumerate(lines, 1):
        username, separator, name = line.partition("|")
        username = username.strip().removeprefix("@").lower()
        name = " ".join(name.split())
        if not separator or not re.fullmatch(r"[a-z][a-z0-9_]{3,31}", username) or not valid_name(name):
            raise AccessError(f"Строка {number}: нужен формат @username | ФИО (2–3 полных слова)")
        if username in seen:
            raise AccessError(f"Строка {number}: @{username} указан дважды")
        seen.add(username)
        entries.append((username, name))
    return entries


class UsernameResolver:
    def __init__(self, token):
        self.token = token
        self.client = None
        self.lock = asyncio.Lock()

    async def resolve(self, username):
        api_id, api_hash = os.getenv("TELEGRAM_API_ID"), os.getenv("TELEGRAM_API_HASH")
        if not api_id or not api_hash:
            raise AccessError(
                "Поиск по @username ещё не настроен. Выбери человека кнопкой «Выбрать пользователя» "
                "или настрой TELEGRAM_API_ID и TELEGRAM_API_HASH по инструкции в README."
            )
        try:
            from telethon import TelegramClient, errors, functions, types
            from telethon.sessions import MemorySession
        except ImportError as exc:
            raise AccessError("Для поиска по @username установи: pip install -e '.[usernames]'") from exc
        async with self.lock:
            try:
                if self.client is None:
                    self.client = TelegramClient(
                        MemorySession(),
                        int(api_id),
                        api_hash,
                        flood_sleep_threshold=0,
                        receive_updates=False,
                        request_retries=0,
                        connection_retries=1,
                    )
                async with asyncio.timeout(30):
                    if not self.client.is_connected():
                        await self.client.connect()
                    if not await self.client.is_user_authorized():
                        await self.client.sign_in(bot_token=self.token)
                    # Always ask Telegram. A cached username could belong to a different user now.
                    result = await self.client(functions.contacts.ResolveUsernameRequest(username))
                if not isinstance(result.peer, types.PeerUser):
                    raise AccessError(f"@{username} — группа или канал. Нужен аккаунт человека.")
                user = next((user for user in result.users if user.id == result.peer.user_id), None)
                if user is None or user.bot or user.deleted:
                    raise AccessError(f"@{username} — не действующий аккаунт человека.")
                return user.id
            except (errors.UsernameInvalidError, errors.UsernameNotOccupiedError) as exc:
                raise AccessError(f"@{username} не найден. Проверь написание.") from exc
            except errors.FloodWaitError as exc:
                raise AccessError(
                    f"Telegram просит подождать {exc.seconds} сек. Список не сохранён."
                ) from exc
            except (errors.RPCError, OSError, TimeoutError, ValueError) as exc:
                raise AccessError(
                    "Не удалось проверить юзернейм в Telegram. Список не сохранён; попробуй позже."
                ) from exc

    async def close(self):
        if self.client is not None:
            await self.client.disconnect()

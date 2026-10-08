"""Shared SOCKS5 configuration for Telegram and Google Sheets transports."""

from contextlib import asynccontextmanager
from urllib.parse import unquote, urlsplit

import aiohttp
from aiogram.client.session.aiohttp import AiohttpSession
from aiohttp_socks import ProxyConnector, ProxyType


class ProxyConfigurationError(ValueError):
    pass


def proxy_options(url):
    if not url or not url.strip():
        return None
    try:
        parts = urlsplit(url.strip())
        port = parts.port
        if (
            parts.scheme not in ("socks5", "socks5h")
            or not parts.hostname
            or port is None
            or not 1 <= port <= 65535
            or parts.path not in ("", "/")
            or parts.query
            or parts.fragment
        ):
            raise ValueError
        username = unquote(parts.username) if parts.username is not None else None
        password = unquote(parts.password) if parts.password is not None else None
        if username is not None:
            # Aiogram accepts an explicit BasicAuth pair, preserving URL-escaped credentials.
            aiohttp.BasicAuth(username, password or "")
        return {
            "proxy_type": ProxyType.SOCKS5,
            "host": parts.hostname,
            "port": port,
            "username": username,
            "password": password,
            "rdns": True,
        }
    except ValueError:
        raise ProxyConfigurationError("PROXY_URL: укажите socks5://[login:password@]host:port") from None


@asynccontextmanager
async def network_sessions(proxy_url=None):
    options = proxy_options(proxy_url)
    telegram_proxy, connector = None, None
    if options:
        host = options["host"]
        if ":" in host:
            host = f"[{host}]"
        telegram_proxy = f"socks5://{host}:{options['port']}"
        if options["username"] is not None:
            telegram_proxy = (
                telegram_proxy,
                aiohttp.BasicAuth(options["username"], options["password"] or ""),
            )
    async with AiohttpSession(proxy=telegram_proxy) as telegram:
        if options:
            connector = ProxyConnector(**options)
        async with aiohttp.ClientSession(connector=connector) as sheets:
            yield telegram, sheets

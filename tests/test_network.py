import asyncio

import pytest

from gradebot.network import ProxyConfigurationError, network_sessions, proxy_options


@pytest.mark.parametrize(
    "url",
    [
        "http://host:1080",
        "socks5://host",
        "socks5://host:0",
        "socks5://host:65536",
        "socks5://host:bad",
        "socks5://host:1080/path",
    ],
)
def test_invalid_proxy_has_safe_error(url):
    with pytest.raises(ProxyConfigurationError, match="PROXY_URL"):
        proxy_options(url)
    with pytest.raises(ProxyConfigurationError) as error:
        proxy_options("socks5://user:secret@host:invalid")
    assert "secret" not in str(error.value)


async def test_direct_sessions_work_and_close_without_proxy():
    async with network_sessions() as (telegram, sheets):
        assert telegram.proxy is None
        assert not sheets.closed
    assert sheets.closed


@pytest.mark.parametrize("credentials", ["", "user:p%40ss@"])
async def test_both_transports_use_socks5_and_remote_dns(credentials):
    targets, authentication, errors = [], [], []

    async def serve(reader, writer):
        try:
            version, count = await reader.readexactly(2)
            assert version == 5
            methods = await reader.readexactly(count)
            method = 2 if credentials else 0
            assert method in methods
            writer.write(bytes([5, method]))
            await writer.drain()
            if method == 2:
                version, length = await reader.readexactly(2)
                assert version == 1
                user = await reader.readexactly(length)
                length = (await reader.readexactly(1))[0]
                password = await reader.readexactly(length)
                authentication.append((user.decode(), password.decode()))
                writer.write(b"\x01\x00")
                await writer.drain()
            version, command, reserved, address_type = await reader.readexactly(4)
            assert (version, command, reserved, address_type) == (5, 1, 0, 3)
            length = (await reader.readexactly(1))[0]
            host = (await reader.readexactly(length)).decode()
            port = int.from_bytes(await reader.readexactly(2), "big")
            targets.append((host, port))
            writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x50")
            await writer.drain()
            request = await reader.readuntil(b"\r\n\r\n")
            assert request.startswith(b"GET /resource ")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        except Exception as error:
            errors.append(error)
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        async with network_sessions(f"socks5h://{credentials}127.0.0.1:{port}") as (telegram, sheets):
            telegram_http = await telegram.create_session()
            for session in [telegram_http, sheets]:
                async with session.get("http://example.invalid/resource", timeout=3) as response:
                    assert await response.text() == "ok"
    assert not errors
    assert targets == [("example.invalid", 80), ("example.invalid", 80)]
    assert authentication == ([("user", "p@ss")] * 2 if credentials else [])
    assert sheets.closed and telegram_http.closed

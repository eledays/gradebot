import json
from unittest.mock import AsyncMock

import pytest

from gradebot.db import Database
from gradebot.monitor import Monitor
from gradebot.sheets import Sheet


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "test.sqlite3"))
    database.register(123, "Иванов Иван Иванович")
    database.add_sheet("Матан", "https://docs.google.com/spreadsheets/d/id/edit", 1, (1,), 123, "approved")
    with database.conn:
        database.cache(1, parsed())
    yield database
    database.close()


def parsed(first="5", second=None):
    headers = ["ФИО", "С1"] + (["С2"] if second is not None else [])
    row = ["Иван Иванович Иванов", first] + ([second] if second is not None else [])
    return Sheet([headers, row])


async def test_registration_baseline_new_column_and_edit(db):
    bot = AsyncMock()
    monitor = Monitor(db, None, bot)
    monitor.fetch = AsyncMock(return_value=parsed())
    reports = await monitor.check_user(123)
    assert "С1: 5" in reports[0]
    await monitor.scan()
    bot.send_message.assert_not_called()
    monitor.fetch.return_value = parsed("4", "5")
    await monitor.scan()
    assert bot.send_message.await_count == 1
    texts = [call.args[1] for call in bot.send_message.await_args_list]
    assert "<b>Матан</b>\n- С1: 4\n- С2: 5" in texts[0]
    await monitor.scan()
    assert bot.send_message.await_count == 1


async def test_delivery_retried_after_database_reopen(db):
    monitor = Monitor(db, None, AsyncMock())
    monitor.fetch = AsyncMock(return_value=parsed())
    monitor.bot.send_message.side_effect = RuntimeError("Network down")
    await monitor.scan()
    assert db.all("SELECT * FROM outbox WHERE status='pending'")
    path = db.all("PRAGMA database_list")[0]["file"]
    reopened = Database(path)
    try:
        restarted = Monitor(reopened, None, AsyncMock())
        await restarted.deliver()
        assert restarted.bot.send_message.await_count == 0  # Backoff survives a restart.
        reopened.execute("UPDATE outbox SET next_attempt=0")
        await restarted.deliver()
        assert restarted.bot.send_message.await_count == 1
        assert not reopened.all("SELECT * FROM outbox WHERE status='pending'")
    finally:
        reopened.close()


async def test_failed_fetch_preserves_baseline(db):
    monitor = Monitor(db, None, AsyncMock())
    monitor.fetch = AsyncMock(return_value=parsed())
    await monitor.check_user(123)
    monitor.fetch.side_effect = RuntimeError("Google down")
    await monitor.scan()
    monitor.fetch.side_effect = None
    monitor.fetch.return_value = parsed("4")
    await monitor.scan()
    assert monitor.bot.send_message.await_count == 1


async def test_missing_and_ambiguous_rows_do_not_erase_snapshot(db):
    monitor = Monitor(db, None, AsyncMock())
    monitor.fetch = AsyncMock(return_value=parsed())
    await monitor.check_user(123)
    monitor.fetch.return_value = Sheet([["ФИО", "С1"], ["Петров Петр", "2"]])
    await monitor.scan()
    assert json.loads(db.all("SELECT grades FROM snapshots")[0]["grades"]) == {"С1": "5"}
    monitor.bot.send_message.assert_not_called()


def test_name_change_resets_only_that_users_pending_notifications(db):
    db.snapshot(123, 1, {"С1": "5"}, ["old name notification"])
    db.register(123, "Петров Петр")
    assert not db.all("SELECT * FROM snapshots")
    assert not db.all("SELECT * FROM outbox")


async def test_blank_and_removed_columns(db):
    monitor = Monitor(db, None, AsyncMock())
    await monitor.check_user(123)
    monitor.fetch = AsyncMock(return_value=parsed(""))
    await monitor.scan()
    assert "оценка удалена" in monitor.bot.send_message.await_args.args[1]
    count = monitor.bot.send_message.await_count
    monitor.fetch.return_value = Sheet([["ФИО", "С2"], ["Иванов Иван Иванович", ""]])
    await monitor.scan()
    assert monitor.bot.send_message.await_count == count


async def test_registration_checks_all_approved_sheets(db):
    db.add_sheet("Физика", "https://docs.google.com/spreadsheets/d/physics/edit", 1, (1,), 123, "approved")
    db.add_sheet(
        "Предложение", "https://docs.google.com/spreadsheets/d/pending/edit", 1, (1,), 123, "pending"
    )
    monitor = Monitor(db, None, AsyncMock())
    monitor.fetch = AsyncMock(return_value=parsed())
    with db.conn:
        db.cache(2, parsed())
    reports = await monitor.check_user(123)
    assert len(reports) == 2
    monitor.fetch.assert_not_called()
    assert len(db.all("SELECT * FROM snapshots")) == 2
    assert not db.all("SELECT * FROM outbox")


async def test_manual_refresh_resets_scheduler_deadline(db, monkeypatch):
    import asyncio
    import contextlib
    from types import SimpleNamespace

    import gradebot.monitor as monitor_module

    clock = [100.0]
    monitor = Monitor(db, None, AsyncMock())
    monitor._scan = AsyncMock(return_value={"updated": 1, "failed": [], "notifications": 0})
    delays = asyncio.Queue()

    async def wait_for_reset(awaitable, timeout):
        delays.put_nowait(timeout)
        await awaitable

    monkeypatch.setattr(monitor_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(monitor_module, "asyncio", SimpleNamespace(wait_for=wait_for_reset))
    task = asyncio.create_task(monitor.run(60))
    try:
        assert await asyncio.wait_for(delays.get(), 1) == 60
        clock[0] = 110
        await monitor.scan()
        assert monitor.next_refresh == 170
        assert await asyncio.wait_for(delays.get(), 1) == 60
        clock[0] = 150
        monitor.schedule_changed.set()
        assert await asyncio.wait_for(delays.get(), 1) == 20
        assert monitor._scan.await_count == 2
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_scheduled_refresh_waiting_for_manual_does_not_scan_again(db):
    import asyncio

    monitor = Monitor(db, None, AsyncMock())
    started, finish = asyncio.Event(), asyncio.Event()

    async def scan():
        started.set()
        await finish.wait()
        return {"updated": 1, "failed": [], "notifications": 0}

    monitor._scan = AsyncMock(side_effect=scan)
    manual = asyncio.create_task(monitor.scan())
    await started.wait()
    background = asyncio.create_task(monitor.scan(scheduled=True))
    finish.set()
    await manual
    assert await background is None
    assert monitor._scan.await_count == 1


async def test_manual_scan_refreshes_cache_and_notifies_without_duplicates(db):
    monitor = Monitor(db, None, AsyncMock())
    await monitor.check_user(123)
    monitor.fetch = AsyncMock(return_value=parsed("4"))
    result = await monitor.scan()
    assert result == {"updated": 1, "failed": [], "notifications": 1}
    assert (await monitor.inspect_user(123))[0]["grades"] == {"С1": "4"}
    assert monitor.bot.send_message.await_count == 1
    result = await monitor.scan()
    assert result["notifications"] == 0
    assert monitor.bot.send_message.await_count == 1


async def test_failed_first_hundred_messages_do_not_starve_next_user(db):
    db.register(456, "Петров Петр")
    with db.conn:
        for _ in range(100):
            db.queue(123, ["temporary error"])
        db.queue(456, ["healthy recipient"])
    bot = AsyncMock()

    async def send(user_id, text, **kwargs):
        if user_id == 123:
            raise RuntimeError("Network failure")

    bot.send_message.side_effect = send
    monitor = Monitor(db, None, bot)
    await monitor.deliver()
    assert bot.send_message.await_count == 100
    await monitor.deliver()
    assert bot.send_message.await_count == 101
    assert db.all("SELECT status FROM outbox WHERE user_id=456")[0][0] == "sent"


async def test_retry_after_respects_full_deadline_after_restart(db, monkeypatch):
    from aiogram.exceptions import TelegramRetryAfter
    from aiogram.methods import SendMessage

    import gradebot.monitor as module

    clock = [1000.0]
    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    with db.conn:
        db.queue(123, ["grades"])
    bot = AsyncMock()
    bot.send_message.side_effect = TelegramRetryAfter(
        method=SendMessage(chat_id=123, text="grades"),
        message="Flood",
        retry_after=120,
    )
    await Monitor(db, None, bot).deliver()
    assert db.all("SELECT next_attempt FROM outbox")[0][0] == 1120
    restarted = Monitor(db, None, AsyncMock())
    with db.conn:
        db.queue(123, ["new notification during rate limit"])
    clock[0] = 1119
    await restarted.deliver()
    restarted.bot.send_message.assert_not_called()
    clock[0] = 1120
    await restarted.deliver()
    assert restarted.bot.send_message.await_count == 2


async def test_permanent_bad_message_does_not_retry_forever(db):
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import SendMessage

    with db.conn:
        db.queue(123, ["bad"])
        db.queue(123, ["good"])
    bot = AsyncMock()
    bot.send_message.side_effect = [
        TelegramBadRequest(method=SendMessage(chat_id=123, text="bad"), message="invalid HTML"),
        None,
    ]
    monitor = Monitor(db, None, bot)
    await monitor.deliver()
    assert [row[0] for row in db.all("SELECT status FROM outbox ORDER BY id")] == ["failed", "sent"]
    await monitor.deliver()
    assert bot.send_message.await_count == 2


async def test_name_update_preserves_deliverable_table_and_moderation_views(db):
    with db.conn:
        db.queue(123, ["grades"], kind="grades")
        db.queue(123, ["table"], kind="table")
        db.queue(123, ["moderation"], kind="moderation")
    db.grant_members([(123, "newusername", "Петров Петр")])
    bot = AsyncMock()
    await Monitor(db, None, bot).deliver()
    assert [call.args[1] for call in bot.send_message.await_args_list] == ["table", "moderation"]


async def test_legacy_unauthorized_queue_is_not_delivered(db):
    with db.conn:
        db.queue(123, ["grades"])
    db.execute("DELETE FROM members WHERE id=123")
    bot = AsyncMock()
    await Monitor(db, None, bot).deliver()
    bot.send_message.assert_not_called()
    assert not db.all("SELECT * FROM outbox")

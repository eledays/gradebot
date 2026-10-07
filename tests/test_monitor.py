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

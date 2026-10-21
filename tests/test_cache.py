import asyncio
import json
import sqlite3
import time
from unittest.mock import AsyncMock

import pytest

from gradebot.db import Database
from gradebot.monitor import Monitor, format_grades
from gradebot.presentation import paginate, safe_lines, view_markup
from gradebot.sheets import Sheet


def parsed(value="5"):
    return Sheet([["ФИО", "С1"], ["Иванов Иван", value]])


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "cache.sqlite3"))
    database.register(123, "Иванов Иван")
    database.add_sheet(
        "ОРГ",
        "https://docs.google.com/spreadsheets/d/org/edit#gid=42",
        1,
        (1,),
        999,
        "approved",
        parsed=parsed(),
    )
    yield database
    database.close()


async def test_cached_grades_are_immediate_persisted_and_stale(db):
    with db.conn:
        db.cache(1, parsed("3"), fetched_at=1_700_000_000)
    path = db.all("PRAGMA database_list")[0]["file"]
    reopened = Database(path)
    try:
        monitor = Monitor(reopened, None, AsyncMock())
        monitor.fetch = AsyncMock(side_effect=RuntimeError("No network"))
        reports = await asyncio.wait_for(monitor.inspect_user(123), timeout=0.1)
        assert reports[0]["grades"] == {"С1": "3"}
        assert reports[0]["stale"]
        text = format_grades(reports[0])
        assert "Кэш устарел" in text and "15.11.2023" in text and "МСК" in text
        monitor.fetch.assert_not_called()
    finally:
        reopened.close()


async def test_failed_refresh_preserves_cache_and_its_timestamp(db):
    original = db.all("SELECT * FROM sheet_cache")[0]
    monitor = Monitor(db, None, AsyncMock())
    monitor.fetch = AsyncMock(side_effect=RuntimeError("Google down"))
    await monitor.scan()
    current = db.all("SELECT * FROM sheet_cache")[0]
    assert tuple(original) == tuple(current)
    assert (await monitor.inspect_user(123))[0]["grades"] == {"С1": "5"}


async def test_one_notification_per_user_across_subjects(db):
    db.add_sheet(
        "История",
        "https://docs.google.com/spreadsheets/d/history/edit",
        1,
        (1,),
        999,
        "approved",
        parsed=parsed(""),
    )
    db.snapshot(123, 1, {"С1": ""}, [])
    db.snapshot(123, 2, {"С1": ""}, [])
    monitor = Monitor(db, None, AsyncMock())
    monitor.fetch = AsyncMock(
        side_effect=[Sheet([["ФИО", "С1", "С2"], ["Иванов Иван", "2", "3"]]), parsed("1")]
    )
    await monitor.scan()
    assert monitor.bot.send_message.await_count == 1
    text = monitor.bot.send_message.await_args.args[1]
    assert text == "<b>Найдены новые оценки:</b>\n\n<b>ОРГ</b>\n- С1: 2\n- С2: 3\n\n<b>История</b>\n- С1: 1\n"
    assert len(db.all("SELECT * FROM outbox")) == 1
    monitor.fetch.side_effect = [Sheet([["ФИО", "С1", "С2"], ["Иванов Иван", "2", "3"]]), parsed("1")]
    await monitor.scan()
    assert monitor.bot.send_message.await_count == 1


async def test_large_notification_is_sent_once_with_pagination(db):
    headers = ["ФИО"] + [f"С{i}" for i in range(2000)]
    sheet = Sheet([headers, ["Иванов Иван"] + ["5"] * 2000])
    monitor = Monitor(db, None, AsyncMock())
    monitor.fetch = AsyncMock(return_value=sheet)
    await monitor.scan()
    assert monitor.bot.send_message.await_count == 1
    item = db.all("SELECT * FROM outbox")[0]
    view = db.all("SELECT * FROM views WHERE id=?", (item["view_id"],))[0]
    pages = json.loads(view["pages"])
    assert len(pages) > 1
    assert all(len(page) <= 3500 for page in pages)
    assert monitor.bot.send_message.await_args.kwargs["reply_markup"]


def test_html_escaping_does_not_break_pages():
    lines = safe_lines("<script>&" * 1000)
    pages = paginate(lines)
    assert all(len(page) <= 3500 for page in pages)
    assert "<script>" not in "".join(pages)
    assert "&lt;" in pages[0]


def test_legacy_database_migrates_export_urls_and_queue(tmp_path):
    path = str(tmp_path / "legacy.sqlite3")
    connection = sqlite3.connect(path)
    connection.executescript("""
    CREATE TABLE sheets(id INTEGER PRIMARY KEY,title TEXT,url TEXT UNIQUE,header_row INTEGER,
                        name_columns TEXT,proposer INTEGER,status TEXT);
    CREATE TABLE outbox(id INTEGER PRIMARY KEY,user_id INTEGER,text TEXT,status TEXT DEFAULT 'pending');
    INSERT INTO sheets VALUES(1,'ОРГ','https://docs.google.com/spreadsheets/d/id/export?format=csv&gid=42',
                              1,'[1]',123,'approved');
    INSERT INTO outbox(user_id,text) VALUES(123,'old notification');
    """)
    connection.close()
    db = Database(path)
    try:
        assert db.all("SELECT url FROM sheets")[0]["url"].endswith("/edit#gid=42")
        assert "export" not in db.all("SELECT url FROM sheets")[0]["url"]
        assert db.all("SELECT text FROM outbox")[0]["text"] == "old notification"
        db.register(123, "Иванов Иван")
        with db.conn:
            db.cache(1, parsed(), time.time())
            db.queue(123, safe_lines("new notification"))
        assert len(db.all("SELECT * FROM outbox")) == 2
    finally:
        db.close()


def test_name_change_keeps_moderation_and_table_notifications(db):
    with db.conn:
        db.queue(123, ["grades"], kind="grades")
        db.queue(123, ["moderation"], kind="moderation")
        db.queue(123, ["table"], kind="table")
    db.register(123, "Петров Петр")
    assert [r["kind"] for r in db.all("SELECT kind FROM outbox")] == ["moderation", "table"]


def test_new_table_broadcast_is_persistent_and_uses_web_link(db):
    db.register(456, "Петров Петр")
    db.add_sheet(
        "История",
        "https://docs.google.com/spreadsheets/d/history/export?format=csv&gid=12",
        1,
        (1,),
        999,
        "approved",
        parsed=parsed(),
        notify=True,
    )
    rows = db.all("SELECT * FROM outbox WHERE kind='table'")
    assert {row["user_id"] for row in rows} == {123, 456}
    assert all("/edit#gid=12" in row["text"] and "export" not in row["text"] for row in rows)
    assert all(view_markup(db, row["view_id"]) is None for row in rows)


def test_grade_summary_shows_last_three_but_statistics_use_every_column():
    report = {
        "title": "ОРГ",
        "url": "https://docs.google.com/spreadsheets/d/id/edit",
        "status": "found",
        "fetched_at": time.time(),
        "stale": False,
        "grades": dict(
            zip([f"С{i}" for i in range(1, 8)], ["10", "10", "2", "3", "4", "5", "6"], strict=True)
        ),
    }
    text = format_grades(report)
    assert "С1: 10" not in text and "С2: 10" not in text
    assert all(f"- С{i}:" in text for i in range(5, 8))
    assert "- С4:" not in text
    assert text.index("- С7:") < text.index("- С6:") < text.index("- С5:")
    assert text.endswith("</i>")
    assert "Среднее: 5,71" in text and "Количество: 7" in text


def test_grade_summary_handles_empty_text_zero_and_decimal_grades():
    report = {
        "title": "ОРГ",
        "url": "https://docs.google.com/spreadsheets/d/id/edit",
        "status": "found",
        "fetched_at": time.time(),
        "stale": False,
        "grades": {"С1": "0", "С2": "", "С3": "н", "С4": "4,5", "С5": "3.0", "С6": " "},
    }
    text = format_grades(report)
    assert "Среднее: 2,5" in text and "Количество: 4" in text
    report["grades"] = {"С1": "", "С2": " "}
    text = format_grades(report)
    assert "Оценок пока нет" in text and "Среднее: —" in text and "Количество: 0" in text


def test_duplicate_title_is_rejected_at_database_boundary(db):
    from gradebot.db import DuplicateTitleError

    with pytest.raises(DuplicateTitleError):
        db.add_sheet("  орг ", "https://docs.google.com/spreadsheets/d/other/edit", 1, (1,), 999, "pending")
    assert len(db.all("SELECT * FROM sheets")) == 1

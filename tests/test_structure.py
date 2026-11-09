import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from gradebot.db import Database
from gradebot.monitor import Monitor
from gradebot.sheets import Sheet
from gradebot.structure import header_signature

BASE = "https://docs.google.com/spreadsheets/d/book"


def tabs(*names):
    return [{"name": name, "url": BASE + f"/edit#gid={index}"} for index, name in enumerate(names)]


@pytest.fixture
def setup(tmp_path):
    db = Database(str(tmp_path / "watch.sqlite3"))
    db.execute("INSERT INTO settings VALUES('admin','999')")
    db.register(123, "Иванов Иван")
    parsed = Sheet([["ФИО", "Квиз"], ["Иванов Иван", "5"]])
    db.add_sheet("Матан", tabs("Лист 1")[0]["url"], 1, (1,), 999, "approved", parsed=parsed)
    db.snapshot(123, 1, {"Квиз": "5"}, [])
    monitor = Monitor(db, None, AsyncMock())
    monitor.fetch = AsyncMock(return_value=parsed)
    monitor.discover = AsyncMock(return_value=tabs("Лист 1"))
    yield db, monitor
    db.close()


async def test_new_tabs_are_reported_once_and_discovery_is_per_workbook(setup):
    db, monitor = setup
    db.add_sheet("Другой лист", BASE + "/edit#gid=2", 1, (1,), 999, "approved")
    await monitor.scan()  # Existing workbooks get a silent initial baseline.
    assert monitor.discover.await_count == 1
    assert not db.all("SELECT * FROM outbox WHERE kind='structure'")
    monitor.discover.return_value = tabs("Лист 1", "Новый лист <2>")
    result = await monitor.scan()
    assert result["structure_notifications"] == 1
    alerts = db.all("SELECT * FROM outbox WHERE kind='structure'")
    assert len(alerts) == 1 and alerts[0]["user_id"] == 999
    assert "Новый лист &lt;2&gt;" in alerts[0]["text"]
    await monitor.scan()
    assert len(db.all("SELECT * FROM outbox WHERE kind='structure'")) == 1


async def test_watch_baseline_survives_database_reopen(setup):
    db, monitor = setup
    await monitor.scan()
    reopened = Database(db.all("PRAGMA database_list")[0]["file"])
    try:
        restarted = Monitor(reopened, None, AsyncMock())
        restarted.discover = AsyncMock(return_value=tabs("Лист 1", "Новый"))
        restarted.fetch = monitor.fetch
        first = await restarted.scan()
        second = await restarted.scan()
        assert first["structure_notifications"] == 1
        assert second["structure_notifications"] == 0
    finally:
        reopened.close()


async def test_header_edit_compared_with_existing_cache_on_first_scan(setup):
    db, monitor = setup
    monitor.fetch.return_value = Sheet([["ФИО", "Экзамен"], ["Иванов Иван", "5"]])
    result = await monitor.scan()
    assert result["structure_notifications"] == 1
    text = db.all("SELECT text FROM outbox WHERE kind='structure'")[0]["text"]
    assert "Изменилась структура листа" in text and "Изменились заголовки" in text
    assert (await monitor.scan())["structure_notifications"] == 0


async def test_grade_and_student_rows_do_not_trigger_structure_alert(setup):
    db, monitor = setup
    await monitor.scan()
    monitor.fetch.return_value = Sheet([["ФИО", "Квиз"], ["Иванов Иван", "4"], ["Петров Петр", "3"]])
    result = await monitor.scan()
    assert result["notifications"] == 1
    assert result["structure_notifications"] == 0
    assert not db.all("SELECT * FROM outbox WHERE kind='structure'")


async def test_discovery_failure_does_not_erase_watch_baseline(setup):
    db, monitor = setup
    await monitor.scan()
    monitor.discover.side_effect = TimeoutError()
    result = await monitor.scan()
    assert result["updated"] == 1 and result["structure_notifications"] == 0
    assert len(json.loads(db.all("SELECT tabs FROM workbook_watch")[0]["tabs"])) == 1
    monitor.discover.side_effect = None
    monitor.discover.return_value = tabs("Лист 1", "Новый")
    assert (await monitor.scan())["structure_notifications"] == 1


async def test_renamed_and_removed_tabs_notify_admin(setup):
    db, monitor = setup
    monitor.discover.return_value = tabs("Первый", "Второй")
    await monitor.scan()
    monitor.discover.return_value = tabs("Переименован")
    await monitor.scan()
    text = db.all("SELECT text FROM outbox WHERE kind='structure'")[0]["text"]
    assert "Первый → Переименован" in text and "удалён или скрыт: Второй" in text


async def test_invalid_headers_notify_even_when_csv_parser_fails_and_keep_cache(setup, monkeypatch):
    db, monitor = setup
    del monitor.fetch  # Exercise the real fetch and parser rather than a mocked Sheet.

    class Response:
        status = 200
        headers = {"Content-Type": "text/csv"}

        def __init__(self):
            self.content = self

        async def iter_chunked(self, size):
            yield "ФИО,Квиз,Квиз\nИванов Иван,4,5\n".encode()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    monitor.session = MagicMock()
    monitor.session.get.return_value = Response()
    result = await monitor.scan()
    assert result["failed"] == ["Матан"] and result["structure_notifications"] == 1
    text = db.all("SELECT text FROM outbox WHERE kind='structure'")[0]["text"]
    assert "Сохранён предыдущий кэш" in text
    assert json.loads(db.all("SELECT rows FROM sheet_cache")[0]["rows"])[1][1] == "5"
    assert (await monitor.scan())["structure_notifications"] == 0


async def test_structure_notifications_do_not_follow_grade_mute_and_old_admin_is_skipped(setup):
    db, monitor = setup
    db.register(999, "Админ Админов")
    db.toggle_notifications(999)
    monitor.fetch.return_value = Sheet([["ФИО", "Новый заголовок"], ["Иванов Иван", "5"]])
    await monitor.scan()
    assert db.all("SELECT status FROM outbox WHERE kind='structure'")[0]["status"] == "sent"
    with db.conn:
        db.queue(999, ["Old admin alert"], kind="structure", admin=True)
    db.execute("UPDATE settings SET value='888' WHERE key='admin'")
    monitor.bot.send_message.reset_mock()
    await monitor.deliver()
    monitor.bot.send_message.assert_not_called()
    assert db.all("SELECT status FROM outbox WHERE text='Old admin alert'")[0]["status"] == "cancelled"


def test_header_signature_is_json_stable_and_detects_reordering():
    rows = [["№", "ФИО", "Квиз", "Экзамен"], ["1", "Иванов Иван", "4", "5"]]
    signature = header_signature(rows, 1)
    assert signature == json.loads(json.dumps(signature))
    reordered = [["№", "ФИО", "Экзамен", "Квиз"], rows[1]]
    assert signature != header_signature(reordered, 1)


def test_saving_workbook_keeps_first_discovery_baseline(setup):
    db, monitor = setup
    with db.conn:
        db.remember_workbook(BASE + "/edit#gid=0", tabs("Первый"))
        db.remember_workbook(BASE + "/edit#gid=1", tabs("Первый", "Новый"))
    assert json.loads(db.all("SELECT tabs FROM workbook_watch")[0]["tabs"]) == tabs("Первый")

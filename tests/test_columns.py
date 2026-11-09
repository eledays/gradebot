import json
from unittest.mock import AsyncMock

import pytest

from gradebot.columns import column_picker
from gradebot.db import Database
from gradebot.monitor import Monitor, format_grades
from gradebot.sheets import Sheet, SheetError
from gradebot.structure import header_signature


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "columns.sqlite3"))
    database.register(123, "Иванов Иван")
    parsed = Sheet([["ФИО", "Квиз", "Экзамен"], ["Иванов Иван", "4", "5"]])
    database.add_sheet("Матан", "https://docs.google.com/spreadsheets/d/id/edit", 1, (1,),
                       999, "approved", parsed=parsed)
    database.snapshot(123, 1, {"Квиз": "3", "Экзамен": "5"}, [])
    yield database
    database.close()


def selection_context(db):
    sheet = db.all("SELECT * FROM sheets WHERE id=1")[0]
    rows = json.loads(db.all("SELECT rows FROM sheet_cache WHERE sheet_id=1")[0]["rows"])
    return {"expected_options": sheet["parser_options"],
            "expected_signature": header_signature(rows, sheet["header_row"],
                                                    json.loads(sheet["parser_options"]).get("header_start"))}


async def test_exclusion_persists_and_preserves_pending_comparisons(db):
    db.update_grade_columns(1, [2], **selection_context(db))
    path = db.all("PRAGMA database_list")[0]["file"]
    reopened = Database(path)
    try:
        monitor = Monitor(reopened, None, AsyncMock())
        report = (await monitor.inspect_user(123))[0]
        assert report["grades"] == {"Квиз": "4"}
        text = format_grades(report)
        assert "Среднее: 4" in text and "Количество: 1" in text and "Экзамен" not in text
        monitor.fetch = AsyncMock(return_value=Sheet(
            [["ФИО", "Квиз", "Экзамен"], ["Иванов Иван", "4", "2"]]))
        await monitor.scan()
        messages = reopened.all("SELECT text FROM outbox WHERE kind='grades'")
        assert len(messages) == 1 and "Квиз: 4" in messages[0]["text"]
        assert "Экзамен" not in messages[0]["text"]
        monitor.fetch.return_value.rows[1][2] = "1"
        assert (await monitor.scan())["notifications"] == 0
    finally:
        reopened.close()


async def test_reenabling_baselines_only_new_columns(db):
    db.update_grade_columns(1, [2], **selection_context(db))
    with db.conn:
        db.cache(1, Sheet([["ФИО", "Квиз", "Экзамен"], ["Иванов Иван", "4", "2"]]))
    db.update_grade_columns(1, [2, 3], **selection_context(db))
    snapshot = json.loads(db.all("SELECT grades FROM snapshots")[0]["grades"])
    assert snapshot == {"Квиз": "3", "Экзамен": "2"}
    monitor = Monitor(db, None, AsyncMock())
    monitor.fetch = AsyncMock(return_value=Sheet(
        [["ФИО", "Квиз", "Экзамен"], ["Иванов Иван", "4", "2"]]))
    await monitor.scan()
    text = db.all("SELECT text FROM outbox WHERE kind='grades'")[0]["text"]
    assert "Квиз: 4" in text and "Экзамен" not in text
    monitor.fetch.return_value.rows[1][2] = "5"
    assert (await monitor.scan())["notifications"] == 1
    assert "Экзамен: 5" in db.all("SELECT text FROM outbox WHERE kind='grades' ORDER BY id DESC")[0]["text"]


async def test_duplicate_labels_are_remapped_by_column(db):
    rows = [["ФИО", "Итог", "Итог"], ["Иванов Иван", "4", "5"]]
    with db.conn:
        db.cache(1, Sheet(rows, 1, (1,), 1, (2, 3)))
        db.conn.execute("UPDATE sheets SET parser_options=?", (json.dumps(
            {"header_start": 1, "grade_columns": [2, 3]}),))
    db.snapshot(123, 1, {"Итог [столбец B]": "3", "Итог [столбец C]": "5"}, [])
    db.update_grade_columns(1, [2], **selection_context(db))
    assert json.loads(db.all("SELECT grades FROM snapshots")[0]["grades"]) == {"Итог": "3"}
    db.update_grade_columns(1, [2, 3], **selection_context(db))
    assert json.loads(db.all("SELECT grades FROM snapshots")[0]["grades"]) == {
        "Итог [столбец B]": "3", "Итог [столбец C]": "5",
    }


@pytest.mark.parametrize("columns", [[], [1], [4]])
def test_invalid_selection_does_not_mutate_database(db, columns):
    options = db.all("SELECT parser_options FROM sheets")[0][0]
    snapshot = db.all("SELECT grades FROM snapshots")[0][0]
    with pytest.raises(SheetError):
        db.update_grade_columns(1, columns, **selection_context(db))
    assert db.all("SELECT parser_options FROM sheets")[0][0] == options
    assert db.all("SELECT grades FROM snapshots")[0][0] == snapshot


@pytest.mark.parametrize("change", ["structure", "options", "deleted"])
def test_stale_selection_is_rejected(db, change):
    expected = selection_context(db)
    if change == "structure":
        with db.conn:
            db.cache(1, Sheet([["ФИО", "Новый квиз", "Экзамен"], ["Иванов Иван", "4", "5"]]))
    elif change == "options":
        db.update_grade_columns(1, [3], **expected)
    else:
        db.execute("DELETE FROM sheets WHERE id=1")
    with pytest.raises(SheetError, match="заново"):
        db.update_grade_columns(1, [2], **expected)


def test_column_picker_bounds_html_and_paginates():
    data = {"column_title": "<&>" * 100, "column_labels": {i: "<&>" * 200 for i in range(2, 22)},
            "column_selected": [2, 10], "column_token": "a" * 16, "column_page": 1}
    text, markup = column_picker(data)
    assert len(text) < 3500 and "<b>&lt;&amp;&gt;" in text
    assert len(markup.inline_keyboard) == 11
    buttons = [button for row in markup.inline_keyboard for button in row]
    assert any(button.callback_data.endswith(":10") and button.text.startswith("✅ J:") for button in buttons)
    assert all("○" not in button.text for button in buttons)
    assert any(button.text == "2/3" for button in buttons)
    assert all(len(button.callback_data.encode()) <= 64 for button in buttons)


def test_column_picker_uses_custom_check_only_for_selected_columns():
    data = {"column_title": "Лист", "column_labels": {2: "Квиз", 3: "Экзамен"},
            "column_selected": [2], "column_token": "a" * 16}
    _, markup = column_picker(data, check_emoji_id="12345")
    selected, unselected = markup.inline_keyboard[:2]
    assert selected[0].icon_custom_emoji_id == "12345" and selected[0].text == "B: Квиз"
    assert unselected[0].icon_custom_emoji_id is None and unselected[0].text == "C: Экзамен"

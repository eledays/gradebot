"""Offline regression checks for failure and deletion paths found during QA."""

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from gradebot.db import Database
from gradebot.monitor import Monitor
from gradebot.sheets import Sheet


def _sheet(value: str) -> Sheet:
    """Build a small synthetic grade sheet without personal data."""
    return Sheet([["ФИО", "Контрольная"], ["Тестов Тест", value]])


@pytest.fixture
def audit_db(tmp_path: Path) -> Iterator[tuple[Database, int]]:
    """Create a temporary SQLite database for audit scenarios."""
    db = Database(str(tmp_path / "audit.sqlite3"))
    db.execute("INSERT INTO settings VALUES('admin','999')")
    db.register(123, "Тестов Тест")
    sheet_id = db.add_sheet(
        "Математика", "https://docs.google.com/spreadsheets/d/synthetic/edit#gid=1",
        1, (1,), 999, "approved", parsed=_sheet("4"),
    )
    yield db, sheet_id
    db.close()


async def test_offline_refresh_recovers_and_delivers_once(audit_db: tuple[Database, int]) -> None:
    """A failed Google fetch must preserve the baseline until recovery."""
    db, sheet_id = audit_db
    bot = AsyncMock()
    monitor = Monitor(db, None, bot)
    await monitor.inspect_user(123, baseline=True)
    monitor.fetch = AsyncMock(side_effect=[TimeoutError(), _sheet("5"), _sheet("5")])

    first = await monitor.scan()
    assert first["updated"] == 0
    assert first["failed"] == ["Математика"]
    assert bot.send_message.await_count == 0
    assert db.cached_rows(sheet_id) is not None

    second = await monitor.scan()
    assert second["updated"] == 1
    assert second["notifications"] == 1
    assert bot.send_message.await_count == 1
    assert "Контрольная: 5" in bot.send_message.await_args.args[1]

    third = await monitor.scan()
    assert third["notifications"] == 0
    assert bot.send_message.await_count == 1


async def test_removed_sheet_cancels_queued_grade_notification(audit_db: tuple[Database, int]) -> None:
    """Deleting a sheet should cancel its undelivered grade alert."""
    db, sheet_id = audit_db
    db.queue(123, ["<b>Найдены новые оценки</b>", "Математика", "- Контрольная: 5"])
    assert db.remove_approved_sheet(sheet_id) is not None

    bot = AsyncMock()
    await Monitor(db, None, bot).deliver()
    bot.send_message.assert_not_awaited()


def test_rejected_proposal_does_not_create_saved_workbook(tmp_path: Path) -> None:
    """A rejected suggestion should disappear from stored table choices."""
    db = Database(str(tmp_path / "proposal.sqlite3"))
    try:
        db.execute("INSERT INTO settings VALUES('admin','999')")
        db.register(123, "Тестов Тест")
        sheet_id = db.add_sheet(
            "Предложение", "https://docs.google.com/spreadsheets/d/proposed/edit#gid=3",
            1, (1,), 123, "pending",
        )
        assert db.moderate_sheet(sheet_id, False, None)
        assert db.workbooks_for_removal() == []
    finally:
        db.close()


@pytest.mark.parametrize("remove_whole_workbook", [False, True])
async def test_deletion_preserves_other_sheet_in_pending_notification(
    audit_db: tuple[Database, int], remove_whole_workbook: bool,
) -> None:
    """A queued multi-sheet alert keeps the surviving sheet and its pagination view."""
    db, first = audit_db
    second = db.add_sheet(
        "История", "https://docs.google.com/spreadsheets/d/history/edit#gid=2",
        1, (1,), 999, "approved", parsed=_sheet("4"),
    )
    bot = AsyncMock()
    monitor = Monitor(db, None, bot)
    await monitor.inspect_user(123, baseline=True)
    monitor.fetch = AsyncMock(side_effect=[_sheet("5"), _sheet("5")])
    bot.send_message.side_effect = RuntimeError("offline")
    await monitor.scan()
    pending = db.all("SELECT * FROM outbox WHERE status='pending'")
    assert len(pending) == 1
    assert "Математика" in pending[0]["text"] and "История" in pending[0]["text"]

    if remove_whole_workbook:
        workbook_id = db.all("SELECT workbook_id FROM sheets WHERE id=?", (first,))[0][0]
        assert db.remove_workbook(workbook_id, [first]) == 1
    else:
        assert db.remove_approved_sheet(first) is not None

    pending = db.all("SELECT * FROM outbox WHERE status='pending'")
    assert len(pending) == 1
    assert "Математика" not in pending[0]["text"]
    assert "История" in pending[0]["text"]
    view = db.view(pending[0]["view_id"])
    assert view is not None and "Математика" not in view["pages"]
    assert db.sheet(second, "approved") is not None

    bot.send_message.side_effect = None
    db.execute("UPDATE outbox SET next_attempt=0")
    await monitor.deliver()
    assert "История" in bot.send_message.await_args.args[1]
    assert "Математика" not in bot.send_message.await_args.args[1]


def test_rejected_proposal_preserves_previously_kept_empty_workbook(
    audit_db: tuple[Database, int],
) -> None:
    """Rejecting a proposal must not erase a table deliberately kept after last-sheet removal."""
    db, approved = audit_db
    workbook_id = db.all("SELECT workbook_id FROM sheets WHERE id=?", (approved,))[0][0]
    assert db.remove_approved_sheet(approved) is not None
    pending = db.add_sheet(
        "Предложение", "https://docs.google.com/spreadsheets/d/synthetic/edit#gid=2",
        1, (1,), 123, "pending",
    )
    assert db.moderate_sheet(pending, False, None)
    assert db.workbook_for_removal(workbook_id) is not None
    assert db.workbook_for_removal(workbook_id)["sheet_ids"] == []

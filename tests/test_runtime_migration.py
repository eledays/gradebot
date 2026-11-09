"""Legacy database migration must keep real rows and avoid overwriting data."""

import sqlite3
from pathlib import Path

from gradebot.runtime import migrate_legacy_database


def test_migrates_legacy_database_after_checkpoint(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    with sqlite3.connect("grades.sqlite3") as connection:
        connection.execute("CREATE TABLE sample (value TEXT)")
        connection.execute("INSERT INTO sample VALUES ('kept')")
        connection.execute("PRAGMA journal_mode=WAL")
    migrate_legacy_database("instance/grades.sqlite3")
    assert not Path("grades.sqlite3").exists()
    with sqlite3.connect("instance/grades.sqlite3") as connection:
        assert connection.execute("SELECT value FROM sample").fetchone()[0] == "kept"
    migrate_legacy_database("instance/grades.sqlite3")

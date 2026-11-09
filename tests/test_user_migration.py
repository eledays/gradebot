"""Local user IDs, legacy snapshots, and CLI removal."""

import sqlite3
import sys

import pytest

from gradebot.cli import _main
from gradebot.db import Database


def test_legacy_users_and_snapshots_get_local_ids(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            INSERT INTO users VALUES(456,'Second'),(123,'First');
            CREATE TABLE sheets(id INTEGER PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL UNIQUE,
                header_row INTEGER NOT NULL, name_columns TEXT NOT NULL, proposer INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending');
            INSERT INTO sheets VALUES(1,'Test','https://docs.google.com/spreadsheets/d/test/edit',1,'[0]',123,'approved');
            CREATE TABLE snapshots(user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                sheet_id INTEGER REFERENCES sheets(id) ON DELETE CASCADE,
                grades TEXT NOT NULL, PRIMARY KEY(user_id,sheet_id));
            INSERT INTO snapshots VALUES(456,1,'{"A":"5"}');
        """)
    db = Database(str(path))
    try:
        assert [(r["id"], r["telegram_id"]) for r in db.all("SELECT * FROM users ORDER BY id")] == [
            (1, 123), (2, 456)
        ]
        assert db.all("SELECT user_id,grades FROM snapshots")[0]["user_id"] == 2
        assert db.all("PRAGMA user_version")[0][0] == 2
        assert db.all("SELECT workbook_id FROM sheets")[0][0] == db.all("SELECT id FROM workbooks")[0][0]
        assert db.all("PRAGMA foreign_key_check") == []
    finally:
        db.close()
    reopened = Database(str(path))
    try:
        assert reopened.all("SELECT user_id FROM snapshots")[0][0] == 2
    finally:
        reopened.close()


def test_v1_workbook_migration_groups_tabs_without_changing_sheet_ids(tmp_path):
    path = tmp_path / "v1.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            PRAGMA user_version=1;
            CREATE TABLE users(id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER NOT NULL UNIQUE,
                name TEXT NOT NULL, notifications INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE sheets(id INTEGER PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL UNIQUE,
                header_row INTEGER NOT NULL, name_columns TEXT NOT NULL, proposer INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', parser_options TEXT NOT NULL DEFAULT '{}');
            INSERT INTO sheets(id,title,url,header_row,name_columns,proposer,status) VALUES
                (7,'Алгебра \u00b7 Лист1','https://docs.google.com/spreadsheets/d/algebra/edit#gid=1',1,'[1]',999,'approved'),
                (8,'Алгебра \u00b7 Лист3','https://docs.google.com/spreadsheets/d/algebra/edit#gid=3',1,'[1]',999,'approved'),
                (9,'Простые оценки','https://docs.google.com/spreadsheets/d/simple/edit',1,'[1]',999,'approved');
        """)
    db = Database(str(path))
    try:
        assert db.all("PRAGMA user_version")[0][0] == 2
        workbooks = db.all("SELECT id,title,url FROM workbooks ORDER BY id")
        assert [(row["title"], row["url"]) for row in workbooks] == [
            ("Алгебра", "https://docs.google.com/spreadsheets/d/algebra"),
            ("Простые оценки", "https://docs.google.com/spreadsheets/d/simple"),
        ]
        assert [(row["id"], row["workbook_id"], row["tab_name"]) for row in db.all(
            "SELECT id,workbook_id,tab_name FROM sheets ORDER BY id"
        )] == [
            (7, workbooks[0]["id"], "Лист1"),
            (8, workbooks[0]["id"], "Лист3"),
            (9, workbooks[1]["id"], "Простые оценки"),
        ]
        assert db.all("PRAGMA foreign_key_check") == []
    finally:
        db.close()


@pytest.mark.parametrize("mode", ["erase", "block"])
def test_cli_user_removal_requires_confirmation_and_cleans_references(
    tmp_path, monkeypatch, capsys, mode
):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "users.sqlite3"
    db = Database(str(path))
    try:
        db.execute("INSERT INTO settings VALUES('admin','999')")
        db.register(123, "Example User")
        local_id = db.all("SELECT id FROM users WHERE telegram_id=123")[0][0]
        db.execute("INSERT INTO name_requests VALUES(123,'Another Name',0)")
        db.execute("INSERT INTO registration_drafts VALUES(123,'token')")
        db.execute("INSERT INTO views(id,owner,pages) VALUES('view',123,'[]')")
        db.execute("INSERT INTO views(id,owner,pages,admin) "
                   "VALUES('members',999,'[\"#123: Example User - ID 123\"]',1)")
        db.execute("INSERT INTO views(id,owner,pages,admin) VALUES('proposal',999,'[]',1)")
        db.execute("INSERT INTO outbox(user_id,text) VALUES(123,'queued')")
        db.execute("INSERT INTO outbox(user_id,text,view_id,kind) "
                   "VALUES(999,'<i>От пользователя 123</i>','proposal','proposal')")
        db.add_sheet("Test", "https://docs.google.com/spreadsheets/d/test/edit", 1, (0,), 123, "pending")
        db.snapshot(123, 1, {"A": "5"}, [])
    finally:
        db.close()
    monkeypatch.setattr(sys, "argv", ["gradebot", "--db", str(path), "user", "list"])
    _main()
    assert f"{local_id}\t123" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["gradebot", "--db", str(path), "user", "delete",
                                      str(local_id), "--mode", mode])
    monkeypatch.setattr("builtins.input", lambda _: "no")
    _main()
    db = Database(str(path))
    try:
        assert db.user_for_removal(local_id) is not None
    finally:
        db.close()
    monkeypatch.setattr("builtins.input", lambda _: f"DELETE {local_id}")
    _main()
    db = Database(str(path))
    try:
        for table in ("users", "members", "snapshots", "views", "outbox",
                      "name_requests", "registration_drafts"):
            assert not db.all(f"SELECT * FROM {table}")
        assert db.all("SELECT proposer FROM sheets")[0][0] == 0
        assert db.blocked(123) == (mode == "block")
        db.register(456, "New User")
        assert db.all("SELECT id FROM users WHERE telegram_id=456")[0][0] > local_id
    finally:
        db.close()
    assert "Cancelled" in capsys.readouterr().out

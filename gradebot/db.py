import json
import sqlite3
from pathlib import Path


class Database:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
        PRAGMA foreign_keys=ON;
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sheets(
          id INTEGER PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL UNIQUE,
          header_row INTEGER NOT NULL, name_columns TEXT NOT NULL,
          proposer INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending');
        CREATE TABLE IF NOT EXISTS snapshots(
          user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
          sheet_id INTEGER REFERENCES sheets(id) ON DELETE CASCADE,
          grades TEXT NOT NULL, PRIMARY KEY(user_id,sheet_id));
        CREATE TABLE IF NOT EXISTS outbox(
          id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, text TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending');
        """)

    def execute(self, sql, args=()):
        with self.conn:
            return self.conn.execute(sql, args)

    def all(self, sql, args=()):
        return self.conn.execute(sql, args).fetchall()

    def admin_id(self):
        row = self.conn.execute("SELECT value FROM settings WHERE key='admin'").fetchone()
        return int(row[0]) if row else None

    def register(self, user_id, name):
        with self.conn:
            old = self.conn.execute("SELECT name FROM users WHERE id=?", (user_id,)).fetchone()
            self.conn.execute(
                "INSERT INTO users VALUES(?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                (user_id, name),
            )
            if old and old[0] != name:
                self.conn.execute("DELETE FROM snapshots WHERE user_id=?", (user_id,))
                self.conn.execute("DELETE FROM outbox WHERE user_id=? AND status='pending'", (user_id,))

    def add_sheet(self, title, url, header_row, columns, proposer, status):
        return self.execute(
            """INSERT INTO sheets(title,url,header_row,name_columns,proposer,status)
                               VALUES(?,?,?,?,?,?)""",
            (title, url, header_row, json.dumps(columns), proposer, status),
        ).lastrowid

    def snapshot(self, user_id, sheet_id, grades, notifications):
        with self.conn:
            self.conn.execute(
                "INSERT INTO snapshots VALUES(?,?,?) ON CONFLICT(user_id,sheet_id) "
                "DO UPDATE SET grades=excluded.grades",
                (user_id, sheet_id, json.dumps(grades)),
            )
            self.conn.executemany(
                "INSERT INTO outbox(user_id,text) VALUES(?,?)",
                [
                    (user_id, text[offset : offset + 4000])
                    for text in notifications
                    for offset in range(0, len(text), 4000)
                ],
            )

    def close(self):
        self.conn.close()

import json
import sqlite3
import time
from pathlib import Path

from .presentation import bold, create_view, table_link
from .sheets import sheet_url


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
        CREATE TABLE IF NOT EXISTS sheet_cache(
          sheet_id INTEGER PRIMARY KEY REFERENCES sheets(id) ON DELETE CASCADE,
          rows TEXT NOT NULL, fetched_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS views(
          id TEXT PRIMARY KEY, owner INTEGER NOT NULL, pages TEXT NOT NULL,
          admin INTEGER NOT NULL DEFAULT 0, actions TEXT NOT NULL DEFAULT '[]');
        """)

        existing = {row["name"] for row in self.all("PRAGMA table_info(outbox)")}
        for column, definition in [("view_id", "TEXT"), ("kind", "TEXT NOT NULL DEFAULT 'grades'")]:
            if column not in existing:
                self.conn.execute(f"ALTER TABLE outbox ADD COLUMN {column} {definition}")
        # Migrate CSV URLs without producing duplicate logical sheets.
        with self.conn:
            for row in self.all("SELECT id,url FROM sheets"):
                self.conn.execute("UPDATE sheets SET url=? WHERE id=?", (sheet_url(row["url"]), row["id"]))

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
                self.conn.execute(
                    "DELETE FROM outbox WHERE user_id=? AND status='pending' AND kind='grades'", (user_id,)
                )

    def add_sheet(self, title, url, header_row, columns, proposer, status, *, parsed=None, notify=False):
        url = sheet_url(url)
        with self.conn:
            sheet_id = self.conn.execute(
                "INSERT INTO sheets(title,url,header_row,name_columns,proposer,status) VALUES(?,?,?,?,?,?)",
                (title, url, header_row, json.dumps(columns), proposer, status),
            ).lastrowid
            if parsed is not None:
                self.cache(sheet_id, parsed)
            if status == "approved" and notify:
                self.broadcast_table(title, url)
            if status == "pending" and notify:
                self.queue(
                    self.admin_id(),
                    [
                        bold(f"Предложение #{sheet_id}"),
                        table_link(title, url),
                        f"<i>От пользователя {proposer}</i>",
                    ],
                    kind="proposal",
                    admin=True,
                    actions=[[("Принять", f"approve:{sheet_id}"), ("Отклонить", f"reject:{sheet_id}")]],
                )
            return sheet_id

    def snapshot(self, user_id, sheet_id, grades, notifications):
        with self.conn:
            self.conn.execute(
                "INSERT INTO snapshots VALUES(?,?,?) ON CONFLICT(user_id,sheet_id) "
                "DO UPDATE SET grades=excluded.grades",
                (user_id, sheet_id, json.dumps(grades)),
            )
            for text in notifications:
                self.queue(user_id, text.splitlines(), kind="grades")

    def cache(self, sheet_id, parsed, fetched_at=None):
        self.conn.execute(
            "INSERT INTO sheet_cache VALUES(?,?,?) ON CONFLICT(sheet_id) "
            "DO UPDATE SET rows=excluded.rows,fetched_at=excluded.fetched_at",
            (sheet_id, json.dumps(parsed.rows), time.time() if fetched_at is None else fetched_at),
        )

    def queue(self, user_id, lines, *, kind="grades", actions=None, admin=False):
        view_id, pages = create_view(self, user_id, lines, admin=admin, actions=actions)
        self.conn.execute(
            "INSERT INTO outbox(user_id,text,view_id,kind) VALUES(?,?,?,?)",
            (user_id, pages[0], view_id, kind),
        )

    def broadcast_table(self, title, url):
        for user in self.all("SELECT id FROM users"):
            self.queue(
                user["id"], [bold("Добавлена новая таблица"), "", table_link(title, url)], kind="table"
            )

    def close(self):
        self.conn.close()

import json
import re
import sqlite3
import time
from contextlib import nullcontext
from pathlib import Path

from .logging_setup import audit
from .presentation import bold, create_view, table_link
from .sheets import sheet_url


class DuplicateTitleError(ValueError):
    pass


def normalized_title(title):
    return " ".join(title.casefold().replace("ё", "е").split())


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
        CREATE TABLE IF NOT EXISTS members(
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, username TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS sheets(
          id INTEGER PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL UNIQUE,
          header_row INTEGER NOT NULL, name_columns TEXT NOT NULL,
          proposer INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending');
        CREATE TABLE IF NOT EXISTS workbook_watch(
          url TEXT PRIMARY KEY, tabs TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sheet_structure(
          sheet_id INTEGER PRIMARY KEY REFERENCES sheets(id) ON DELETE CASCADE,
          signature TEXT NOT NULL, invalid INTEGER NOT NULL DEFAULT 0);
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
        for column, definition in [
            ("view_id", "TEXT"),
            ("kind", "TEXT NOT NULL DEFAULT 'grades'"),
            ("attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("next_attempt", "REAL NOT NULL DEFAULT 0"),
            ("created_at", "REAL NOT NULL DEFAULT 0"),
        ]:
            if column not in existing:
                self.conn.execute(f"ALTER TABLE outbox ADD COLUMN {column} {definition}")
        if "notifications" not in {row["name"] for row in self.all("PRAGMA table_info(users)")}:
            self.conn.execute("ALTER TABLE users ADD COLUMN notifications INTEGER NOT NULL DEFAULT 1")
        if "created_at" not in {row["name"] for row in self.all("PRAGMA table_info(views)")}:
            self.conn.execute("ALTER TABLE views ADD COLUMN created_at REAL NOT NULL DEFAULT 0")
        if "parser_options" not in {row["name"] for row in self.all("PRAGMA table_info(sheets)")}:
            self.conn.execute("ALTER TABLE sheets ADD COLUMN parser_options TEXT NOT NULL DEFAULT '{}'")
        # Migrate CSV URLs without producing duplicate logical sheets.
        with self.conn:
            # Never reuse IDs referenced by old Telegram moderation buttons.
            maximum = self.all("SELECT COALESCE(MAX(id),0) AS n FROM sheets")[0]["n"]
            for view in self.all("SELECT actions FROM views"):
                for match in re.finditer(r"(?:approve|reject|configure):(\d+)", view["actions"]):
                    maximum = max(maximum, int(match[1]))
            self.conn.execute(
                "INSERT INTO settings VALUES('sheet_sequence',?) ON CONFLICT(key) "
                "DO UPDATE SET value=MAX(CAST(settings.value AS INTEGER),CAST(excluded.value AS INTEGER))",
                (str(maximum),),
            )
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
        """Internal administrator/test helper. Telegram users never supply their own name."""
        self.grant_members([(user_id, "", name)])
        self.activate(user_id)

    def authorized(self, user_id):
        return user_id == self.admin_id() or bool(self.all("SELECT id FROM members WHERE id=?", (user_id,)))

    def grant_members(self, entries):
        count = 0
        with self.conn:
            for user_id, username, name in entries:
                count += 1
                self.conn.execute(
                    "INSERT INTO members VALUES(?,?,?) ON CONFLICT(id) "
                    "DO UPDATE SET name=excluded.name,username=excluded.username",
                    (user_id, name, username),
                )
                if self.all("SELECT id FROM users WHERE id=?", (user_id,)):
                    self._save_name(user_id, name)
        if count:
            audit("members_granted", count=count)

    def activate(self, user_id):
        member = self.all("SELECT name FROM members WHERE id=?", (user_id,))
        if not member:
            return False
        with self.conn:
            self._save_name(user_id, member[0]["name"])
        return True

    def _save_name(self, user_id, name):
        old = self.conn.execute("SELECT name FROM users WHERE id=?", (user_id,)).fetchone()
        self.conn.execute(
            "INSERT INTO users(id,name) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name",
            (user_id, name),
        )
        if old and old[0] != name:
            self.conn.execute("DELETE FROM snapshots WHERE user_id=?", (user_id,))
            self.conn.execute(
                "DELETE FROM views WHERE owner=? AND admin=0 AND id NOT IN "
                "(SELECT view_id FROM outbox WHERE user_id=? AND status='pending' "
                "AND kind!='grades' AND view_id IS NOT NULL)",
                (user_id, user_id),
            )
            self.conn.execute(
                "DELETE FROM outbox WHERE user_id=? AND status='pending' AND kind='grades'", (user_id,)
            )

    def revoke(self, user_id):
        with self.conn:
            result = self.conn.execute("DELETE FROM members WHERE id=?", (user_id,))
            self.conn.execute("DELETE FROM users WHERE id=?", (user_id,))
            self.conn.execute("DELETE FROM views WHERE owner=?", (user_id,))
            self.conn.execute("DELETE FROM outbox WHERE user_id=?", (user_id,))
        audit("member_revoked", count=int(bool(result.rowcount)))
        return bool(result.rowcount)

    def toggle_notifications(self, user_id):
        with self.conn:
            self.conn.execute("UPDATE users SET notifications=1-notifications WHERE id=?", (user_id,))
            enabled = self.all("SELECT notifications FROM users WHERE id=?", (user_id,))[0][0]
            if not enabled:
                self.conn.execute(
                    "DELETE FROM outbox WHERE user_id=? AND kind='grades' AND status='pending'", (user_id,)
                )
        return bool(enabled)

    def cleanup(self):
        cutoff = time.time() - 30 * 86400
        with self.conn:
            self.conn.execute("DELETE FROM outbox WHERE status!='pending' AND created_at<?", (cutoff,))
            self.conn.execute(
                "DELETE FROM views WHERE created_at<? AND id NOT IN "
                "(SELECT view_id FROM outbox WHERE status='pending' AND view_id IS NOT NULL)",
                (cutoff,),
            )

    def title_exists(self, title):
        key = normalized_title(title)
        return any(normalized_title(row["title"]) == key for row in self.all("SELECT title FROM sheets"))

    def add_sheet(self, title, url, header_row, columns, proposer, status, *, parsed=None, notify=False):
        url = sheet_url(url)
        with nullcontext() if self.conn.in_transaction else self.conn:
            if self.title_exists(title):
                raise DuplicateTitleError(title)
            sheet_id = int(self.all("SELECT value FROM settings WHERE key='sheet_sequence'")[0][0]) + 1
            self.conn.execute("UPDATE settings SET value=? WHERE key='sheet_sequence'", (str(sheet_id),))
            self.conn.execute(
                "INSERT INTO sheets(id,title,url,header_row,name_columns,proposer,status) VALUES(?,?,?,?,?,?,?)",
                (sheet_id, title, url, header_row, json.dumps(columns), proposer, status),
            )
            if parsed is not None:
                self.conn.execute("UPDATE sheets SET parser_options=? WHERE id=?", (
                    json.dumps({"header_start": parsed.header_start, "grade_columns": parsed.grade_columns}),
                    sheet_id,
                ))
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
                    actions=[[("Настроить", f"configure:{sheet_id}"), ("Отклонить", f"reject:{sheet_id}")]],
                )
        audit("sheet_added", sheet_id=sheet_id)
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
            "INSERT INTO outbox(user_id,text,view_id,kind,created_at) VALUES(?,?,?,?,?)",
            (user_id, pages[0], view_id, kind, time.time()),
        )

    def remember_workbook(self, url, tabs):
        from .structure import workbook_url

        self.conn.execute("INSERT OR IGNORE INTO workbook_watch(url,tabs) VALUES(?,?)",
                          (workbook_url(url), json.dumps(tabs)))

    def notification_users(self):
        """Activated users with access, including an administrator outside the member list."""
        return self.all(
            "SELECT u.* FROM users u WHERE u.id=? "
            "OR EXISTS (SELECT 1 FROM members m WHERE m.id=u.id)",
            (self.admin_id(),),
        )

    def broadcast_table(self, title, url):
        for user in self.notification_users():
            self.queue(
                user["id"], [bold("Добавлена новая таблица"), "", table_link(title, url)], kind="table"
            )

    def close(self):
        self.conn.close()

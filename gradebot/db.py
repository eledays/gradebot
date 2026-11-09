import json
import re
import sqlite3
import time
from contextlib import nullcontext
from pathlib import Path

from .logging_setup import audit
from .presentation import bold, create_view, table_link
from .sheets import Sheet, SheetError, sheet_url
from .structure import header_signature


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
        CREATE TABLE IF NOT EXISTS invitations(
          token TEXT PRIMARY KEY, expires_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS blocked_users(id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS registration_drafts(user_id INTEGER PRIMARY KEY, token TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS name_requests(
          user_id INTEGER PRIMARY KEY, name TEXT NOT NULL, created_at REAL NOT NULL);
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
        """Internal administrator/test helper."""
        self.grant_members([(user_id, "", name)])
        self.activate(user_id)

    def authorized(self, user_id):
        return user_id == self.admin_id() or bool(self.all("SELECT id FROM members WHERE id=?", (user_id,)))

    def blocked(self, user_id):
        return bool(self.all("SELECT 1 FROM blocked_users WHERE id=?", (user_id,)))

    def create_invitation(self, lifetime=86400):
        import secrets
        token = secrets.token_urlsafe(24)
        self.execute("INSERT INTO invitations VALUES(?,?)", (token, time.time() + lifetime))
        return token

    def invitation_valid(self, token):
        return bool(self.all("SELECT 1 FROM invitations WHERE token=? AND expires_at>?", (token, time.time())))

    def begin_join(self, user_id, token):
        if self.blocked(user_id) or not self.invitation_valid(token):
            return False
        self.execute("INSERT INTO registration_drafts VALUES(?,?) ON CONFLICT(user_id) "
                     "DO UPDATE SET token=excluded.token", (user_id, token))
        return True

    def join(self, user_id, username, name, token):
        with self.conn:
            if self.blocked(user_id) or not self.invitation_valid(token):
                return False
            self.conn.execute(
                "INSERT INTO members(id,name,username) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name,username=excluded.username",
                (user_id, name, username or ""),
            )
            self._save_name(user_id, name)
            self.conn.execute("DELETE FROM registration_drafts WHERE user_id=?", (user_id,))
        return True

    def change_name(self, user_id, name):
        with self.conn:
            result = self.conn.execute("UPDATE members SET name=? WHERE id=?", (name, user_id))
            if result.rowcount:
                self._save_name(user_id, name)
                self.conn.execute("DELETE FROM name_requests WHERE user_id=?", (user_id,))
        return bool(result.rowcount)

    def request_name(self, user_id, name):
        self.execute(
            "INSERT INTO name_requests VALUES(?,?,?) ON CONFLICT(user_id) "
            "DO UPDATE SET name=excluded.name,created_at=excluded.created_at",
            (user_id, name, time.time()),
        )

    def block(self, user_id):
        if user_id == self.admin_id():
            return False
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO blocked_users VALUES(?)", (user_id,))
            self.revoke(user_id)
            self.conn.execute("DELETE FROM name_requests WHERE user_id=?", (user_id,))
            self.conn.execute("DELETE FROM registration_drafts WHERE user_id=?", (user_id,))
        return True

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
            self.conn.execute("DELETE FROM invitations WHERE expires_at<?", (time.time(),))
            self.conn.execute("DELETE FROM registration_drafts WHERE token NOT IN (SELECT token FROM invitations)")
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

    def update_grade_columns(self, sheet_id, columns, *, expected_options, expected_signature):
        """Save selection and preserve comparisons for columns that remain enabled."""
        with self.conn:
            sheets = self.all("SELECT * FROM sheets WHERE id=? AND status='approved'", (sheet_id,))
            caches = self.all("SELECT rows FROM sheet_cache WHERE sheet_id=?", (sheet_id,))
            if not sheets or not caches:
                raise SheetError("Таблица или её кэш больше недоступны. Открой /columns заново")
            sheet = sheets[0]
            options = json.loads(sheet["parser_options"])
            rows = json.loads(caches[0]["rows"])
            signature = header_signature(rows, sheet["header_row"], options.get("header_start"))
            if sheet["parser_options"] != expected_options or signature != expected_signature:
                raise SheetError("Структура или настройки изменились. Открой /columns заново")
            old = Sheet(rows, sheet["header_row"], tuple(json.loads(sheet["name_columns"])), **options)
            options["grade_columns"] = sorted(columns)
            parsed = Sheet(rows, sheet["header_row"], old.name_columns, **options)
            for snapshot in self.all(
                "SELECT s.user_id,s.grades,u.name FROM snapshots s JOIN users u ON u.id=s.user_id "
                "WHERE s.sheet_id=?", (sheet_id,),
            ):
                previous = json.loads(snapshot["grades"])
                status, current = parsed.lookup(snapshot["name"])
                grades = {}
                for index, label in parsed.labels.items():
                    if index in old.labels:
                        grades[label] = previous.get(old.labels[index], "")
                    elif status == "found":
                        # Re-enabling a column starts from the current cached value.
                        grades[label] = current[label]
                self.conn.execute("UPDATE snapshots SET grades=? WHERE user_id=? AND sheet_id=?",
                                  (json.dumps(grades), snapshot["user_id"], sheet_id))
            self.conn.execute("UPDATE sheets SET parser_options=? WHERE id=?",
                              (json.dumps(options), sheet_id))

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
        """Enqueue a notification and its view in the current transaction."""
        with nullcontext() if self.conn.in_transaction else self.conn:
            if not self.conn.in_transaction:
                self.conn.execute("BEGIN")
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

    def has_join_draft(self, user_id: int) -> bool:
        """Return whether a user is entering a name through an invitation."""
        return bool(self.all("SELECT 1 FROM registration_drafts WHERE user_id=?", (user_id,)))

    def join_token(self, user_id: int) -> str | None:
        """Return the pending invitation token for a user."""
        rows = self.all("SELECT token FROM registration_drafts WHERE user_id=?", (user_id,))
        return str(rows[0]["token"]) if rows else None

    def discard_join_draft(self, user_id: int) -> None:
        """Remove an unfinished invitation registration."""
        self.execute("DELETE FROM registration_drafts WHERE user_id=?", (user_id,))

    def user_name(self, user_id: int) -> str | None:
        """Return an activated user's name, if present."""
        rows = self.all("SELECT name FROM users WHERE id=?", (user_id,))
        return str(rows[0]["name"]) if rows else None

    def member_name(self, user_id: int) -> str | None:
        """Return a member's registered name, if present."""
        rows = self.all("SELECT name FROM members WHERE id=?", (user_id,))
        return str(rows[0]["name"]) if rows else None

    def name_request(self, user_id: int) -> str | None:
        """Return the requested replacement name, if present."""
        rows = self.all("SELECT name FROM name_requests WHERE user_id=?", (user_id,))
        return str(rows[0]["name"]) if rows else None

    def discard_name_request(self, user_id: int) -> None:
        """Remove a pending name request."""
        self.execute("DELETE FROM name_requests WHERE user_id=?", (user_id,))

    def latest_cache_timestamp(self) -> float | None:
        """Return the newest approved sheet cache timestamp."""
        row = self.all("SELECT MAX(c.fetched_at) AS timestamp FROM sheet_cache c "
                       "JOIN sheets s ON s.id=c.sheet_id WHERE s.status='approved'")[0]
        return row["timestamp"]

    def sheets_by_status(self, status: str) -> list[sqlite3.Row]:
        """List sheets with the requested moderation status."""
        return self.all("SELECT * FROM sheets WHERE status=?", (status,))

    def sheet(self, sheet_id: int, status: str) -> sqlite3.Row | None:
        """Return a sheet only while it has the requested status."""
        rows = self.all("SELECT * FROM sheets WHERE id=? AND status=?", (sheet_id, status))
        return rows[0] if rows else None

    def approved_sheets_with_cache_time(self) -> list[sqlite3.Row]:
        """List approved sheets and their cache timestamps in ID order."""
        return self.all("SELECT s.*,c.fetched_at FROM sheets s "
                        "LEFT JOIN sheet_cache c ON c.sheet_id=s.id "
                        "WHERE s.status='approved' ORDER BY s.id")

    def cached_rows(self, sheet_id: int) -> str | None:
        """Return serialized rows for a sheet, if cached."""
        rows = self.all("SELECT rows FROM sheet_cache WHERE sheet_id=?", (sheet_id,))
        return str(rows[0]["rows"]) if rows else None

    def view(self, view_id: str) -> sqlite3.Row | None:
        """Return a saved pagination view."""
        rows = self.all("SELECT * FROM views WHERE id=?", (view_id,))
        return rows[0] if rows else None

    def member_rows(self) -> list[sqlite3.Row]:
        """List members with activation state for the admin screen."""
        return self.all("SELECT m.*,u.id AS active FROM members m "
                        "LEFT JOIN users u ON u.id=m.id ORDER BY m.name")

    def has_other_members(self) -> bool:
        """Return whether any non-admin members can be revoked."""
        return bool(self.all("SELECT id FROM members WHERE id!=?", (self.admin_id(),)))

    def remove_approved_sheet(self, sheet_id: int) -> bool:
        """Remove an approved sheet by ID."""
        return bool(self.execute("DELETE FROM sheets WHERE id=? AND status='approved'", (sheet_id,)).rowcount)

    def parser_options(self, sheet_id: int, status: str) -> str | None:
        """Return serialized parser options for a sheet in the given state."""
        row = self.sheet(sheet_id, status)
        return str(row["parser_options"]) if row else None

    def set_parser_options(self, sheet_id: int, options: dict[str, object]) -> None:
        """Persist parser options for a sheet."""
        with nullcontext() if self.conn.in_transaction else self.conn:
            self.conn.execute("UPDATE sheets SET parser_options=? WHERE id=?", (json.dumps(options), sheet_id))

    def moderate_sheet(self, sheet_id: int, approve: bool, parsed: Sheet | None) -> bool:
        """Apply a moderation decision and queue resulting notifications atomically."""
        with self.conn:
            sheet = self.sheet(sheet_id, "pending")
            if sheet is None:
                return False
            if approve:
                if parsed is None:
                    raise ValueError("An approved sheet needs parsed data")
                self.conn.execute("UPDATE sheets SET status='approved' WHERE id=?", (sheet_id,))
                self.cache(sheet_id, parsed)
                self.broadcast_table(sheet["title"], sheet["url"])
            else:
                self.conn.execute("DELETE FROM sheets WHERE id=?", (sheet_id,))
            self.queue(sheet["proposer"], [bold("Таблица одобрена" if approve else "Таблица отклонена"),
                       "", table_link(sheet["title"], sheet["url"])], kind="moderation")
        return True

    def save_workbook(self, data: dict[str, object], admin_id: int) -> None:
        """Save configured tabs and resolve their proposal in one transaction."""
        with self.conn:
            if not self.conn.in_transaction:
                self.conn.execute("BEGIN")
            proposal = data.get("proposal_id")
            if proposal:
                if self.sheet(int(proposal), "pending") is None:
                    raise SheetError("Предложение уже обработано")
                self.conn.execute("DELETE FROM sheets WHERE id=?", (proposal,))
            configured = data["configured"]
            for item in configured:
                parsed = Sheet(item["rows"], item["header"], tuple(item["columns"]),
                               item["header_start"], tuple(item["grade_columns"]))
                self.add_sheet(item["title"], item["url"], item["header"], item["columns"],
                               data.get("proposer", admin_id), "approved", parsed=parsed)
            self.remember_workbook(data["tabs"][0]["url"], data["tabs"])
            for item in configured:
                self.broadcast_table(item["title"], item["url"])
            if proposal:
                self.queue(data["proposer"], [bold("Таблица одобрена"),
                           table_link(data["title"], data["url"])], kind="moderation")

    def save_proposal_selection(self, sheet_id: int, selection: dict[str, object],
                                admin_id: int, title: str, url: str, proposer: int,
                                actions: list[list[tuple[str, str]]]) -> None:
        """Store tab choices and the corresponding moderation notification atomically."""
        with self.conn:
            if not self.conn.in_transaction:
                self.conn.execute("BEGIN")
            self.set_parser_options(sheet_id, selection)
            self.queue(admin_id, [bold(f"Предложение #{sheet_id}"), table_link(title, url),
                     f"От пользователя {proposer}",
                     "Выбери листы для парсинга (по умолчанию выбраны все)"],
                     kind="proposal", admin=True, actions=actions)

    def store_view(self, view_id: str, owner: int, pages: list[str], admin: bool,
                   actions: object) -> None:
        """Persist pages and buttons for an owner-scoped message view."""
        with nullcontext() if self.conn.in_transaction else self.conn:
            self.conn.execute("INSERT INTO views(id,owner,pages,admin,actions,created_at) VALUES(?,?,?,?,?,?)",
                              (view_id, owner, json.dumps(pages), int(admin), json.dumps(actions), time.time()))

    def pending_sheet_ids(self) -> set[int]:
        """Return identifiers that still need moderation."""
        return {int(row["id"]) for row in self.all("SELECT id FROM sheets WHERE status='pending'")}

    def close(self):
        self.conn.close()

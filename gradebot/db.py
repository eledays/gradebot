import json
import sqlite3
import time
from collections.abc import Iterable, Sequence
from contextlib import nullcontext
from pathlib import Path
from typing import NotRequired, TypedDict

from .logging_setup import audit
from .messages import TrustedHtml, messages
from .presentation import bold, create_view, table_link
from .schema import initialize_database
from .sheets import Sheet, SheetError, sheet_url
from .structure import header_signature, sheet_group_title, workbook_url


class WorkbookTab(TypedDict):
    """A discoverable tab in a Google workbook."""

    name: str
    url: str


class ConfiguredSheet(TypedDict):
    """A validated tab selected for storage."""

    title: str
    tab_name: str
    url: str
    rows: list[list[str]]
    header: int
    header_start: int | None
    columns: list[int]
    grade_columns: list[int]


class WorkbookDraft(TypedDict):
    """Validated workbook setup persisted by the database layer."""

    configured: list[ConfiguredSheet]
    tabs: list[WorkbookTab]
    title: str
    url: NotRequired[str]
    proposer: NotRequired[int]
    proposal_id: NotRequired[int]
    extension_source: NotRequired[int]
    all_tabs: NotRequired[list[WorkbookTab]]


class ExtensionWorkbook(TypedDict):
    """An existing workbook and its tabs already stored in the database."""

    id: int
    title: str
    url: str
    existing_urls: list[str]


class WorkbookRemoval(TypedDict):
    """A workbook and the exact set of sheets awaiting deletion."""

    id: int
    title: str
    sheet_ids: list[int]


class RemovedSheet(TypedDict):
    """The parent workbook left after removing an approved sheet."""

    workbook_id: int
    workbook_title: str
    workbook_empty: bool


class DuplicateTitleError(ValueError):
    pass


def normalized_title(title: str) -> str:
    """Normalize a sheet title for duplicate detection."""
    return " ".join(title.casefold().replace("ё", "е").split())


class Database:
    def __init__(self, path: str) -> None:
        """Open the database and apply schema migrations."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        initialize_database(self.conn)

    def execute(self, sql: str, args: Sequence[object] = ()) -> sqlite3.Cursor:
        """Execute a statement and commit it outside an existing transaction."""
        with nullcontext() if self.conn.in_transaction else self.conn:
            return self.conn.execute(sql, args)

    def all(self, sql: str, args: Sequence[object] = ()) -> list[sqlite3.Row]:
        """Fetch all rows for a read-only query."""
        return self.conn.execute(sql, args).fetchall()

    def admin_id(self) -> int | None:
        """Return the configured administrator ID."""
        row = self.conn.execute("SELECT value FROM settings WHERE key='admin'").fetchone()
        return int(row[0]) if row else None

    def register(self, user_id: int, name: str) -> None:
        """Internal administrator/test helper."""
        self.grant_members([(user_id, "", name)])
        self.activate(user_id)

    def authorized(self, user_id: int) -> bool:
        """Check whether a user may interact with the bot."""
        return not self.blocked(user_id) and (
            user_id == self.admin_id() or bool(self.all("SELECT id FROM members WHERE id=?", (user_id,)))
        )

    def blocked(self, user_id: int) -> bool:
        """Check whether a user is denied access."""
        return bool(self.all("SELECT 1 FROM blocked_users WHERE id=?", (user_id,)))

    def create_invitation(self, lifetime: int = 86400) -> str:
        """Create an expiring shared invitation token."""
        import secrets
        token = secrets.token_urlsafe(24)
        self.execute("INSERT INTO invitations VALUES(?,?)", (token, time.time() + lifetime))
        return token

    def invitation_valid(self, token: str) -> bool:
        """Check whether an invitation is still usable."""
        return bool(self.all("SELECT 1 FROM invitations WHERE token=? AND expires_at>?", (token, time.time())))

    def begin_join(self, user_id: int, token: str) -> bool:
        """Save a pending invitation for name entry."""
        if self.blocked(user_id) or not self.invitation_valid(token):
            return False
        self.execute("INSERT INTO registration_drafts VALUES(?,?) ON CONFLICT(user_id) "
                     "DO UPDATE SET token=excluded.token", (user_id, token))
        return True

    def join(self, user_id: int, username: str | None, name: str, token: str) -> bool:
        """Activate a member after validating the invitation again."""
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

    def change_name(self, user_id: int, name: str) -> bool:
        """Update a member name and clear stale grade comparisons."""
        with self.conn:
            result = self.conn.execute("UPDATE members SET name=? WHERE id=?", (name, user_id))
            if result.rowcount:
                self._save_name(user_id, name)
                self.conn.execute("DELETE FROM name_requests WHERE user_id=?", (user_id,))
        return bool(result.rowcount)

    def request_name(self, user_id: int, name: str) -> None:
        """Save a name change request for administrator review."""
        self.execute(
            "INSERT INTO name_requests VALUES(?,?,?) ON CONFLICT(user_id) "
            "DO UPDATE SET name=excluded.name,created_at=excluded.created_at",
            (user_id, name, time.time()),
        )

    def block(self, user_id: int) -> bool:
        """Prevent a user from joining and clear their private data."""
        if user_id == self.admin_id():
            return False
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO blocked_users VALUES(?)", (user_id,))
            self.revoke(user_id)
            self.conn.execute("DELETE FROM name_requests WHERE user_id=?", (user_id,))
            self.conn.execute("DELETE FROM registration_drafts WHERE user_id=?", (user_id,))
        return True

    def grant_members(self, entries: Iterable[tuple[int, str, str]]) -> None:
        """Grant access to the supplied members."""
        count = 0
        with self.conn:
            for user_id, username, name in entries:
                count += 1
                self.conn.execute(
                    "INSERT INTO members VALUES(?,?,?) ON CONFLICT(id) "
                    "DO UPDATE SET name=excluded.name,username=excluded.username",
                    (user_id, name, username),
                )
                if self.all("SELECT id FROM users WHERE telegram_id=?", (user_id,)):
                    self._save_name(user_id, name)
        if count:
            audit("members_granted", count=count)

    def activate(self, user_id: int) -> bool:
        """Create an active user record from a member."""
        member = self.all("SELECT name FROM members WHERE id=?", (user_id,))
        if not member:
            return False
        with self.conn:
            self._save_name(user_id, member[0]["name"])
        return True

    def _save_name(self, user_id: int, name: str) -> None:
        """Upsert an active name and invalidate stale grade state."""
        old = self.conn.execute("SELECT name FROM users WHERE telegram_id=?", (user_id,)).fetchone()
        self.conn.execute(
            "INSERT INTO users(telegram_id,name) VALUES(?,?) "
            "ON CONFLICT(telegram_id) DO UPDATE SET name=excluded.name",
            (user_id, name),
        )
        if old and old[0] != name:
            self.conn.execute("DELETE FROM snapshots WHERE user_id="
                              "(SELECT id FROM users WHERE telegram_id=?)", (user_id,))
            self.conn.execute(
                "DELETE FROM views WHERE owner=? AND admin=0 AND id NOT IN "
                "(SELECT view_id FROM outbox WHERE user_id=? AND status='pending' "
                "AND kind!='grades' AND view_id IS NOT NULL)",
                (user_id, user_id),
            )
            self.conn.execute(
                "DELETE FROM outbox WHERE user_id=? AND status='pending' AND kind='grades'", (user_id,)
            )

    def revoke(self, user_id: int) -> bool:
        """Remove a member, their active state, and queued messages."""
        with self.conn:
            result = self.conn.execute("DELETE FROM members WHERE id=?", (user_id,))
            self.conn.execute("DELETE FROM users WHERE telegram_id=?", (user_id,))
            self.conn.execute("DELETE FROM views WHERE owner=?", (user_id,))
            self.conn.execute("DELETE FROM outbox WHERE user_id=?", (user_id,))
        audit("member_revoked", count=int(bool(result.rowcount)))
        return bool(result.rowcount)

    def user_for_removal(self, local_id: int) -> sqlite3.Row | None:
        """Find an active user by local ID without exposing private data."""
        row = self.conn.execute(
            "SELECT id,telegram_id FROM users WHERE id=?", (local_id,)
        ).fetchone()
        return row

    def user_ids(self) -> list[sqlite3.Row]:
        """List local and Telegram IDs for CLI administration."""
        return self.all("SELECT id,telegram_id FROM users ORDER BY id")

    def remove_user(self, local_id: int, *, block: bool) -> bool:
        """Erase a user and related records, optionally barring another registration."""
        with self.conn:
            user = self.user_for_removal(local_id)
            if user is None or user["telegram_id"] == self.admin_id():
                return False
            telegram_id = int(user["telegram_id"])
            # A queued proposal embeds the proposer ID in rendered HTML; remove its view too.
            proposal_views = self.conn.execute(
                "SELECT view_id FROM outbox WHERE kind='proposal' AND "
                "instr(text, '<i>От пользователя ' || ? || '</i>')>0",
                (telegram_id,),
            ).fetchall()
            self.conn.execute(
                "DELETE FROM outbox WHERE kind='proposal' AND "
                "instr(text, '<i>От пользователя ' || ? || '</i>')>0",
                (telegram_id,),
            )
            for row in proposal_views:
                if row["view_id"] is not None:
                    self.conn.execute("DELETE FROM views WHERE id=?", (row["view_id"],))
            self.conn.execute(
                "DELETE FROM views WHERE owner=? AND admin=1 AND instr(pages, 'ID ' || ?)>0",
                (self.admin_id(), telegram_id),
            )
            self.conn.execute("UPDATE sheets SET proposer=0 WHERE proposer=?", (telegram_id,))
            self.conn.execute("DELETE FROM members WHERE id=?", (telegram_id,))
            self.conn.execute("DELETE FROM name_requests WHERE user_id=?", (telegram_id,))
            self.conn.execute("DELETE FROM registration_drafts WHERE user_id=?", (telegram_id,))
            self.conn.execute("DELETE FROM views WHERE owner=?", (telegram_id,))
            self.conn.execute("DELETE FROM outbox WHERE user_id=?", (telegram_id,))
            self.conn.execute("DELETE FROM users WHERE id=?", (local_id,))
            if block:
                self.conn.execute("INSERT OR IGNORE INTO blocked_users(id) VALUES(?)", (telegram_id,))
            else:
                self.conn.execute("DELETE FROM blocked_users WHERE id=?", (telegram_id,))
        audit("user_removed")
        return True

    def toggle_notifications(self, user_id: int) -> bool:
        """Toggle grade notifications and discard queued grades when disabled."""
        with self.conn:
            self.conn.execute("UPDATE users SET notifications=1-notifications WHERE telegram_id=?", (user_id,))
            enabled = self.all("SELECT notifications FROM users WHERE telegram_id=?", (user_id,))[0][0]
            if not enabled:
                self.conn.execute(
                    "DELETE FROM outbox WHERE user_id=? AND kind='grades' AND status='pending'", (user_id,)
                )
        return bool(enabled)

    def cleanup(self) -> None:
        """Remove expired invitations, views, and delivered messages."""
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

    def title_exists(self, title: str) -> bool:
        """Find a sheet with the same normalized title."""
        key = normalized_title(title)
        return any(normalized_title(row["title"]) == key for row in self.all("SELECT title FROM sheets"))

    def _ensure_workbook(self, title: str, url: str) -> int:
        """Return the stored parent ID, creating it for a newly seen Google file."""
        base = workbook_url(url)
        existing = self.all("SELECT id FROM workbooks WHERE url=?", (base,))
        if existing:
            return int(existing[0]["id"])
        return int(self.conn.execute(
            "INSERT INTO workbooks(title,url) VALUES(?,?)", (title, base)
        ).lastrowid)

    def add_sheet(self, title: str, url: str, header_row: int, columns: Sequence[int],
                  proposer: int, status: str, *, parsed: Sheet | None = None, notify: bool = False,
                  workbook_id: int | None = None, tab_name: str | None = None) -> int:
        """Create a sheet and optionally queue a moderation or table message."""
        url = sheet_url(url)
        with nullcontext() if self.conn.in_transaction else self.conn:
            if self.title_exists(title):
                raise DuplicateTitleError(title)
            if workbook_id is None:
                workbook_id = self._ensure_workbook(title, url)
                group = self.all("SELECT title FROM sheets WHERE workbook_id=?", (workbook_id,))
                if group:
                    # Direct additions may arrive one tab at a time without a workbook draft.
                    shared = sheet_group_title([*group, {"title": title}])
                    if shared:
                        self.conn.execute("UPDATE workbooks SET title=? WHERE id=?", (shared, workbook_id))
                        for previous in group:
                            self.conn.execute(
                                "UPDATE sheets SET tab_name=? WHERE workbook_id=? AND title=?",
                                (str(previous["title"]).removeprefix(shared + " · "), workbook_id,
                                 previous["title"]),
                            )
            workbook = self.all("SELECT title,url FROM workbooks WHERE id=?", (workbook_id,))
            if not workbook or workbook[0]["url"] != workbook_url(url):
                raise SheetError("Лист не принадлежит указанной таблице")
            if tab_name is None:
                tab_name = title.removeprefix(str(workbook[0]["title"]) + " · ")
            sheet_id = int(self.all("SELECT value FROM settings WHERE key='sheet_sequence'")[0][0]) + 1
            self.conn.execute("UPDATE settings SET value=? WHERE key='sheet_sequence'", (str(sheet_id),))
            self.conn.execute(
                "INSERT INTO sheets(id,title,url,header_row,name_columns,proposer,status,workbook_id,tab_name) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (sheet_id, title, url, header_row, json.dumps(columns), proposer, status, workbook_id, tab_name),
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

    def update_grade_columns(self, sheet_id: int, columns: Sequence[int], *,
                             expected_options: str, expected_signature: dict[str, object]) -> None:
        """Save selection and preserve comparisons for columns that remain enabled."""
        with self.conn:
            sheets = self.all("SELECT * FROM sheets WHERE id=? AND status='approved'", (sheet_id,))
            caches = self.all("SELECT rows FROM sheet_cache WHERE sheet_id=?", (sheet_id,))
            if not sheets or not caches:
                raise SheetError(messages.render("replies.columns_sheet_unavailable"))
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

    def snapshot(self, user_id: int, sheet_id: int, grades: dict[str, str],
                 notifications: Sequence[str]) -> None:
        """Persist grades and queue detected changes."""
        with self.conn:
            self.conn.execute(
                "INSERT INTO snapshots(user_id,sheet_id,grades) "
                "VALUES((SELECT id FROM users WHERE telegram_id=?),?,?) ON CONFLICT(user_id,sheet_id) "
                "DO UPDATE SET grades=excluded.grades",
                (user_id, sheet_id, json.dumps(grades)),
            )
            for text in notifications:
                self.queue(user_id, text.splitlines(), kind="grades")

    def cache(self, sheet_id: int, parsed: Sheet, fetched_at: float | None = None) -> None:
        """Persist the latest successfully parsed sheet rows."""
        self.conn.execute(
            "INSERT INTO sheet_cache VALUES(?,?,?) ON CONFLICT(sheet_id) "
            "DO UPDATE SET rows=excluded.rows,fetched_at=excluded.fetched_at",
            (sheet_id, json.dumps(parsed.rows), time.time() if fetched_at is None else fetched_at),
        )

    def queue(self, user_id: int, lines: Sequence[str], *, kind: str = "grades",
              actions: object = None, admin: bool = False) -> None:
        """Enqueue a notification and its view in the current transaction."""
        with nullcontext() if self.conn.in_transaction else self.conn:
            if not self.conn.in_transaction:
                self.conn.execute("BEGIN")
            view_id, pages = create_view(self, user_id, lines, admin=admin, actions=actions)
            self.conn.execute(
                "INSERT INTO outbox(user_id,text,view_id,kind,created_at) VALUES(?,?,?,?,?)",
                (user_id, pages[0], view_id, kind, time.time()),
            )

    def remember_workbook(self, url: str, tabs: list[dict[str, str]], *, replace: bool = False) -> None:
        """Record discovered tabs and refresh stored names of saved sheets."""
        base = workbook_url(url)
        if replace:
            self.conn.execute("INSERT INTO workbook_watch(url,tabs) VALUES(?,?) "
                              "ON CONFLICT(url) DO UPDATE SET tabs=excluded.tabs",
                              (base, json.dumps(tabs)))
        else:
            self.conn.execute("INSERT OR IGNORE INTO workbook_watch(url,tabs) VALUES(?,?)",
                              (base, json.dumps(tabs)))
        for tab in tabs:
            self.conn.execute(
                "UPDATE sheets SET tab_name=? WHERE url=? AND workbook_id IN "
                "(SELECT id FROM workbooks WHERE url=?)",
                (tab["name"], sheet_url(tab["url"]), base),
            )

    def extension_workbooks(self) -> list[ExtensionWorkbook]:
        """List approved and empty workbooks that can accept new tabs."""
        return [self._extension_group(row) for row in self.all(
            "SELECT w.* FROM workbooks w WHERE EXISTS "
            "(SELECT 1 FROM sheets s WHERE s.workbook_id=w.id AND s.status='approved') "
            "OR NOT EXISTS (SELECT 1 FROM sheets s WHERE s.workbook_id=w.id) "
            "ORDER BY w.id"
        )]

    def extension_source(self, workbook_id: int) -> ExtensionWorkbook | None:
        """Find an approved or empty workbook by its own ID."""
        rows = self.all(
            "SELECT w.* FROM workbooks w WHERE w.id=? AND (EXISTS "
            "(SELECT 1 FROM sheets s WHERE s.workbook_id=w.id AND s.status='approved') "
            "OR NOT EXISTS (SELECT 1 FROM sheets s WHERE s.workbook_id=w.id))",
            (workbook_id,),
        )
        return self._extension_group(rows[0]) if rows else None

    def extension_source_for_sheet(self, sheet_id: int) -> ExtensionWorkbook | None:
        """Resolve a legacy add-tab button that carried a sheet ID."""
        sheet = self.sheet(sheet_id, "approved")
        return self.extension_source(int(sheet["workbook_id"])) if sheet else None

    def _extension_group(self, workbook: sqlite3.Row) -> ExtensionWorkbook:
        """Build an extension choice from one persisted parent workbook."""
        sheets = self.all("SELECT url FROM sheets WHERE workbook_id=? AND status='approved'", (workbook["id"],))
        return {
            "id": int(workbook["id"]),
            "title": str(workbook["title"]),
            "url": str(workbook["url"]) + "/edit",
            "existing_urls": [sheet_url(row["url"]) for row in sheets],
        }

    def notification_users(self) -> list[sqlite3.Row]:
        """Activated users with access, including an administrator outside the member list."""
        return self.all(
            "SELECT u.* FROM users u WHERE (u.telegram_id=? "
            "OR EXISTS (SELECT 1 FROM members m WHERE m.id=u.telegram_id)) "
            "AND NOT EXISTS (SELECT 1 FROM blocked_users b WHERE b.id=u.telegram_id)",
            (self.admin_id(),),
        )

    def broadcast_table(self, title: str, url: str) -> None:
        """Queue a new-sheet announcement for active users."""
        for user in self.notification_users():
            self.queue(
                user["telegram_id"],
                [messages.render("notifications.new_sheet",
                                 sheet=TrustedHtml(table_link(self.grade_display_title(title, url), url)))],
                kind="table",
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
        rows = self.all("SELECT name FROM users WHERE telegram_id=?", (user_id,))
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
        return self.all("SELECT s.*,w.title AS workbook_title,c.fetched_at FROM sheets s "
                        "JOIN workbooks w ON w.id=s.workbook_id "
                        "LEFT JOIN sheet_cache c ON c.sheet_id=s.id "
                        "WHERE s.status='approved' ORDER BY s.id")

    def cached_rows(self, sheet_id: int) -> str | None:
        """Return serialized rows for a sheet, if cached."""
        rows = self.all("SELECT rows FROM sheet_cache WHERE sheet_id=?", (sheet_id,))
        return str(rows[0]["rows"]) if rows else None

    def grade_display_title(self, title: str, url: str) -> str:
        """Show the persisted parent title and tab name for grouped sheets."""
        stored = self.all(
            "SELECT w.title AS workbook_title,s.tab_name,s.workbook_id FROM sheets s "
            "JOIN workbooks w ON w.id=s.workbook_id WHERE s.url=?", (sheet_url(url),),
        )
        if stored:
            count = self.all(
                "SELECT COUNT(*) AS n FROM sheets WHERE workbook_id=? AND status='approved'",
                (stored[0]["workbook_id"],),
            )[0]["n"]
            if count > 1:
                return f"{stored[0]['workbook_title']}. {stored[0]['tab_name']}"
        if " · " in title:
            return title.replace(" · ", ". ")
        watched = self.all("SELECT tabs FROM workbook_watch WHERE url=?", (workbook_url(url),))
        if not watched:
            return title
        tabs = json.loads(watched[0]["tabs"])
        if len(tabs) < 2:
            return title
        for tab in tabs:
            if sheet_url(tab["url"]) == sheet_url(url):
                prefix = stored[0]["workbook_title"] if stored else title
                name = stored[0]["tab_name"] if stored else tab["name"]
                return f"{prefix}. {name}"
        return title

    def view(self, view_id: str) -> sqlite3.Row | None:
        """Return a saved pagination view."""
        rows = self.all("SELECT * FROM views WHERE id=?", (view_id,))
        return rows[0] if rows else None

    def member_rows(self) -> list[sqlite3.Row]:
        """List members with activation state for the admin screen."""
        return self.all("SELECT m.*,u.id AS active FROM members m "
                        "LEFT JOIN users u ON u.telegram_id=m.id ORDER BY m.name")

    def has_other_members(self) -> bool:
        """Return whether any non-admin members can be revoked."""
        return bool(self.all("SELECT id FROM members WHERE id!=?", (self.admin_id(),)))

    def approved_sheet_with_workbook(self, sheet_id: int) -> sqlite3.Row | None:
        """Return one approved sheet with its parent title for confirmation."""
        rows = self.all(
            "SELECT s.*,w.title AS workbook_title FROM sheets s "
            "JOIN workbooks w ON w.id=s.workbook_id WHERE s.id=? AND s.status='approved'",
            (sheet_id,),
        )
        return rows[0] if rows else None

    def workbooks_for_removal(self) -> list[sqlite3.Row]:
        """List all stored workbooks, including empty ones."""
        return self.all("SELECT id,title FROM workbooks ORDER BY id")

    def workbook_for_removal(self, workbook_id: int) -> WorkbookRemoval | None:
        """Read a workbook and the precise set of its saved sheet IDs."""
        rows = self.all("SELECT id,title FROM workbooks WHERE id=?", (workbook_id,))
        if not rows:
            return None
        sheets = self.all("SELECT id FROM sheets WHERE workbook_id=? ORDER BY id", (workbook_id,))
        return {"id": int(rows[0]["id"]), "title": str(rows[0]["title"]),
                "sheet_ids": [int(row["id"]) for row in sheets]}

    def remove_approved_sheet(self, sheet_id: int) -> RemovedSheet | None:
        """Remove one approved sheet and report whether its parent became empty."""
        with self.conn:
            if not self.conn.in_transaction:
                self.conn.execute("BEGIN IMMEDIATE")
            sheet = self.approved_sheet_with_workbook(sheet_id)
            if sheet is None:
                return None
            self.conn.execute("DELETE FROM sheets WHERE id=? AND status='approved'", (sheet_id,))
            empty = not self.all("SELECT 1 FROM sheets WHERE workbook_id=?", (sheet["workbook_id"],))
            return {"workbook_id": int(sheet["workbook_id"]),
                    "workbook_title": str(sheet["workbook_title"]), "workbook_empty": empty}

    def remove_workbook(self, workbook_id: int, expected_sheet_ids: Sequence[int]) -> int | None:
        """Delete a workbook and all its sheets only if its contents are unchanged."""
        with self.conn:
            if not self.conn.in_transaction:
                self.conn.execute("BEGIN IMMEDIATE")
            workbook = self.workbook_for_removal(workbook_id)
            if workbook is None or workbook["sheet_ids"] != list(expected_sheet_ids):
                return None
            self.conn.execute("DELETE FROM sheets WHERE workbook_id=?", (workbook_id,))
            self.conn.execute("DELETE FROM workbook_watch WHERE url=(SELECT url FROM workbooks WHERE id=?)",
                              (workbook_id,))
            self.conn.execute("DELETE FROM workbooks WHERE id=?", (workbook_id,))
            return len(workbook["sheet_ids"])

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

    def save_workbook(self, data: WorkbookDraft, admin_id: int) -> None:
        """Save configured tabs and resolve their proposal in one transaction."""
        with self.conn:
            if not self.conn.in_transaction:
                self.conn.execute("BEGIN")
            proposal = data.get("proposal_id")
            extension = data.get("extension_source")
            if extension:
                source = self.extension_source(extension)
                if source is None or workbook_url(source["url"]) != workbook_url(data["all_tabs"][0]["url"]):
                    raise SheetError("Исходная таблица больше недоступна")
                if any(sheet_url(item["url"]) in source["existing_urls"] for item in data["configured"]):
                    raise SheetError("Один из листов уже добавлен")
                workbook_id = source["id"]
                self.conn.execute("UPDATE workbooks SET title=? WHERE id=?", (data["title"], workbook_id))
            else:
                workbook_url_input = data.get("url") or data.get("all_tabs", data["tabs"])[0]["url"]
                workbook_id = self._ensure_workbook(data["title"], workbook_url_input)
                if not self.all("SELECT 1 FROM sheets WHERE workbook_id=? AND status='approved'", (workbook_id,)):
                    self.conn.execute("UPDATE workbooks SET title=? WHERE id=?", (data["title"], workbook_id))
            if proposal:
                if self.sheet(int(proposal), "pending") is None:
                    raise SheetError("Предложение уже обработано")
                self.conn.execute("DELETE FROM sheets WHERE id=?", (proposal,))
            configured = data["configured"]
            for item in configured:
                parsed = Sheet(item["rows"], item["header"], tuple(item["columns"]),
                               item["header_start"], tuple(item["grade_columns"]))
                self.add_sheet(item["title"], item["url"], item["header"], item["columns"],
                               data.get("proposer", admin_id), "approved", parsed=parsed,
                               workbook_id=workbook_id, tab_name=item["tab_name"])
            all_tabs = data.get("all_tabs", data["tabs"])
            self.remember_workbook(all_tabs[0]["url"], all_tabs, replace=bool(extension))
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

    def close(self) -> None:
        """Close the SQLite connection."""
        self.conn.close()

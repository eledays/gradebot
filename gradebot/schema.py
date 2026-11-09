"""SQLite schema creation and additive migrations."""

import json
import re
import sqlite3

from .sheets import sheet_url
from .structure import (
    LEGACY_TITLE_SEPARATOR,
    TITLE_SEPARATOR,
    sheet_group_title,
    tab_name_from_title,
    workbook_url,
)

SCHEMA_VERSION = 2


def initialize_database(conn: sqlite3.Connection) -> None:
    """Create the legacy baseline and apply numbered, transactional migrations."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RuntimeError("Database schema is newer than this version of gradebot")
    conn.executescript("""
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

    existing = {row["name"] for row in conn.execute("PRAGMA table_info(outbox)")}
    for column, definition in [
        ("view_id", "TEXT"),
        ("kind", "TEXT NOT NULL DEFAULT 'grades'"),
        ("attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("next_attempt", "REAL NOT NULL DEFAULT 0"),
        ("created_at", "REAL NOT NULL DEFAULT 0"),
        ("grade_sections", "TEXT"),
    ]:
        if column not in existing:
            conn.execute(f"ALTER TABLE outbox ADD COLUMN {column} {definition}")
    if "notifications" not in {row["name"] for row in conn.execute("PRAGMA table_info(users)")}:
        conn.execute("ALTER TABLE users ADD COLUMN notifications INTEGER NOT NULL DEFAULT 1")
    if "created_at" not in {row["name"] for row in conn.execute("PRAGMA table_info(views)")}:
        conn.execute("ALTER TABLE views ADD COLUMN created_at REAL NOT NULL DEFAULT 0")
    if "parser_options" not in {row["name"] for row in conn.execute("PRAGMA table_info(sheets)")}:
        conn.execute("ALTER TABLE sheets ADD COLUMN parser_options TEXT NOT NULL DEFAULT '{}'")
    if version == 0:
        _migrate_users(conn)
    if version < 2:
        _migrate_workbooks(conn)
    if "retain_empty" not in {row["name"] for row in conn.execute("PRAGMA table_info(workbooks)")}:
        # Existing empty workbooks may have been deliberately kept after removing their last sheet.
        conn.execute("ALTER TABLE workbooks ADD COLUMN retain_empty INTEGER NOT NULL DEFAULT 1")
    # Migrate CSV URLs without producing duplicate logical sheets.
    with conn:
        conn.execute("CREATE INDEX IF NOT EXISTS sheets_workbook_status ON sheets(workbook_id,status)")
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS sheets_require_parent_insert BEFORE INSERT ON sheets "
            "WHEN NEW.workbook_id IS NULL OR NEW.tab_name='' BEGIN "
            "SELECT RAISE(ABORT,'sheet requires a workbook and tab name'); END"
        )
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS sheets_require_parent_update "
            "BEFORE UPDATE OF workbook_id,tab_name ON sheets "
            "WHEN NEW.workbook_id IS NULL OR NEW.tab_name='' BEGIN "
            "SELECT RAISE(ABORT,'sheet requires a workbook and tab name'); END"
        )
        # Never reuse IDs referenced by old Telegram moderation buttons.
        maximum = conn.execute("SELECT COALESCE(MAX(id),0) AS n FROM sheets").fetchone()["n"]
        for view in conn.execute("SELECT actions FROM views"):
            for match in re.finditer(r"(?:approve|reject|configure):(\d+)", view["actions"]):
                maximum = max(maximum, int(match[1]))
        conn.execute(
            "INSERT INTO settings VALUES('sheet_sequence',?) ON CONFLICT(key) "
            "DO UPDATE SET value=MAX(CAST(settings.value AS INTEGER),CAST(excluded.value AS INTEGER))",
            (str(maximum),),
        )
        for row in conn.execute("SELECT id,url FROM sheets"):
            conn.execute("UPDATE sheets SET url=? WHERE id=?", (sheet_url(row["url"]), row["id"]))


def _migrate_users(conn: sqlite3.Connection) -> None:
    """Replace Telegram-keyed users with local IDs and remap grade snapshots."""
    with conn:
        conn.execute(
            "CREATE TABLE users_new(id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "telegram_id INTEGER NOT NULL UNIQUE, name TEXT NOT NULL, "
            "notifications INTEGER NOT NULL DEFAULT 1)"
        )
        conn.execute(
            "INSERT INTO users_new(telegram_id,name,notifications) "
            "SELECT id,name,notifications FROM users ORDER BY id"
        )
        conn.execute(
            "CREATE TABLE snapshots_new(user_id INTEGER REFERENCES users_new(id) ON DELETE CASCADE, "
            "sheet_id INTEGER REFERENCES sheets(id) ON DELETE CASCADE, "
            "grades TEXT NOT NULL, PRIMARY KEY(user_id,sheet_id))"
        )
        conn.execute(
            "INSERT INTO snapshots_new(user_id,sheet_id,grades) "
            "SELECT u.id,s.sheet_id,s.grades FROM snapshots s "
            "JOIN users_new u ON u.telegram_id=s.user_id"
        )
        conn.execute("DROP TABLE snapshots")
        conn.execute("DROP TABLE users")
        conn.execute("ALTER TABLE users_new RENAME TO users")
        conn.execute("ALTER TABLE snapshots_new RENAME TO snapshots")
        conn.execute("PRAGMA user_version=1")


def _migrate_workbooks(conn: sqlite3.Connection) -> None:
    """Persist one parent workbook for every existing group of tabs."""
    with conn:
        conn.execute(
            "CREATE TABLE workbooks(id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "title TEXT NOT NULL, url TEXT NOT NULL UNIQUE)"
        )
        conn.execute("ALTER TABLE sheets ADD COLUMN workbook_id INTEGER REFERENCES workbooks(id)")
        conn.execute("ALTER TABLE sheets ADD COLUMN tab_name TEXT NOT NULL DEFAULT ''")
        groups: dict[str, list[sqlite3.Row]] = {}
        for row in conn.execute("SELECT id,title,url FROM sheets ORDER BY id"):
            groups.setdefault(workbook_url(row["url"]), []).append(row)
        for url, sheets in groups.items():
            title = sheet_group_title(sheets) or str(sheets[0]["title"])
            watched = conn.execute("SELECT tabs FROM workbook_watch WHERE url=?", (url,)).fetchone()
            known_tabs = {
                sheet_url(tab["url"]): tab["name"]
                for tab in json.loads(watched["tabs"])
            } if watched else {}
            if len(sheets) == 1:
                tab_name = known_tabs.get(sheet_url(sheets[0]["url"]))
                if tab_name:
                    for separator in (TITLE_SEPARATOR, LEGACY_TITLE_SEPARATOR):
                        if title.endswith(separator + tab_name):
                            title = title.removesuffix(separator + tab_name)
                            break
            workbook_id = conn.execute(
                "INSERT INTO workbooks(title,url) VALUES(?,?)", (title, url)
            ).lastrowid
            conn.executemany(
                "UPDATE sheets SET workbook_id=?,tab_name=? WHERE id=?",
                [(
                    workbook_id,
                    known_tabs.get(sheet_url(sheet["url"]),
                                   tab_name_from_title(str(sheet["title"]), title)),
                    sheet["id"],
                ) for sheet in sheets],
            )
        conn.execute("PRAGMA user_version=2")

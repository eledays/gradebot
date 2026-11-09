"""SQLite schema creation and additive migrations."""

import re
import sqlite3

from .sheets import sheet_url


def initialize_database(conn: sqlite3.Connection) -> None:
    """Create schema and migrate older databases without changing stored grades."""
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
    ]:
        if column not in existing:
            conn.execute(f"ALTER TABLE outbox ADD COLUMN {column} {definition}")
    if "notifications" not in {row["name"] for row in conn.execute("PRAGMA table_info(users)")}:
        conn.execute("ALTER TABLE users ADD COLUMN notifications INTEGER NOT NULL DEFAULT 1")
    if "created_at" not in {row["name"] for row in conn.execute("PRAGMA table_info(views)")}:
        conn.execute("ALTER TABLE views ADD COLUMN created_at REAL NOT NULL DEFAULT 0")
    if "parser_options" not in {row["name"] for row in conn.execute("PRAGMA table_info(sheets)")}:
        conn.execute("ALTER TABLE sheets ADD COLUMN parser_options TEXT NOT NULL DEFAULT '{}'")
    # Migrate CSV URLs without producing duplicate logical sheets.
    with conn:
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

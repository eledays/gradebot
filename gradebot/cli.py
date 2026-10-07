import argparse
import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from .db import Database


def main():
    load_dotenv(Path.cwd() / ".env", override=False)
    parser = argparse.ArgumentParser(description="Telegram grade bot for one university group")
    parser.add_argument("--db", default=os.getenv("DATABASE_PATH", "grades.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    admin = commands.add_parser("admin", help="Set the single administrator Telegram ID")
    admin.add_argument("telegram_id", type=int)
    run = commands.add_parser("run", help="Start polling Telegram and Google Sheets")
    run.add_argument("--interval", type=int, default=int(os.getenv("POLL_INTERVAL", "60")))
    args = parser.parse_args()
    db = Database(args.db)
    try:
        if args.command == "admin":
            if args.telegram_id <= 0:
                parser.error("Telegram ID must be positive")
            db.execute(
                "INSERT INTO settings VALUES('admin',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(args.telegram_id),),
            )
            print(f"Administrator: {args.telegram_id}. Register via /start in Telegram as well.")
        else:
            if args.interval < 10:
                parser.error("Polling interval must be at least 10 seconds")
            token = os.getenv("BOT_TOKEN")
            if not token:
                parser.error("Set BOT_TOKEN in the environment")
            if db.admin_id() is None:
                parser.error("Assign an administrator first: gradebot admin TELEGRAM_ID")
            logging.basicConfig(level=logging.INFO)
            from .bot import run_bot

            asyncio.run(run_bot(db, token, args.interval, os.getenv("CONTACT_USERNAME", "")))
    finally:
        db.close()


if __name__ == "__main__":
    main()

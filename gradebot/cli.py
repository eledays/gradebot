import argparse
import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from .db import Database
from .logging_setup import audit, configure_logging


def main():
    try:
        _main()
    except Exception as exc:
        audit("process_failed", level=logging.ERROR, error=exc)
        raise SystemExit("Бот завершил работу из-за ошибки; проверьте журнал событий") from None


def _main():
    load_dotenv(Path.cwd() / ".env", override=False)
    parser = argparse.ArgumentParser(description="Telegram grade bot for one university group")
    parser.add_argument("--db", default=os.getenv("DATABASE_PATH", "grades.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    admin = commands.add_parser("admin", help="Set the single administrator Telegram ID")
    admin.add_argument("telegram_id", type=int)
    run = commands.add_parser("run", help="Start polling Telegram and Google Sheets")
    run.add_argument("--interval", type=int, default=int(os.getenv("POLL_INTERVAL", "60")))
    args = parser.parse_args()
    try:
        configure_logging(os.getenv("LOG_LEVEL", "INFO").upper())
    except ValueError as exc:
        parser.error(str(exc))
    db = Database(args.db)
    try:
        if args.command == "admin":
            if args.telegram_id <= 0:
                parser.error("Telegram ID must be positive")
            db.execute(
                "INSERT INTO settings VALUES('admin',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(args.telegram_id),),
            )
            audit("admin_configured")
            print(f"Administrator: {args.telegram_id}. Open /start in Telegram to manage access and tables.")
        else:
            if args.interval < 10:
                parser.error("Polling interval must be at least 10 seconds")
            token = os.getenv("BOT_TOKEN")
            if not token:
                parser.error("Set BOT_TOKEN in the environment")
            if db.admin_id() is None:
                parser.error("Assign an administrator first: gradebot admin TELEGRAM_ID")
            proxy_url = os.getenv("PROXY_URL", "").strip() or None
            from .network import ProxyConfigurationError, proxy_options

            try:
                proxy_options(proxy_url)
            except ProxyConfigurationError as exc:
                parser.error(str(exc))
            from .bot import run_bot
            from .runtime import AlreadyRunningError, database_run_lock

            try:
                with database_run_lock(args.db):
                    audit("bot_starting", interval=args.interval, proxy=bool(proxy_url))
                    try:
                        asyncio.run(
                            run_bot(
                                db,
                                token,
                                args.interval,
                                os.getenv("CONTACT_USERNAME", ""),
                                proxy_url=proxy_url,
                            )
                        )
                    finally:
                        audit("bot_stopped")
            except AlreadyRunningError as exc:
                parser.error(str(exc))
    finally:
        db.close()


if __name__ == "__main__":
    main()

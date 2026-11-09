import argparse
import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv


def main() -> None:
    """Run the command-line entry point with safe error reporting."""
    try:
        _main()
    except Exception as exc:
        from .logging_setup import audit

        audit("process_failed", level=logging.ERROR, error=exc)
        raise SystemExit("Бот завершил работу из-за ошибки; проверьте журнал событий") from None


def _main() -> None:
    """Parse configuration and start administrator setup or polling."""
    load_dotenv(Path.cwd() / ".env", override=False)
    parser = argparse.ArgumentParser(description="Telegram grade bot for one university group")
    parser.add_argument("--db", default=os.getenv("DATABASE_PATH", "instance/grades.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    admin = commands.add_parser("admin", help="Set the single administrator Telegram ID")
    admin.add_argument("telegram_id", type=int)
    user = commands.add_parser("user", help="Manage an active user by local ID")
    user_commands = user.add_subparsers(dest="user_command", required=True)
    user_commands.add_parser("list", help="List local and Telegram IDs")
    delete = user_commands.add_parser("delete", help="Erase or block a user")
    delete.add_argument("local_id", type=int)
    delete.add_argument("--mode", choices=("erase", "block"), required=True)
    run = commands.add_parser("run", help="Start polling Telegram and Google Sheets")
    run.add_argument("--interval", type=int, default=int(os.getenv("POLL_INTERVAL", "60")))
    args = parser.parse_args()
    from .db import Database
    from .logging_setup import audit, configure_logging
    from .runtime import AlreadyRunningError, database_run_lock, migrate_legacy_database

    try:
        configure_logging(os.getenv("LOG_LEVEL", "INFO").upper())
    except ValueError as exc:
        parser.error(str(exc))
    try:
        migrate_legacy_database(args.db)
        Path(args.db).parent.mkdir(parents=True, exist_ok=True)
        with database_run_lock(args.db):
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
                elif args.command == "user":
                    if args.user_command == "list":
                        for row in db.user_ids():
                            print(f"{row['id']}\t{row['telegram_id']}")
                        return
                    if args.local_id <= 0:
                        parser.error("Local user ID must be positive")
                    target = db.user_for_removal(args.local_id)
                    if target is None:
                        parser.error("User not found")
                    if target["telegram_id"] == db.admin_id():
                        parser.error("Cannot remove the current administrator")
                    print(f"Local ID: {target['id']}; Telegram ID: {target['telegram_id']}; mode: {args.mode}")
                    try:
                        confirmation = input(f"Type DELETE {args.local_id} to confirm: ")
                    except EOFError:
                        confirmation = ""
                    if confirmation != f"DELETE {args.local_id}":
                        print("Cancelled")
                        return
                    db.remove_user(args.local_id, block=args.mode == "block")
                    print("User removed" if args.mode == "erase" else "User blocked")
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
            finally:
                db.close()
    except AlreadyRunningError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()

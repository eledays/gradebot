"""Hold a process lock for the database while polling Telegram."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class AlreadyRunningError(RuntimeError):
    pass


@contextmanager
def database_run_lock(path: str | Path) -> Iterator[None]:
    # Deployment is on POSIX. Keep the lock file: unlinking it permits competing inodes.
    """Prevent two bot processes from using the same database."""
    import fcntl

    lock_path = Path(str(Path(path).resolve()) + ".run.lock")
    with lock_path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AlreadyRunningError("Бот с этой БД уже запущен. Сначала останови другой процесс.") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def migrate_legacy_database(path: str) -> None:
    """Move the old default database into instance when no bot holds its lock."""
    destination = Path(path)
    legacy = Path("grades.sqlite3")
    if destination != Path("instance/grades.sqlite3") or destination.exists() or not legacy.exists():
        return
    with database_run_lock(legacy):
        with sqlite3.connect(legacy) as connection:
            busy, _, _ = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if busy:
                raise AlreadyRunningError("База данных используется другим процессом")
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Проверка целостности старой базы данных не прошла")
        destination.parent.mkdir(parents=True, exist_ok=True)
        legacy.rename(destination)
        for suffix in ("-wal", "-shm"):
            Path(str(legacy) + suffix).unlink(missing_ok=True)

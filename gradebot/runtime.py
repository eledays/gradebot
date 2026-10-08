"""Hold a process lock for the database while polling Telegram."""

from contextlib import contextmanager
from pathlib import Path


class AlreadyRunningError(RuntimeError):
    pass


@contextmanager
def database_run_lock(path):
    # Deployment is on POSIX. Keep the lock file: unlinking it permits competing inodes.
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

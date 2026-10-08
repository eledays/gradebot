import io
import logging
import sys
from unittest.mock import AsyncMock

import pytest

from gradebot.logging_setup import SafeFormatter, audit

SECRETS = (
    "Иванов Иван 123456:SECRET_TOKEN socks5://login:PASSWORD@host:1080 "
    "https://docs.google.com/spreadsheets/d/SECRET_SHEET/edit Семинар 1: 5"
)


@pytest.fixture
def log_stream():
    logger = logging.getLogger("gradebot.audit")
    previous = logger.handlers[:], logger.level, logger.propagate
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(SafeFormatter())
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    yield stream
    logger.handlers, logger.level, logger.propagate = previous


def test_audit_logs_only_allowlisted_metadata(log_stream):
    audit(
        "cache_refresh_failed",
        level=logging.WARNING,
        sheet_id=2,
        error=TimeoutError(SECRETS),
        name=SECRETS,
        user_id=999,
        url=SECRETS,
        grade=5,
    )
    text = log_stream.getvalue()
    assert "cache_refresh_failed error=timeout sheet_id=2" in text
    assert "999" not in text and "grade" not in text and "Иванов" not in text
    assert "SECRET" not in text and "PASSWORD" not in text and "Семинар" not in text


def test_raw_library_logs_and_tracebacks_are_never_rendered():
    try:
        raise RuntimeError(SECRETS)
    except RuntimeError:
        record = logging.LogRecord(
            "aiogram.event", logging.ERROR, __file__, 1, "Update: %s", (SECRETS,), sys.exc_info()
        )
    text = SafeFormatter().format(record)
    assert text.endswith("ERROR external_log")
    assert SECRETS not in text and "Traceback" not in text


def test_raw_application_logs_and_forged_fields_are_safe():
    record = logging.LogRecord("gradebot.audit", logging.INFO, __file__, 1, SECRETS, (), None)
    record.audit_event = "sheet_added"
    record.audit_fields = {"sheet_id": SECRETS, "name": SECRETS, "error": SECRETS, "count": 1}
    text = SafeFormatter().format(record)
    assert text.endswith("sheet_added count=1")
    assert SECRETS not in text


async def test_failed_refresh_and_delivery_do_not_log_exception_payload(log_stream, tmp_path):
    from gradebot.db import Database
    from gradebot.monitor import Monitor
    from gradebot.sheets import Sheet

    db = Database(str(tmp_path / "logs.sqlite3"))
    try:
        db.register(123, "Иванов Иван")
        db.add_sheet(
            "SECRET_SUBJECT",
            "https://docs.google.com/spreadsheets/d/SECRET_SHEET/edit",
            1,
            (1,),
            123,
            "approved",
            parsed=Sheet([["ФИО", "С1"], ["Иванов Иван", "5"]]),
        )
        monitor = Monitor(db, None, AsyncMock())
        monitor.fetch = AsyncMock(side_effect=RuntimeError(SECRETS))
        monitor.bot.send_message.side_effect = RuntimeError(SECRETS)
        await monitor.scan()
        output = log_stream.getvalue()
        assert "cache_refresh_failed" in output and "notification_retry" in output
        assert "refresh_finished" in output
        assert all(secret not in output for secret in ["Иванов", "SECRET", "PASSWORD", "Семинар", "123"])
    finally:
        db.close()


def test_startup_exception_does_not_expose_secrets(monkeypatch, log_stream):
    import gradebot.cli as cli

    def failing_main():
        raise RuntimeError(SECRETS)

    monkeypatch.setattr(cli, "_main", failing_main)
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert "SECRET" not in str(error.value)
    assert "process_failed error=internal" in log_stream.getvalue()


def test_malformed_record_and_output_failure_cannot_dump_message(capsys):
    from gradebot.logging_setup import SafeStreamHandler

    record = logging.LogRecord("gradebot.audit", logging.ERROR, __file__, 1, SECRETS, (), None)
    record.audit_event = ["invalid"]
    assert SafeFormatter().format(record).endswith("ERROR external_log")
    record.audit_event = "sheet_added"
    record.audit_fields = {"count": 10**1000}
    assert SafeFormatter().format(record).endswith("ERROR sheet_added")

    class BrokenStream:
        def write(self, value):
            raise OSError(SECRETS)

    handler = SafeStreamHandler(BrokenStream())
    handler.setFormatter(SafeFormatter())
    handler.emit(record)
    assert SECRETS not in capsys.readouterr().err

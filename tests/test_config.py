import sys

import pytest

from gradebot.bot import help_message
from gradebot.db import Database


@pytest.mark.parametrize("interval,label", [(30, "30 сек"), (60, "1 мин"), (90, "90 сек"), (3600, "1 ч")])
def test_help_displays_actual_interval_and_contact(interval, label):
    text = help_message(interval, "@group_admin")
    assert "<b>Описание</b>" in text
    assert "<b>Управление</b>" in text
    assert f"каждые {label}" in text
    assert text.endswith("Для связи: @group_admin")
    assert "<b>Админ</b>" not in text and "/add" not in text
    assert "/start" not in text


def test_help_admin_block_and_optional_contact():
    text = help_message(60, "", admin=True)
    assert "<b>Админ</b>" in text
    assert all(command in text for command in ["/add", "/pending", "/remove"])
    assert "Для связи" not in text
    assert "@&lt;invalid&gt;" in help_message(60, "<invalid>")


@pytest.mark.parametrize(
    "environment_interval,cli_interval,expected", [(None, None, 60), ("90", None, 90), ("90", "120", 120)]
)
def test_cli_loads_env_and_respects_override_priority(
    tmp_path, monkeypatch, environment_interval, cli_interval, expected
):
    from gradebot.cli import main

    monkeypatch.chdir(tmp_path)
    for name in ["BOT_TOKEN", "DATABASE_PATH", "POLL_INTERVAL", "CONTACT_USERNAME"]:
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text(
        "BOT_TOKEN=fake-token\nDATABASE_PATH=group.sqlite3\nPOLL_INTERVAL=60\nCONTACT_USERNAME=group_admin\n"
    )
    db = Database(str(tmp_path / "group.sqlite3"))
    db.execute("INSERT INTO settings VALUES('admin','999')")
    db.close()
    if environment_interval:
        monkeypatch.setenv("POLL_INTERVAL", environment_interval)
    arguments = ["gradebot", "run"]
    if cli_interval:
        arguments.extend(["--interval", cli_interval])
    monkeypatch.setattr(sys, "argv", arguments)
    calls = []

    async def fake_run(db, token, interval, contact):
        calls.append((db.admin_id(), token, interval, contact))

    monkeypatch.setattr("gradebot.bot.run_bot", fake_run)
    main()
    assert calls == [(999, "fake-token", expected, "group_admin")]

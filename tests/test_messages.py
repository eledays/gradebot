"""Message templates must preserve formatting and escape user values."""

from pathlib import Path

import pytest

from gradebot.messages import MessageCatalog, TrustedHtml


def test_catalog_escapes_arguments_and_allows_explicit_html(tmp_path: Path) -> None:
    source = tmp_path / "messages.toml"
    source.write_text('[example]\nmessage = "<b>{name}</b> {fragment}"\n')
    catalog = MessageCatalog(source)
    assert catalog.render("example.message", name="<Ivan>", fragment=TrustedHtml("<i>ok</i>")) == (
        "<b>&lt;Ivan&gt;</b> <i>ok</i>"
    )
    with pytest.raises(ValueError, match="requires"):
        catalog.render("example.message", name="Ivan")


def test_packaged_help_and_welcome_templates() -> None:
    catalog = MessageCatalog()
    assert "<b>Описание</b>" in catalog.render("help.body", period="60 сек")
    assert "&lt;Иван&gt;" in catalog.render("registration.welcome", first_name="<Иван>")

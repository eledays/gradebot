"""Editable Telegram HTML text templates with escaped dynamic arguments."""

from __future__ import annotations

import string
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from html import escape
from importlib.resources import files
from pathlib import Path


@dataclass(frozen=True)
class TrustedHtml:
    """A fragment deliberately generated as safe Telegram HTML."""

    value: str


class MessageCatalog:
    """Read TOML templates and render their placeholders safely."""

    def __init__(self, path: Path | None = None) -> None:
        source = path or Path(str(files("gradebot").joinpath("messages.toml")))
        with source.open("rb") as handle:
            self._sections: dict[str, dict[str, str]] = tomllib.load(handle)

    def render(self, key: str, /, **values: str | int | TrustedHtml) -> str:
        """Render a dotted key, escaping arguments unless marked as trusted HTML."""
        section, separator, name = key.partition(".")
        if not separator:
            raise KeyError(f"Expected section.key: {key}")
        template = self._sections[section][name]
        fields = {field for _, field, _, _ in string.Formatter().parse(template) if field}
        if fields != values.keys():
            raise ValueError(f"Template {key} requires {sorted(fields)}; got {sorted(values)}")
        safe = {name: value.value if isinstance(value, TrustedHtml) else escape(str(value))
                for name, value in values.items()}
        return template.format_map(safe)

    def templates(self) -> Mapping[str, Mapping[str, str]]:
        """Expose loaded templates for validation and inspection."""
        return self._sections


messages = MessageCatalog()

"""Persistent workbook and header comparisons, independent of student grade values."""

import sqlite3
from collections.abc import Mapping, Sequence
from typing import TypeVar

from .presentation import bold, safe_lines, table_link
from .sheets import SheetError, detect_layout, sheet_url

SheetRow = Mapping[str, object] | sqlite3.Row
RowT = TypeVar("RowT", bound=SheetRow)



def workbook_url(url: str) -> str:
    """Return the canonical workbook URL without tab selection."""
    return sheet_url(url).split("/edit")[0]


def group_sheets(sheets: Sequence[RowT]) -> list[list[RowT]]:
    """Group sheets by their Google workbook."""
    groups = {}
    for sheet in sheets:
        groups.setdefault(workbook_url(sheet["url"]), []).append(sheet)
    return list(groups.values())


def sheet_group_title(sheets: Sequence[SheetRow]) -> str:
    """Find the shared prefix in a group of tab titles."""
    common = []
    for parts in zip(*(sheet["title"].split(" · ")[:-1] for sheet in sheets), strict=False):
        if len(set(parts)) != 1:
            break
        common.append(parts[0])
    return " · ".join(common)


def header_signature(rows: list[list[str]], header_row: int,
                     header_start: int | None = None) -> dict[str, object]:
    """Summarize layout and headers without student grade data."""
    width = max((len(row) for row in rows), default=0)
    start = header_start or header_row
    headers = [[value.strip() for value in row] for row in rows[start - 1:header_row]]
    # CSV padding is irrelevant; the overall width is tracked separately.
    for row in headers:
        while row and not row[-1]:
            row.pop()
    try:
        detected = detect_layout(rows)
        detected = {key: detected[key] for key in ("header_start", "header", "columns")}
        detected["columns"] = list(detected["columns"])
    except SheetError:
        detected = None
    return {"width": width, "headers": headers, "detected": detected}


def workbook_changes(old: Sequence[dict[str, str]], new: Sequence[dict[str, str]]) -> list[str]:
    """Describe added, renamed, or removed workbook tabs."""
    old = {sheet_url(tab["url"]): tab["name"] for tab in old}
    new = {sheet_url(tab["url"]): tab["name"] for tab in new}
    lines = []
    for url, name in new.items():
        if url not in old:
            lines.append("Новый лист: " + table_link(name, url))
        elif old[url] != name:
            lines.extend(safe_lines(f"Лист переименован: {old[url]} → {name}"))
    for url, name in old.items():
        if url not in new:
            lines.extend(safe_lines(f"Лист удалён или скрыт: {name}"))
    return lines


def structure_message(sheet: SheetRow, old: dict[str, object] | None,
                      new: dict[str, object], invalid: bool = False) -> list[str]:
    """Build a notification about a changed sheet structure."""
    lines = [bold("Изменилась структура листа"), table_link(sheet["title"], sheet["url"])]
    if old and old["width"] != new["width"]:
        lines.extend(safe_lines(f"Число столбцов: {old['width']} → {new['width']}"))
    if not old or old["headers"] != new["headers"]:
        lines.append("Изменились заголовки, их порядок или число уровней")
    if old and old["detected"] != new["detected"]:
        lines.append("Изменилось расположение ФИО или строк заголовков")
    if invalid:
        lines.append("Текущие настройки больше не позволяют прочитать лист. Сохранён предыдущий кэш")
    lines.append("Проверьте настройки парсинга. Для повторной настройки удалите лист через /remove и добавьте через /add")
    return lines

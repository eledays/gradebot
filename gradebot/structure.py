"""Persistent workbook and header comparisons, independent of student grade values."""

from .presentation import bold, safe_lines, table_link
from .sheets import SheetError, detect_layout, sheet_url


def workbook_url(url):
    return sheet_url(url).split("/edit")[0]


def header_signature(rows, header_row, header_start=None):
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


def workbook_changes(old, new):
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


def structure_message(sheet, old, new, invalid=False):
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

"""CSV transport and conservative, order-independent name matching."""

import csv
import io
import re
from collections import Counter
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

import aiohttp


class SheetError(ValueError):
    pass


def export_url(url: str) -> str:
    parts = urlsplit(url.strip())
    match = re.fullmatch(r"/spreadsheets/d/([A-Za-z0-9_-]{1,200})(?:/[^?]*)?", parts.path)
    if parts.scheme != "https" or parts.netloc != "docs.google.com" or not match:
        raise SheetError("Нужна ссылка https://docs.google.com/spreadsheets/d/.../edit#gid=...")
    params = parse_qs(parts.query)
    params.update(parse_qs(parts.fragment))
    gid = params.get("gid", ["0"])[0]
    if not re.fullmatch(r"[0-9]{1,20}", gid):
        raise SheetError("gid листа должен быть числом")
    return f"https://docs.google.com/spreadsheets/d/{match[1]}/export?format=csv&gid={gid}"


def sheet_url(url: str) -> str:
    """Canonical human-readable URL, also accepts legacy CSV export links."""
    canonical = export_url(url)
    parts = urlsplit(canonical)
    sheet_id = parts.path.split("/")[3]
    gid = parse_qs(parts.query)["gid"][0]
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit#gid={gid}"


def tokens(name: str) -> Counter:
    return Counter(re.findall(r"[^\W\d_]+(?:-[^\W\d_]+)*", name.casefold().replace("ё", "е")))


def valid_name(name: str) -> bool:
    return (
        len(name) <= 150
        and 2 <= sum(tokens(name).values()) <= 3
        and re.fullmatch(r"[^\W\d_]+(?:-[^\W\d_]+)*(?:\s+[^\W\d_]+(?:-[^\W\d_]+)*){1,2}", name.strip())
        is not None
    )


def matches(left: str, right: str) -> bool:
    a, b = tokens(left), tokens(right)
    if not (2 <= sum(a.values()) <= 3 and 2 <= sum(b.values()) <= 3):
        return False
    return a == b or (min(sum(a.values()), sum(b.values())) == 2 and (a <= b or b <= a))


@dataclass
class Sheet:
    rows: list[list[str]]
    header_row: int = 1
    name_columns: tuple[int, ...] = (1,)

    def __post_init__(self):
        if self.header_row < 1 or not self.name_columns or min(self.name_columns) < 1:
            raise SheetError("Номера строк и столбцов начинаются с 1")
        if len(self.rows) <= self.header_row:
            raise SheetError("Нет строк студентов после заголовка")
        width = len(self.rows[self.header_row - 1])
        if max(self.name_columns) > width:
            raise SheetError("Столбец ФИО находится за пределами таблицы")
        headers = [
            v.strip()
            for i, v in enumerate(self.rows[self.header_row - 1], 1)
            if i not in self.name_columns and v.strip()
        ]
        if not headers or len(headers) != len(set(headers)):
            raise SheetError("Нужны непустые уникальные заголовки столбцов оценок")
        if any(
            len(row) > width and any(v.strip() for v in row[width:]) for row in self.rows[self.header_row :]
        ):
            raise SheetError("Есть данные за пределами заголовков")

    def lookup(self, name: str) -> tuple[str, dict[str, str]]:
        candidates = []
        for row in self.rows[self.header_row :]:
            candidate = " ".join(row[i - 1] if i <= len(row) else "" for i in self.name_columns)
            if matches(name, candidate):
                candidates.append(row)
        if len(candidates) != 1:
            return ("ambiguous" if candidates else "missing"), {}
        row = candidates[0]
        grades = {
            label.strip(): row[i].strip() if i < len(row) else ""
            for i, label in enumerate(self.rows[self.header_row - 1])
            if i + 1 not in self.name_columns and label.strip()
        }
        return "found", grades


async def fetch_sheet(session: aiohttp.ClientSession, url: str, header_row=1, name_columns=(1,)) -> Sheet:
    async with session.get(export_url(url), timeout=aiohttp.ClientTimeout(total=30)) as response:
        if response.status != 200:
            raise SheetError(f"Google вернул HTTP {response.status}; проверьте доступ по ссылке")
        data = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            data.extend(chunk)
            if len(data) > 2_000_000:
                raise SheetError("Таблица больше 2 МБ")
        text = data.decode("utf-8-sig")
        if "text/html" in response.headers.get("Content-Type", "") or text.lstrip().startswith("<"):
            raise SheetError("Google вернул страницу вместо CSV; откройте доступ на чтение по ссылке")
        return Sheet(list(csv.reader(io.StringIO(text))), header_row, tuple(name_columns))

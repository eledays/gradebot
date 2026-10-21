"""CSV transport and conservative, order-independent name matching."""

import csv
import io
import json
import re
from collections import Counter
from dataclasses import dataclass
from html.parser import HTMLParser
from itertools import permutations
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


def one_typo(left: str, right: str) -> bool:
    if min(len(left), len(right)) < 4 or abs(len(left) - len(right)) > 1:
        return False
    if len(left) > len(right):
        left, right = right, left
    if len(left) == len(right):
        positions = [i for i, (a, b) in enumerate(zip(left, right, strict=True)) if a != b]
        return len(positions) == 1 or (
            len(positions) == 2 and positions[1] == positions[0] + 1
            and left[positions[0]] == right[positions[1]]
            and left[positions[1]] == right[positions[0]]
        )
    return any(left == right[:i] + right[i + 1:] for i in range(len(right)))


def matches(left: str, right: str, *, fuzzy=True) -> bool:
    a, b = tokens(left), tokens(right)
    if not (2 <= sum(a.values()) <= 3 and 2 <= sum(b.values()) <= 3):
        return False
    if a == b or (min(sum(a.values()), sum(b.values())) == 2 and (a <= b or b <= a)):
        return True
    if not fuzzy:
        return False
    short, long = list(a.elements()), list(b.elements())
    if len(short) > len(long):
        short, long = long, short
    for order in permutations(long, len(short)):
        differences = [(x, y) for x, y in zip(short, order, strict=True) if x != y]
        if len(differences) == 1 and one_typo(*differences[0]):
            return True
    return False


@dataclass
class Sheet:
    rows: list[list[str]]
    header_row: int = 1
    name_columns: tuple[int, ...] = (1,)
    header_start: int | None = None
    grade_columns: tuple[int, ...] | None = None

    def __post_init__(self):
        if self.header_row < 1 or not self.name_columns or min(self.name_columns) < 1:
            raise SheetError("Номера строк и столбцов начинаются с 1")
        if len(self.rows) <= self.header_row:
            raise SheetError("Нет строк студентов после заголовка")
        start = self.header_start or self.header_row
        if not 1 <= start <= self.header_row:
            raise SheetError("Неверный диапазон заголовков")
        width = max(len(row) for row in self.rows[start - 1:self.header_row])
        if self.grade_columns is not None and (
            not self.grade_columns or min(self.grade_columns) < 1 or max(self.grade_columns) > width
            or len(set(self.grade_columns)) != len(self.grade_columns)
            or set(self.grade_columns) & set(self.name_columns)
        ):
            raise SheetError("Выберите столбцы оценок в пределах таблицы, отдельно от ФИО")
        paths = [[] for _ in range(width)]
        for level, row in enumerate(self.rows[start - 1:self.header_row]):
            previous, parent = "", None
            for index in range(width):
                if index + 1 in self.name_columns:
                    previous, parent = "", None
                    continue
                current_parent = tuple(paths[index])
                if current_parent != parent:
                    previous = ""
                value = row[index].strip() if index < len(row) else ""
                # CSV omits merged cells: fill group levels only, within their parent.
                if level < self.header_row - start:
                    if value:
                        previous = value
                    value = value or previous
                if value and (not paths[index] or paths[index][-1] != value):
                    paths[index].append(value)
                parent = current_parent
        self.labels = {i + 1: " → ".join(path) for i, path in enumerate(paths)
                       if path and i + 1 not in self.name_columns
                       and (self.grade_columns is None or i + 1 in self.grade_columns)}
        if max(self.name_columns) > width:
            raise SheetError("Столбец ФИО находится за пределами таблицы")
        headers = list(self.labels.values())
        if not headers or len(headers) != len(set(headers)):
            if self.header_start is None and self.grade_columns is None:
                raise SheetError("Нужны непустые уникальные заголовки столбцов оценок")
            counts = Counter(headers)
            self.labels = {i: f"{label} [столбец {column_letter(i)}]" if counts[label] > 1 else label
                           for i, label in self.labels.items()}
            if not headers:
                raise SheetError("Не выбрано ни одного столбца оценок")
        if any(
            len(row) > width and any(v.strip() for v in row[width:]) for row in self.rows[self.header_row :]
        ):
            raise SheetError("Есть данные за пределами заголовков")

    def lookup(self, name: str) -> tuple[str, dict[str, str]]:
        exact_candidates, candidates, fuzzy_candidates = [], [], []
        name_tokens = tokens(name)
        for row in self.rows[self.header_row :]:
            candidate = " ".join(row[i - 1] if i <= len(row) else "" for i in self.name_columns)
            if matches(name, candidate, fuzzy=False):
                if tokens(candidate) == name_tokens:
                    exact_candidates.append(row)
                else:
                    candidates.append(row)
            elif matches(name, candidate):
                fuzzy_candidates.append(row)
        candidates = exact_candidates or candidates or fuzzy_candidates
        if len(candidates) != 1:
            return ("ambiguous" if candidates else "missing"), {}
        row = candidates[0]
        grades = {label: row[i - 1].strip() if i <= len(row) else ""
                  for i, label in self.labels.items()}
        return "found", grades


async def fetch_sheet(session: aiohttp.ClientSession, url: str, header_row=1, name_columns=(1,),
                      *, header_start=None, grade_columns=None, raw=False):
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
        rows = list(csv.reader(io.StringIO(text)))
        if raw:
            return rows
        try:
            return Sheet(rows, header_row, tuple(name_columns),
                         header_start, tuple(grade_columns) if grade_columns is not None else None)
        except SheetError as exc:
            # Let the monitor inspect header changes even when parsing no longer succeeds.
            exc.rows = rows
            raise


def column_letter(number):
    result = ""
    while number:
        number, digit = divmod(number - 1, 26)
        result = chr(65 + digit) + result
    return result


def column_number(value):
    value = value.strip().upper()
    if value.isdigit() and int(value) > 0:
        return int(value)
    if not re.fullmatch(r"[A-Z]{1,3}", value):
        raise SheetError("Столбцы задаются буквами или номерами: C,E:G или 3,5-7")
    result = 0
    for char in value:
        result = result * 26 + ord(char) - 64
    return result


def parse_columns(text, width):
    result = set()
    for item in text.split(","):
        parts = re.split(r"[:-]", item.strip())
        if len(parts) > 2:
            raise SheetError("Неверный диапазон столбцов")
        first, last = column_number(parts[0]), column_number(parts[-1])
        if not 1 <= first <= last <= width:
            raise SheetError("Столбцы находятся за пределами таблицы")
        result.update(range(first, last + 1))
    return tuple(sorted(result))


class TabsParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tabs, self.gid, self.parts = [], None, []
        self.in_script = False

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.in_script = True
        attrs = dict(attrs)
        match = re.fullmatch(r"sheet-button-(\d+)", attrs.get("id", ""))
        if match:
            self.gid, self.parts = match[1], []

    def handle_data(self, data):
        if self.in_script:
            # Modern htmlview builds its tab menu from these JS records.
            # Read literal strings only; never execute the remote script.
            pattern = (r'items\.push\(\{\s*name:\s*("(?:\\.|[^"\\])*")'
                       r'\s*,\s*pageUrl:\s*"(?:\\.|[^"\\])*"\s*,\s*gid:\s*"([0-9]{1,20})"')
            for match in re.finditer(pattern, data):
                literal = re.sub(r"\\x([0-9a-fA-F]{2})", r"\\u00\1", match[1])
                try:
                    name = json.loads(literal)
                except json.JSONDecodeError:
                    continue
                self.tabs.append((match[2], name))
        if self.gid is not None:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False
        if tag == "li" and self.gid is not None:
            self.tabs.append((self.gid, "".join(self.parts).strip()))
            self.gid = None


async def discover_sheets(session, url):
    canonical = sheet_url(url)
    base = canonical.split("/edit")[0]
    async with session.get(base + "/htmlview", timeout=aiohttp.ClientTimeout(total=30)) as response:
        if response.status != 200:
            raise SheetError(f"Google вернул HTTP {response.status}; проверьте доступ по ссылке")
        data = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            data.extend(chunk)
            if len(data) > 5_000_000:
                raise SheetError("Список листов больше 5 МБ")
    parser = TabsParser()
    parser.feed(data.decode("utf-8-sig"))
    if not parser.tabs:
        raise SheetError("Не удалось распознать список листов в ответе Google; попробуйте повторить позже")
    return [{"name": name or f"Лист {gid}", "url": base + f"/edit#gid={gid}"}
            for gid, name in dict(parser.tabs).items()]


def detect_layout(rows):
    """Suggest a layout only when a name header or repeated full names supports it."""
    name_labels = {"фио", "ф.и.о.", "фамилия имя отчество", "фамилия", "имя", "отчество"}
    for index, row in enumerate(rows[:100]):
        columns = tuple(i for i, value in enumerate(row, 1)
                        if value.strip().casefold().replace("ё", "е") in name_labels)
        if not columns:
            continue
        for first in range(index + 1, min(len(rows), 100)):
            name = " ".join(rows[first][i-1] if i <= len(rows[first]) else "" for i in columns)
            if valid_name(name):
                parsed = Sheet(rows, first, columns, index + 1)
                grades = tuple(i for i, label in parsed.labels.items()
                               if label.strip().casefold() not in {"№", "номер", "п/п", "№ п/п"})
                if grades:
                    return {"header_start": index + 1, "header": first,
                            "columns": columns, "grade_columns": grades}
    raise SheetError("Не удалось уверенно определить ФИО и заголовки")

import pytest

from gradebot.sheets import Sheet, SheetError, export_url, matches


@pytest.mark.parametrize(
    "left,right",
    [
        ("Иванов Иван Иванович", "Иван Иванович Иванов"),
        ("Иванов Иван Иванович", "Иван Иванов"),
        ("Семёнов Пётр", "петр семенов"),
    ],
)
def test_names(left, right):
    assert matches(left, right)


def test_no_fuzzy_or_initial_matches():
    assert not matches("Иванов Иван", "Иванова Ирина")
    assert not matches("Иванов Иван Иванович", "Иванов Иван Петрович")
    assert not matches("Иванов Иван", "Иванов И.")


def test_ambiguous_names_do_not_choose_first():
    sheet = Sheet([["ФИО", "Семинар"], ["Иванов Иван Иванович", "5"], ["Иванов Иван Петрович", "2"]])
    assert sheet.lookup("Иванов Иван") == ("ambiguous", {})
    assert sheet.lookup("Иванов Иван Иванович") == ("found", {"Семинар": "5"})


def test_split_names_header_offset_and_short_rows():
    sheet = Sheet(
        [["Предмет"], ["Фамилия", "Имя", "Отчество", "С1", "С2"], ["Иванов", "Иван", "Иванович", "5"]],
        2,
        (1, 2, 3),
    )
    assert sheet.lookup("Иван Иванов") == ("found", {"С1": "5", "С2": ""})
    assert sheet.lookup("Петров Петр") == ("missing", {})


def test_column_reordering_preserves_grade_keys():
    sheet = Sheet([["С2", "ФИО", "С1"], ["4", "Иванов Иван", "5"]], 1, (2,))
    assert sheet.lookup("Иванов Иван")[1] == {"С2": "4", "С1": "5"}


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [["ФИО", "С1"]],
        [["ФИО", "С1", "С1"], ["Иванов Иван", "5"]],
        [["ФИО", "С1"], ["Иванов Иван", "5", "4"]],
    ],
)
def test_bad_layout(rows):
    with pytest.raises(SheetError):
        Sheet(rows)


def test_google_url_canonicalization():
    assert (
        export_url("https://docs.google.com/spreadsheets/d/abc_123/edit?usp=sharing#gid=42")
        == "https://docs.google.com/spreadsheets/d/abc_123/export?format=csv&gid=42"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://docs.google.com/spreadsheets/d/id/edit",
        "https://evil.com/spreadsheets/d/id/edit",
        "https://docs.google.com/document/d/id/edit",
        "https://docs.google.com/spreadsheets/d/id/edit#gid=foo",
    ],
)
def test_reject_bad_links(url):
    with pytest.raises(SheetError):
        export_url(url)


async def test_fetch_reads_every_chunk_and_bom():
    from unittest.mock import MagicMock

    from gradebot.sheets import fetch_sheet

    class Response:
        status = 200
        headers = {"Content-Type": "text/csv"}
        content = None

        def __init__(self):
            self.content = self

        async def iter_chunked(self, size):
            for data in [b"\xef\xbb\xbf", "ФИО,С1\nИванов ".encode(), "Иван,5\n".encode()]:
                yield data

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    session = MagicMock()
    session.get.return_value = Response()
    sheet = await fetch_sheet(session, "https://docs.google.com/spreadsheets/d/id/edit")
    assert sheet.lookup("Иван Иванов") == ("found", {"С1": "5"})


@pytest.mark.parametrize("name", ["Иванов123 Иван", "Иванов И.", "Иванов", "Иванов Иван /start"])
def test_registration_rejects_invalid_names(name):
    from gradebot.sheets import valid_name

    assert not valid_name(name)

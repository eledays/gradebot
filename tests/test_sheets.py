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


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("query,expected", [
    ("иванович ИВАН семенов", "5"),
    ("Иван Семёнов", "3"),
])
def test_exact_name_wins_over_partial_name(reverse, query, expected):
    rows = [["Семёнов Иван Иванович", "5"], ["Семенов Иван", "3"]]
    sheet = Sheet([["ФИО", "Семинар"], *(reversed(rows) if reverse else rows)])
    assert sheet.lookup(query) == ("found", {"Семинар": expected})


def test_duplicate_exact_names_remain_ambiguous_with_partial_match():
    sheet = Sheet([["ФИО", "Семинар"], ["Иванов Иван", "3"],
                   ["Иванов Иван Иванович", "5"], ["Иван Иванович Иванов", "4"]])
    assert sheet.lookup("Иванов Иван Иванович") == ("ambiguous", {})


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


@pytest.mark.parametrize("typo", ["Констанин", "Константни", "Константтин", "Константил"])
def test_small_name_typo(typo):
    sheet = Sheet([["ФИО", "С1"], [f"Иванов {typo}", "5"]])
    assert sheet.lookup("Константин Иванов") == ("found", {"С1": "5"})


def test_exact_name_wins_and_fuzzy_ambiguity_is_preserved():
    sheet = Sheet([["ФИО", "С1"], ["Иванов Констанин", "5"], ["Иванов Константил", "3"]])
    assert sheet.lookup("Иванов Константин") == ("ambiguous", {})
    sheet.rows.append(["Иванов Константин", "4"])
    assert sheet.lookup("Иванов Константин") == ("found", {"С1": "4"})
    assert not matches("Иванов Константин", "Иваноф Констанин")


def test_multilevel_paths_respect_parent_boundaries():
    rows = [
        ["№", "ФИО", "03.09.2026", "", "", "09.09.2026", "", ""],
        ["", "", "Семинар", "", "ДЗ-1", "Семинар", "", "ДЗ-2"],
        ["", "", "Отсутствие", "Выход к доске", "Число задач", "Отсутствие", "Выход к доске", "Число задач"],
        ["1", "Боженко Станислав Павлович", "0", "2", "3", "0", "4", "5"],
    ]
    sheet = Sheet(rows, 3, (2,), 1, (3, 4, 5, 6, 7, 8))
    status, grades = sheet.lookup("Боженко Станислав Павлович")
    assert status == "found"
    assert grades["03.09.2026 → Семинар → Выход к доске"] == "2"
    assert grades["09.09.2026 → ДЗ-2 → Число задач"] == "5"
    assert len(grades) == 6


def test_only_totals_with_duplicate_headers_and_comma_numbers():
    sheet = Sheet([
        ["ФИО", "Квиз 1", "ИТОГ", "Квиз 2", "ИТОГ"],
        ["", "№1", "№2", "№1", ""],
        ["Боженко Станислав Павлович", "0", "0,0", "1", "2,5"],
    ], 2, (1,), 1, (3, 5))
    assert sheet.lookup("Боженко Станислав Павлович")[1] == {"ИТОГ → №2": "0,0", "ИТОГ": "2,5"}


def test_duplicate_paths_have_distinct_column_keys():
    sheet = Sheet([["ФИО", "ИТОГ", "ИТОГ"], ["", "", ""], ["Иванов Иван", "4", "5"]],
                  2, (1,), 1, (2, 3))
    assert sheet.lookup("Иванов Иван")[1] == {"ИТОГ [столбец B]": "4", "ИТОГ [столбец C]": "5"}


@pytest.mark.parametrize("text, expected", [("C,E:G", (3, 5, 6, 7)), ("3,5-7", (3, 5, 6, 7)),
                                          ("AA:AB", (27, 28)), ("C,C", (3,))])
def test_column_selection_ranges(text, expected):
    from gradebot.sheets import parse_columns
    assert parse_columns(text, 30) == expected


@pytest.mark.parametrize("text", ["A:ZZZ", "0", "D:B", "C::D", "", "A1"])
def test_invalid_column_selection(text):
    from gradebot.sheets import parse_columns
    with pytest.raises(SheetError):
        parse_columns(text, 10)


def test_tab_discovery_html_entities_and_nonzero_gids():
    from gradebot.sheets import TabsParser
    parser = TabsParser()
    parser.feed('<ul><li id="sheet-button-432"><a>Матан &amp; семинары</a></li>'
                '<li id="sheet-button-91"><span>Итоги</span></li></ul>')
    assert parser.tabs == [("432", "Матан & семинары"), ("91", "Итоги")]


def test_tab_discovery_modern_google_htmlview_script():
    from gradebot.sheets import TabsParser

    parser = TabsParser()
    parser.feed(r'''<script>
    var items = [];
    items.push({name: "Семинары 1 модуль", pageUrl: "https:\/\/docs.google.com\/htmlview\/sheet?headers\x3dtrue&gid=0", gid: "0",initialSheet: ("0" == gid)});
    items.push({name: "Семинары 2 модуль", pageUrl: "https:\/\/docs.google.com\/htmlview\/sheet?gid=792792975", gid: "792792975",initialSheet: false});
    items.push({name: "КР 1", pageUrl: "https:\/\/docs.google.com\/htmlview\/sheet?gid=1371992815", gid: "1371992815",initialSheet: false});
    items.push({name: "Экзамен 1", pageUrl: "https:\/\/docs.google.com\/htmlview\/sheet?gid=1343735077", gid: "1343735077",initialSheet: false});
    items.push({name: "Итоги I семестр", pageUrl: "https:\/\/docs.google.com\/htmlview\/sheet?gid=1012826541", gid: "1012826541",initialSheet: false});
    </script>''')
    assert parser.tabs == [("0", "Семинары 1 модуль"), ("792792975", "Семинары 2 модуль"),
                           ("1371992815", "КР 1"), ("1343735077", "Экзамен 1"),
                           ("1012826541", "Итоги I семестр")]


def test_tab_discovery_decodes_js_name_escapes_without_executing_script():
    from gradebot.sheets import TabsParser

    parser = TabsParser()
    parser.feed(r'''<script>items.push({name: "Квиз \x22А\x22 \u0026 Б", pageUrl: "url", gid: "42"});
    throw new Error("do not execute");</script>''')
    assert parser.tabs == [("42", 'Квиз "А" & Б')]


async def test_discover_fetches_modern_menu_and_preserves_sheet_gids():
    from unittest.mock import MagicMock

    from gradebot.sheets import discover_sheets

    class Response:
        status = 200
        content = None

        def __init__(self):
            self.content = self

        async def iter_chunked(self, size):
            yield b'<script>items.push({name: "First", pageUrl: "url", gid: "42"});'
            yield b'items.push({name: "Second", pageUrl: "url", gid: "99"});</script>'

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    session = MagicMock()
    session.get.return_value = Response()
    tabs = await discover_sheets(session, "https://docs.google.com/spreadsheets/d/id/edit?usp=sharing")
    assert tabs == [{"name": "First", "url": "https://docs.google.com/spreadsheets/d/id/edit#gid=42"},
                    {"name": "Second", "url": "https://docs.google.com/spreadsheets/d/id/edit#gid=99"}]
    assert session.get.call_args.args[0] == "https://docs.google.com/spreadsheets/d/id/htmlview"


def test_auto_layout_with_merged_headers_and_student_number():
    from gradebot.sheets import detect_layout

    rows = [["№", "ФИО", "03.09.2026", "", "Итоги"],
            ["", "", "Семинар", "", "Текущая оценка"],
            ["", "", "Отсутствие", "Выход к доске", ""],
            ["1", "Боженко Станислав Павлович", "", "", "0,0"]]
    assert detect_layout(rows) == {"header_start": 1, "header": 3, "columns": (2,),
                                  "grade_columns": (3, 4, 5)}


def test_auto_layout_with_title_and_split_names():
    from gradebot.sheets import detect_layout

    rows = [["Матан"], ["Фамилия", "Имя", "Отчество", "Квиз"],
            ["Иванов", "Иван", "Иванович", "5"]]
    assert detect_layout(rows) == {"header_start": 2, "header": 2, "columns": (1, 2, 3),
                                  "grade_columns": (4,)}


def test_auto_layout_does_not_guess_without_name_header():
    from gradebot.sheets import detect_layout

    with pytest.raises(SheetError):
        detect_layout([["Неизвестная структура", "Оценка"], ["Иванов Иван", "5"]])

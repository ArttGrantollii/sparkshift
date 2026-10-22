"""End-to-end tests of the playground in a real browser (headless Chromium).

They build the site, serve it locally, and drive it as a user would. Pyodide
itself comes from its CDN, so these tests need network access. Run them with
``uv run pytest -m browser`` after ``uv run playwright install chromium``.
"""

import functools
import http.server
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

import sparkshift
from sparkshift.notebook import notebook_cells

pytestmark = pytest.mark.browser

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from build_playground import build  # noqa: E402

playwright_api = pytest.importorskip("playwright.sync_api")

# Pyodide's first load downloads about 10 MB.
READY_TIMEOUT_MS = 180_000
# Python is ready and nothing is converting (Convert also needs input).
IDLE = "#statusbar[data-python='ready'][data-working='false']"
ALLOWED_ORIGINS = {"127.0.0.1", "cdn.jsdelivr.net"}


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture(scope="module")
def site_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    site = tmp_path_factory.mktemp("site")
    build(site)
    handler = functools.partial(_QuietHandler, directory=str(site))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    with playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        yield browser
        browser.close()


def open_page(browser: Any, url: str, **context_options: Any) -> Any:
    """A page in a fresh browser context, loaded and ready."""
    context = browser.new_context(**context_options)
    page = context.new_page()
    page.goto(url)
    page.wait_for_selector(IDLE, timeout=READY_TIMEOUT_MS)
    return page


@pytest.fixture(scope="module")
def session(browser: Any, site_url: str) -> Iterator[dict]:
    """One page, loaded once, shared by most tests (loading Pyodide is slow).
    Records every origin the page contacts and every console error."""
    context = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
    page = context.new_page()
    origins: set[str] = set()
    errors: list[str] = []
    context.on("request", lambda request: origins.add(urlsplit(request.url).hostname))
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on(
        "console",
        lambda message: (
            errors.append(message.text) if message.type == "error" else None
        ),
    )
    page.goto(site_url)
    page.wait_for_selector(IDLE, timeout=READY_TIMEOUT_MS)
    yield {"page": page, "origins": origins, "errors": errors}
    context.close()


def convert_in_page(page: Any, sql: str, dialect: str) -> None:
    page.select_option("#dialect", dialect)
    page.fill("#sql", sql)
    page.click("#convert")
    page.wait_for_selector(IDLE, timeout=READY_TIMEOUT_MS)


# --- Converting ---------------------------------------------------------------


def test_page_is_ready_with_every_dialect(session: dict) -> None:
    page = session["page"]

    assert page.text_content("#status").startswith(
        f"Ready. SparkShift {sparkshift.__version__}, running on Pyodide "
    )
    options = page.locator("#dialect option")
    dialects = [options.nth(n).get_attribute("value") for n in range(options.count())]
    assert dialects == [
        "",
        "tsql",
        "postgres",
        "mysql",
        "snowflake",
        "bigquery",
        "oracle",
        "sas",
    ]


def test_the_page_starts_empty_and_waits_for_input(session: dict) -> None:
    page = session["page"]

    # Nothing is converted until the visitor gives the page something.
    assert page.input_value("#sql") == ""
    assert page.get_attribute("#sql", "placeholder") == (
        "Paste your code here, or open a file."
    )
    assert page.locator("#sql-view .ln").count() == 0
    assert page.is_disabled("#share")
    assert page.is_hidden("#output")
    assert page.text_content("#output-empty") == (
        "Your PySpark appears here after you press Convert."
    )
    assert page.is_disabled("#convert")
    status = page.text_content("#status")
    page.press("#sql", "Control+Enter")
    page.wait_for_timeout(300)
    assert page.is_hidden("#output")
    assert page.is_hidden("#errors")
    assert page.text_content("#status") == status
    page.fill("#sql", "SELECT a FROM t")
    assert page.is_enabled("#convert")
    assert page.is_enabled("#share")
    page.fill("#sql", "   \n")
    assert page.is_disabled("#convert")
    assert page.is_disabled("#share")


def test_choosing_notebooks_converts_nothing_that_was_not_converted(
    session: dict,
) -> None:
    page = session["page"]
    page.fill("#sql", "SELECT a FROM t")

    for kind in ("#output-notebook", "#output-code"):
        page.click(kind)
        # Wait as long as a conversion would take, had one started.
        page.wait_for_timeout(500)
        page.wait_for_selector(IDLE, timeout=READY_TIMEOUT_MS)
        assert page.is_hidden("#output"), kind
        assert page.is_hidden("#output-cells"), kind
        assert page.is_enabled("#convert")


# --- Opening files, and the source language ---------------------------------------

SAS_PROGRAM = "data big;\n    set orders;\n    where amount > 100;\nrun;\n"


def test_an_opened_sas_file_fills_the_editor_as_sas(
    session: dict, tmp_path: Path
) -> None:
    page = session["page"]
    path = tmp_path / "flag_big.sas"
    path.write_bytes(SAS_PROGRAM.replace("\n", "\r\n").encode())

    page.set_input_files("#pick-source", str(path))
    page.wait_for_selector("#status:has-text('Opened flag_big.sas.')")

    # Read as the command line reads it, and not converted yet.
    assert page.input_value("#sql") == SAS_PROGRAM
    assert page.input_value("#dialect") == "sas"
    assert page.is_hidden("#output")
    assert page.text_content("#language") == "SAS program"
    page.click("#convert")
    page.wait_for_selector(IDLE, timeout=READY_TIMEOUT_MS)
    assert page.text_content("#code") == (
        sparkshift.convert(SAS_PROGRAM, dialect="sas").code
    )


@pytest.mark.parametrize(
    ("chosen", "expected"), [("sas", ""), ("tsql", "tsql"), ("", "")]
)
def test_an_opened_sql_file_keeps_the_chosen_sql_dialect(
    session: dict, tmp_path: Path, chosen: str, expected: str
) -> None:
    page = session["page"]
    page.select_option("#dialect", chosen)
    path = tmp_path / "top.sql"
    path.write_text("SELECT a FROM t\n", encoding="utf-8")

    page.set_input_files("#pick-source", str(path))
    page.wait_for_selector("#status:has-text('Opened top.sql.')")

    assert page.input_value("#dialect") == expected
    assert page.input_value("#sql") == "SELECT a FROM t\n"


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("SELECT 'café' AS c FROM t".encode("latin-1"), "it is not UTF-8 text"),
        (b"-- " + b"x" * 100_001, "is larger than the browser demo"),
    ],
)
def test_a_file_that_cannot_be_opened_is_explained(
    session: dict, tmp_path: Path, content: bytes, message: str
) -> None:
    page = session["page"]
    page.fill("#sql", "SELECT kept FROM t")
    path = tmp_path / "bad.sql"
    path.write_bytes(content)

    page.set_input_files("#pick-source", str(path))
    page.wait_for_selector(f"#status:has-text('{message}')")

    assert page.get_attribute("#statusbar", "data-state") == "bad"
    assert page.input_value("#sql") == "SELECT kept FROM t"


def test_a_sas_program_under_sql_offers_to_switch(session: dict) -> None:
    page = session["page"]
    page.select_option("#dialect", "postgres")

    page.fill("#sql", SAS_PROGRAM)

    assert page.is_visible("#language-hint")
    assert page.text_content("#language-hint-text") == (
        "This looks like a SAS program, not SQL."
    )
    # It only suggests: the dialect stays until the visitor switches.
    assert page.input_value("#dialect") == "postgres"
    page.click("#language-switch")
    assert page.input_value("#dialect") == "sas"
    assert page.is_hidden("#language-hint")


def test_sql_under_sas_offers_to_switch_but_proc_sql_does_not(session: dict) -> None:
    page = session["page"]
    page.select_option("#dialect", "sas")

    page.fill("#sql", "SELECT name FROM customers")
    assert page.text_content("#language-switch") == "Switch to Generic SQL"
    page.fill(
        "#sql", "proc sql;\n  create table x as select name from customers;\nquit;"
    )
    assert page.is_hidden("#language-hint")
    page.fill("#sql", "SELECT name FROM customers")
    page.click("#language-switch")
    assert page.input_value("#dialect") == ""


@pytest.mark.parametrize(
    ("source", "sas", "sql"),
    [
        ("data a; set b; run;", True, False),
        ("DATA work.a (keep=x y) b;\n set c;\nrun;", True, False),
        ("proc sort data=a out=b; by x; run;", True, False),
        ("* a comment;\nproc means data=a; run;", True, False),
        ("SELECT a FROM t", False, True),
        ("  with x as (select 1 as a from t) select a from x", False, True),
        ("-- data a; run;\nSELECT a FROM t", False, True),
        ("SELECT 'data a; run;' AS text FROM t", False, True),
        (
            "WITH totals AS (SELECT 1 AS n FROM t),\ndata AS (SELECT n FROM totals)\n"
            "SELECT n FROM data;",
            False,
            True,
        ),
        ("UPDATE t SET a = 1", False, False),
        ("", False, False),
    ],
)
def test_sas_and_sql_are_told_apart(
    session: dict, source: str, sas: bool, sql: bool
) -> None:
    page = session["page"]

    found = page.evaluate(
        """async (source) => {
            const { looksLikeSas, looksLikeSql } = await import("./language.js");
            return [looksLikeSas(source), looksLikeSql(source)];
        }""",
        source,
    )

    assert found == [sas, sql]


def test_every_example_matches_the_command_line(session: dict) -> None:
    page = session["page"]
    examples = [
        *(ROOT / "examples" / "sql").rglob("*.sql"),
        *(ROOT / "examples" / "sas").glob("*.sas"),
    ]
    labels = page.locator("#example option").all_text_contents()[1:]
    assert len(labels) == len(examples)
    assert sum(label.startswith("SAS: ") for label in labels) == len(
        list((ROOT / "examples" / "sas").glob("*.sas"))
    )

    for index in range(len(examples)):
        page.select_option("#example", str(index))
        # An example fills the editor and waits for Convert.
        assert page.is_hidden("#output"), labels[index]
        assert page.text_content("#status").startswith(
            f"Loaded the example “{labels[index]}”. Press Convert"
        )
        page.click("#convert")
        page.wait_for_selector(IDLE, timeout=READY_TIMEOUT_MS)
        dialect = page.input_value("#dialect")
        sql = page.input_value("#sql")

        expected = sparkshift.convert(sql, dialect=dialect or None).code
        assert page.text_content("#code") == expected, labels[index]
        assert page.is_hidden("#errors")


def test_output_is_highlighted_without_changing_the_code(session: dict) -> None:
    page = session["page"]
    sql = "SELECT name, amount * 2 AS doubled FROM orders WHERE status = 'paid'"

    convert_in_page(page, sql, "")

    assert page.text_content("#code") == sparkshift.convert(sql).code
    for token_class in ("string", "keyword", "function", "number"):
        assert page.locator(f"#code span.{token_class}").count() > 0, token_class


def test_unsupported_sql_lists_the_same_issues_as_the_command_line(
    session: dict,
) -> None:
    page = session["page"]
    sql = "SELECT my_udf(a) AS x, CAST(b AS FLOAT) AS y FROM t"
    with pytest.raises(sparkshift.UnsupportedSQLError) as caught:
        sparkshift.convert(sql)

    convert_in_page(page, sql, "")

    assert page.is_visible("#errors")
    assert page.text_content("#errors p") == "2 unsupported constructs:"
    items = page.locator("#errors li").all_text_contents()
    for item, issue in zip(items, caught.value.issues, strict=True):
        assert item == f"{issue.message}: {issue.sql}{issue.hint or ''}"
    assert page.is_hidden("#output")
    assert page.is_disabled("#copy")


def test_modified_star_is_rejected_in_browser(session: dict) -> None:
    page = session["page"]

    convert_in_page(page, "SELECT * EXCEPT (secret) FROM audit_input", "")

    assert page.is_visible("#errors")
    assert "SELECT * with EXCEPT" in page.locator("#errors").inner_text()
    assert (
        "List the selected columns explicitly" in page.locator("#errors").inner_text()
    )
    assert page.is_hidden("#output")
    assert page.is_disabled("#copy")


def test_parse_error_shows_its_position(session: dict) -> None:
    page = session["page"]

    convert_in_page(page, "SELECT (a FROM t", "")

    assert "line 1, column" in page.text_content("#errors")


def test_dialect_changes_the_translation(session: dict) -> None:
    page = session["page"]
    sql = "SELECT TOP 2 name FROM customers ORDER BY name"

    convert_in_page(page, sql, "tsql")

    assert page.text_content("#code") == sparkshift.convert(sql, dialect="tsql").code
    assert page.is_visible("#output")


def test_a_sas_program(session: dict) -> None:
    page = session["page"]
    program = "data big; set orders; where amount > 100; run;"

    convert_in_page(page, program, "sas")

    assert page.text_content("#code") == sparkshift.convert(program, dialect="sas").code
    assert page.text_content("#language") == "SAS program"
    page.select_option("#dialect", "")
    assert page.text_content("#language") == "SQL"


def test_helper_names_are_bound_in_generated_code_in_browser(session: dict) -> None:
    page = session["page"]
    sql = "SELECT * FROM orders QUALIFY ROW_NUMBER() OVER (ORDER BY order_id)=1"

    convert_in_page(page, sql, "")

    assert page.is_hidden("#errors")
    assert page.text_content("#code") == sparkshift.convert(sql).code
    assert ".columns" in page.text_content("#code")
    assert "while helper_column_key(_qualify_1_name)" in page.text_content("#code")


def test_ctrl_enter_converts(session: dict) -> None:
    page = session["page"]
    page.select_option("#dialect", "")
    page.fill("#sql", "SELECT name FROM customers")

    page.press("#sql", "Control+Enter")
    page.wait_for_selector(IDLE, timeout=READY_TIMEOUT_MS)

    assert (
        page.text_content("#code")
        == sparkshift.convert("SELECT name FROM customers").code
    )


def test_notebook_output_is_what_the_command_line_writes(session: dict) -> None:
    page = session["page"]
    sql = "SELECT TOP 3 name FROM customers ORDER BY name"
    convert_in_page(page, sql, "tsql")

    page.click("#output-notebook")
    page.wait_for_selector("#output-cells:visible", timeout=READY_TIMEOUT_MS)
    page.wait_for_selector(IDLE, timeout=READY_TIMEOUT_MS)

    result = sparkshift.convert(sql, dialect="tsql")
    # Shown as cells; the file stays what Copy and Download take.
    assert page.is_hidden("#output")
    assert page.text_content("#code") == sparkshift.to_notebook(result, dialect="tsql")
    assert page.text_content("#output-label") == "Notebook"
    assert page.get_attribute("#output-notebook", "aria-checked") == "true"

    page.click("#output-code")
    page.wait_for_selector("#output:visible", timeout=READY_TIMEOUT_MS)
    assert page.is_hidden("#output-cells")
    page.wait_for_selector(IDLE, timeout=READY_TIMEOUT_MS)
    assert page.text_content("#code") == result.code
    assert page.text_content("#output-label") == "PySpark"


NOTEBOOK_PROGRAM = (
    "data recent; set orders(keep=order_id amount); where amount > 10; run; "
    "proc sort data=recent out=dedup nodupkey; by order_id; run;"
)


@pytest.fixture
def notebook_page(session: dict) -> Iterator[Any]:
    """The shared page, back to plain code afterwards."""
    page = session["page"]
    yield page
    if page.get_attribute("#output-code", "aria-checked") != "true":
        page.click("#output-code")
        page.wait_for_selector(IDLE, timeout=READY_TIMEOUT_MS)


def show_notebook(page: Any, sql: str, dialect: str) -> list:
    """Convert ``sql`` as a notebook in the file view; return its cells."""
    convert_in_page(page, sql, dialect)
    if page.get_attribute("#output-notebook", "aria-checked") != "true":
        page.click("#output-notebook")
    page.wait_for_selector("#output-cells:visible", timeout=READY_TIMEOUT_MS)
    page.wait_for_selector(IDLE, timeout=READY_TIMEOUT_MS)
    return notebook_cells(
        sparkshift.convert(sql, dialect=dialect or None), dialect=dialect or None
    )


def test_a_notebook_is_shown_as_its_cells(notebook_page: Any) -> None:
    page = notebook_page
    cells = show_notebook(page, NOTEBOOK_PROGRAM, "sas")

    shown = page.locator("#output-cells > .cell")
    assert shown.count() == len(cells)
    for position, cell in enumerate(cells):
        element = shown.nth(position)
        if cell.kind == "markdown":
            assert element.evaluate("node => node.tagName") == "SECTION"
            assert element.locator("h3").text_content() == "Converted code"
            assert "# MAGIC" not in element.text_content()
        elif cell.kind == "setup":
            # Folded, with what it does, until the visitor opens it.
            assert element.evaluate("node => node.tagName") == "DETAILS"
            assert element.get_attribute("open") is None
            assert element.locator(".cell-title").text_content() == cell.title
            lines = cell.source.count("\n") + 1
            assert (
                element.locator(".cell-meta").text_content() == f"Setup · {lines} lines"
            )
            assert element.locator("pre").is_hidden()
            assert element.locator("pre").text_content() == cell.source
        else:
            assert element.locator(".cell-head").text_content() == (
                f"Cell {position + 1}Python"
            )
            assert element.locator("pre").text_content() == cell.source
            assert element.locator("pre .ln").count() == cell.source.count("\n") + 1
    notebook = sparkshift.to_notebook(
        sparkshift.convert(NOTEBOOK_PROGRAM, dialect="sas"), dialect="sas"
    )
    assert page.text_content("#output-lines") == (
        f"{len(cells)} cells · {notebook.count(chr(10))} lines"
    )


def test_a_setup_cell_opens_to_show_its_code(notebook_page: Any) -> None:
    page = notebook_page
    show_notebook(page, NOTEBOOK_PROGRAM, "sas")
    setup = page.locator("#output-cells > details.setup").first

    setup.locator("summary").click()

    assert setup.get_attribute("open") == ""
    assert setup.locator("pre").is_visible()
    setup.locator("summary").press("Enter")
    assert setup.get_attribute("open") is None


def test_notebook_markdown_is_formatted(notebook_page: Any) -> None:
    page = notebook_page
    show_notebook(page, WARNING_PROGRAM, "sas")
    markdown = page.locator("#output-cells > .cell.markdown")

    warnings = sparkshift.convert(WARNING_PROGRAM, dialect="sas").warnings
    assert markdown.locator("strong").text_content() == "Review before running:"
    assert markdown.locator("li").count() == len(warnings)
    assert markdown.locator("li code").first.text_content() == warnings[0].sql
    codes = markdown.locator("p code").all_text_contents()
    assert {"catalog", "schema", "output_schema", "dedup"} <= set(codes)


def test_copy_and_download_keep_the_notebook_file(notebook_page: Any) -> None:
    page = notebook_page
    show_notebook(page, NOTEBOOK_PROGRAM, "sas")
    notebook = sparkshift.to_notebook(
        sparkshift.convert(NOTEBOOK_PROGRAM, dialect="sas"), dialect="sas"
    )

    page.click("#copy")
    page.wait_for_selector("#status:has-text('Copied the notebook.')")
    assert page.evaluate("navigator.clipboard.readText()") == notebook
    with page.expect_download() as download_info:
        page.click("#download")
    download = download_info.value
    assert download.suggested_filename == "converted_notebook.py"
    assert Path(download.path()).read_text(encoding="utf-8") == notebook


def test_a_failed_notebook_clears_its_cells(notebook_page: Any) -> None:
    page = notebook_page
    show_notebook(page, NOTEBOOK_PROGRAM, "sas")

    convert_in_page(page, "data a; set b; retain x; run;", "sas")

    assert page.is_visible("#errors")
    assert page.is_hidden("#output-cells")
    assert page.locator("#output-cells > .cell").count() == 0
    assert page.is_disabled("#copy")


def test_pipeline_output_is_what_the_command_line_writes(notebook_page: Any) -> None:
    page = notebook_page
    convert_in_page(page, NOTEBOOK_PROGRAM, "sas")

    page.click("#output-pipeline")
    page.wait_for_selector(IDLE, timeout=READY_TIMEOUT_MS)

    result = sparkshift.convert(NOTEBOOK_PROGRAM, dialect="sas")
    pipeline = sparkshift.to_pipeline(result, dialect="sas")
    assert page.is_visible("#output")
    assert page.is_hidden("#output-cells")
    assert page.text_content("#code") == pipeline
    assert page.text_content("#output-label") == "Pipeline"
    assert page.get_attribute("#output-pipeline", "aria-checked") == "true"
    assert page.get_attribute("#output-notebook", "aria-checked") == "false"
    with page.expect_download() as download_info:
        page.click("#download")
    download = download_info.value
    assert download.suggested_filename == "converted_pipeline.py"
    assert Path(download.path()).read_text(encoding="utf-8") == pipeline


def test_a_program_that_cannot_be_a_pipeline_says_why(notebook_page: Any) -> None:
    page = notebook_page
    page.click("#output-pipeline")

    convert_in_page(page, "proc print data=orders; run;", "sas")

    assert page.is_visible("#errors")
    assert "a pipeline of a program that creates no data set" in page.text_content(
        "#errors"
    )
    assert page.is_hidden("#output")


def test_input_highlighting_shows_exactly_the_source(session: dict) -> None:
    page = session["page"]
    sql = "SELECT name, 'x' AS y\nFROM customers -- a comment\nWHERE id = 3\n"

    page.fill("#sql", sql)

    assert page.text_content("#sql-view") == sql
    # One numbered line per line of source, including the empty last line.
    assert page.locator("#sql-view .ln").count() == 4
    for token_class in ("keyword", "string", "number"):
        assert page.locator(f"#sql-view span.{token_class}").count() > 0, token_class


def test_line_numbers_are_not_part_of_the_code(session: dict) -> None:
    page = session["page"]
    sql = "SELECT name FROM customers"
    convert_in_page(page, sql, "")

    code = sparkshift.convert(sql).code
    assert page.locator("#code .ln").count() == code.count("\n")
    assert page.text_content("#code") == code
    assert page.text_content("#output-lines") == f"{code.count(chr(10))} lines"


def test_download_saves_exactly_the_code(session: dict) -> None:
    page = session["page"]
    sql = "SELECT name FROM customers WHERE country = 'US'"
    convert_in_page(page, sql, "")

    with page.expect_download() as download_info:
        page.click("#download")
    download = download_info.value

    assert download.suggested_filename == "converted.py"
    assert Path(download.path()).read_text(encoding="utf-8") == (
        sparkshift.convert(sql).code
    )
    assert page.text_content("#status") == "Downloaded converted.py."


def test_theme_choice_is_applied_and_remembered(browser: Any, site_url: str) -> None:
    page = open_page(browser, site_url, color_scheme="dark")
    assert page.get_attribute("#theme", "aria-label") == "Switch to light theme"
    dark_background = page.evaluate("getComputedStyle(document.body).backgroundColor")

    page.click("#theme")

    assert page.get_attribute("html", "data-theme") == "light"
    assert page.get_attribute("#theme", "aria-label") == "Switch to dark theme"
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") != (
        dark_background
    )
    page.reload()
    page.wait_for_selector(IDLE, timeout=READY_TIMEOUT_MS)
    assert page.get_attribute("html", "data-theme") == "light"
    page.context.close()


def test_fonts_are_served_by_the_site(session: dict) -> None:
    page = session["page"]

    loaded = page.evaluate(
        """async () => {
            await document.fonts.ready;
            return [...document.fonts]
                .filter((font) => font.status === "loaded")
                .map((font) => font.family.replaceAll('"', ""));
        }"""
    )

    assert "IBM Plex Sans" in loaded
    assert "JetBrains Mono" in loaded
    # Every request the page made, fonts included, stayed on allowed origins.
    assert session["origins"] <= ALLOWED_ORIGINS


# --- Copying and sharing -------------------------------------------------------


def test_copy_puts_the_code_on_the_clipboard(session: dict) -> None:
    page = session["page"]
    convert_in_page(page, "SELECT name FROM customers", "")

    page.click("#copy")

    # Copying is asynchronous: wait for the page to confirm it.
    page.wait_for_selector("#status:has-text('Copied the PySpark code.')")
    clipboard = page.evaluate("navigator.clipboard.readText()")
    assert clipboard == sparkshift.convert("SELECT name FROM customers").code


def test_shared_link_restores_the_query(session: dict, browser: Any) -> None:
    page = session["page"]
    sql = "SELECT TOP 5 name, score FROM customers ORDER BY score DESC"
    convert_in_page(page, sql, "tsql")

    page.click("#share")

    page.wait_for_selector("#status:has-text('Copied a link to this query.')")
    link = page.evaluate("navigator.clipboard.readText()")
    assert page.url == link
    # The query is after the "#", which browsers never send to the server.
    assert urlsplit(link).query == ""

    shared = open_page(browser, link)
    assert shared.input_value("#sql") == sql
    assert shared.input_value("#dialect") == "tsql"
    # A shared query waits for Convert, like any other.
    assert shared.is_hidden("#output")
    assert shared.text_content("#status").startswith("Loaded a shared query.")
    shared.click("#convert")
    shared.wait_for_selector(IDLE, timeout=READY_TIMEOUT_MS)
    assert shared.text_content("#code") == sparkshift.convert(sql, dialect="tsql").code
    shared.context.close()


# --- Folders ---------------------------------------------------------------------

COVERAGE = ROOT / "examples" / "coverage"


def reset_file_view(page: Any) -> None:
    """Generic SQL and plain code, in the file view, with nothing converting."""
    page.click("#tab-file")
    page.wait_for_selector(IDLE, timeout=READY_TIMEOUT_MS)
    if page.input_value("#dialect") != "":
        page.select_option("#dialect", "")
    if page.get_attribute("#output-code", "aria-checked") != "true":
        page.click("#output-code")
        page.wait_for_selector(IDLE, timeout=READY_TIMEOUT_MS)


@pytest.fixture
def folder_view(session: dict) -> Iterator[Any]:
    """The shared page in its folder view, for generic SQL and plain code;
    back in the file view afterwards."""
    page = session["page"]
    reset_file_view(page)
    page.click("#tab-folder")
    yield page
    reset_file_view(page)


def choose(page: Any, files: Any, picker: str = "#pick-folder") -> None:
    """Choose a folder (or files, with picker #pick-files) and wait until
    every file has converted."""
    page.set_input_files(picker, files)
    page.wait_for_selector(IDLE, timeout=READY_TIMEOUT_MS)


def command_line_outputs(folder: Path, out: Path, *arguments: str) -> dict[str, bytes]:
    """What ``sparkshift convert FOLDER -o OUT`` writes, by relative path."""
    from sparkshift.cli import main

    main(["convert", str(folder), "-o", str(out), *arguments])
    return {
        path.relative_to(out).as_posix(): path.read_bytes()
        for path in out.rglob("*")
        if path.is_file()
    }


def downloaded_zip(page: Any) -> tuple[str, dict[str, bytes]]:
    import zipfile

    with page.expect_download() as download_info:
        page.click("#download-zip")
    download = download_info.value
    with zipfile.ZipFile(download.path()) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    return download.suggested_filename, files


def file_button(page: Any, name: str) -> Any:
    return page.locator("#files button", has=page.locator(".name", has_text=name))


def test_a_folder_converts_as_the_command_line_does(
    folder_view: Any, tmp_path: Path
) -> None:
    page = folder_view

    choose(page, str(COVERAGE))

    expected = command_line_outputs(COVERAGE, tmp_path)
    sources = sorted(COVERAGE.rglob("*.sql"))
    failed = len(sources) - len(expected)
    assert failed > 0, "the folder should include a file that does not convert"
    warned = sum(
        bool(sparkshift.convert(path.read_text(encoding="utf-8")).warnings)
        for path in sources
        if path.relative_to(COVERAGE).with_suffix(".py").as_posix() in expected
    )
    status = (
        f"Converted {len(expected)} of {len(sources)} files; "
        f"{failed} could not be converted."
    )
    if warned:
        status += f" {warned} file{'s' if warned > 1 else ''} with warnings: "
        status += "review before running."
    assert page.text_content("#status") == status
    assert page.text_content("#folder-count") == (
        f"{len(expected)} of {len(sources)} files converted"
    )
    name, files = downloaded_zip(page)
    assert name == "coverage_pyspark.zip"
    assert files == {
        f"coverage_pyspark/{path}": code for path, code in expected.items()
    }
    assert page.text_content("#status") == "Downloaded coverage_pyspark.zip."


def test_each_file_shows_its_source_code_and_issues(folder_view: Any) -> None:
    page = folder_view
    choose(page, str(COVERAGE))

    for path in sorted(COVERAGE.rglob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        file_button(page, path.name).click()
        assert page.text_content("#file-source-name") == (
            path.relative_to(COVERAGE).as_posix()
        )
        assert page.text_content("#file-source") == sql
        try:
            code = sparkshift.convert(sql).code
        except sparkshift.SparkShiftError as error:
            assert page.is_hidden("#file-output")
            assert page.is_disabled("#copy")
            assert page.is_visible("#file-errors")
            assert str(error).split("\n")[0] in page.text_content("#file-errors")
            assert file_button(page, path.name).text_content().endswith("Not converted")
        else:
            assert page.text_content("#file-code") == code
            assert page.is_hidden("#file-errors")
            assert page.is_enabled("#copy")


def test_a_folder_of_notebooks(folder_view: Any) -> None:
    page = folder_view
    choose(page, str(COVERAGE))

    page.click("#output-notebook")
    page.wait_for_selector(IDLE, timeout=READY_TIMEOUT_MS)

    name, files = downloaded_zip(page)
    assert name == "coverage_notebooks.zip"
    revenue = COVERAGE / "orders" / "daily_revenue.sql"
    expected = sparkshift.to_notebook(
        sparkshift.convert(revenue.read_text(encoding="utf-8")),
        source="daily_revenue.sql",
    )
    assert files["coverage_notebooks/orders/daily_revenue.py"] == expected.encode()

    # Each file's notebook is shown as its cells.
    file_button(page, "daily_revenue.sql").click()
    cells = notebook_cells(
        sparkshift.convert(revenue.read_text(encoding="utf-8")),
        source="daily_revenue.sql",
    )
    assert page.is_hidden("#file-output")
    assert page.locator("#file-cells > .cell").count() == len(cells)
    assert page.text_content("#file-code") == expected
    assert page.locator("#file-cells h3").text_content() == "daily_revenue"


PIPELINE_FOLDER = {
    "a/daily.sql": "SELECT order_id FROM orders",
    "b/daily.sql": "SELECT customer_id FROM customers",
    "load.sas": "data clean; set customers(keep=customer_id score); run;",
    "show.sas": "proc print data=clean; run;",
}


def test_a_folder_of_pipeline_files(folder_view: Any, tmp_path: Path) -> None:
    page = folder_view
    root = tmp_path / "jobs"
    for name, text in PIPELINE_FOLDER.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    page.click("#output-pipeline")
    # A folder chosen before converts again first.
    page.wait_for_selector(IDLE, timeout=READY_TIMEOUT_MS)

    choose(page, str(root))

    expected = command_line_outputs(root, tmp_path / "out", "--pipeline")
    assert list(expected) == ["load.py"]
    name, files = downloaded_zip(page)
    assert name == "jobs_pipeline.zip"
    assert files == {f"jobs_pipeline/{path}": code for path, code in expected.items()}
    assert page.text_content("#folder-count") == "1 of 4 files converted"
    # As on the command line: the files that define the same dataset, and the
    # program that creates none, are not converted.
    file_button(page, "daily.sql").first.click()
    assert "its dataset daily is also defined by b/daily.sql" in page.text_content(
        "#file-errors"
    )
    file_button(page, "show.sas").click()
    assert "a pipeline of a program that creates no data set" in page.text_content(
        "#file-errors"
    )


def test_files_chosen_one_by_one_use_the_dialect_except_sas(folder_view: Any) -> None:
    page = folder_view
    page.select_option("#dialect", "tsql")
    # Files chosen by an earlier test convert again in the new dialect.
    page.wait_for_selector(IDLE, timeout=READY_TIMEOUT_MS)
    query = ROOT / "examples" / "sql" / "tsql" / "top_customers.sql"
    program = ROOT / "examples" / "sas" / "status_counts.sas"

    choose(page, [str(query), str(program)], picker="#pick-files")

    _, files = downloaded_zip(page)
    assert files == {
        "converted_pyspark/status_counts.py": sparkshift.convert(
            program.read_text(encoding="utf-8"), dialect="sas"
        ).code.encode(),
        "converted_pyspark/top_customers.py": sparkshift.convert(
            query.read_text(encoding="utf-8"), dialect="tsql"
        ).code.encode(),
    }
    file_button(page, "status_counts.sas").click()
    assert page.text_content("#file-source-label") == "SAS program"


def test_changing_the_dialect_converts_the_folder_again(folder_view: Any) -> None:
    page = folder_view
    choose(page, str(COVERAGE))

    page.select_option("#dialect", "sas")
    page.wait_for_selector(IDLE, timeout=READY_TIMEOUT_MS)

    assert page.text_content("#folder-errors") == "There are no .sas files to convert."
    assert page.text_content("#status") == "Conversion failed."
    assert page.is_disabled("#download-zip")


def test_colliding_outputs_convert_nothing(folder_view: Any, tmp_path: Path) -> None:
    page = folder_view
    folder = tmp_path / "jobs"
    folder.mkdir()
    (folder / "report.sql").write_text("SELECT a FROM t\n", encoding="utf-8")
    (folder / "report.sas").write_text("data x; set t; run;\n", encoding="utf-8")

    choose(page, str(folder))

    errors = page.text_content("#folder-errors")
    assert "Nothing was converted" in errors
    assert "report.sas → report.py" in errors
    assert "report.sql → report.py" in errors
    assert page.locator("#files button").count() == 0
    assert page.is_disabled("#download-zip")


def test_files_are_read_as_the_command_line_reads_them(
    folder_view: Any, tmp_path: Path
) -> None:
    page = folder_view
    folder = tmp_path / "mixed"
    folder.mkdir()
    # Line ends inside literals reach the generated code; "\r" alone ends a
    # line too, for Python.
    (folder / "windows.sql").write_bytes(
        b"SELECT 'two\r\nlines' AS a,\r\n  'old\rmac' AS b\r\nFROM t\r\n"
    )
    (folder / "latin1.sql").write_bytes("SELECT 'café' AS a FROM t".encode("latin-1"))

    choose(page, str(folder))

    expected = command_line_outputs(folder, tmp_path / "cli")
    _, files = downloaded_zip(page)
    assert files == {f"mixed_pyspark/{path}": code for path, code in expected.items()}
    assert list(expected) == ["windows.py"]
    file_button(page, "latin1.sql").click()
    assert page.text_content("#file-errors") == (
        "Cannot read this file: it is not UTF-8 text."
    )


def test_dropped_files_are_converted(folder_view: Any) -> None:
    page = folder_view

    page.evaluate(
        """() => {
            const transfer = new DataTransfer();
            transfer.items.add(new File(["SELECT a FROM t"], "dropped.sql"));
            const drop = document.getElementById("drop");
            const event = { dataTransfer: transfer, bubbles: true };
            drop.dispatchEvent(new DragEvent("drop", event));
        }"""
    )
    page.wait_for_selector("#files button", timeout=READY_TIMEOUT_MS)
    page.wait_for_selector(IDLE, timeout=READY_TIMEOUT_MS)

    assert page.text_content("#file-code") == sparkshift.convert("SELECT a FROM t").code
    assert page.text_content("#status") == "Converted 1 of 1 file."


def test_dropped_folders_are_read_in_full(session: dict) -> None:
    page = session["page"]

    # A folder's entries arrive in batches; the walk must read them all.
    found = page.evaluate(
        """async () => {
            const { filesFromDrop } = await import("./files.js");
            const file = (path) => ({
                isFile: true,
                fullPath: path,
                file: (resolve) => resolve(new File([path], path.split("/").pop())),
            });
            const folder = (path, batches) => ({
                isDirectory: true,
                fullPath: path,
                createReader: () => ({
                    readEntries: (resolve) => resolve(batches.shift() ?? []),
                }),
            });
            const root = folder("/claims", [
                [
                    file("/claims/a.sas"),
                    folder("/claims/legacy", [[file("/claims/legacy/b.sas")]]),
                ],
                [file("/claims/c.sql")],
            ]);
            const found = await filesFromDrop({
                items: [{ webkitGetAsEntry: () => root }],
                files: [],
            });
            return found.map(({ path, file }) => [path, file.name]);
        }"""
    )

    assert found == [
        ["claims/a.sas", "a.sas"],
        ["claims/legacy/b.sas", "b.sas"],
        ["claims/c.sql", "c.sql"],
    ]


def test_views_keep_their_own_status_and_tabs_follow_the_keyboard(
    folder_view: Any,
) -> None:
    page = folder_view
    choose(page, str(COVERAGE))
    folder_status = page.text_content("#status")
    assert page.is_hidden("#view-file")
    assert page.is_hidden("#download")
    assert page.is_hidden("#share")
    assert page.is_visible("#download-zip")

    page.focus("#tab-folder")
    page.keyboard.press("ArrowLeft")

    assert page.get_attribute("#tab-file", "aria-selected") == "true"
    assert page.evaluate("document.activeElement.id") == "tab-file"
    assert page.is_visible("#view-file")
    assert page.is_hidden("#view-folder")
    assert page.is_hidden("#download-zip")
    assert page.text_content("#status") != folder_status
    page.keyboard.press("ArrowRight")
    assert page.is_visible("#view-folder")
    assert page.text_content("#status") == folder_status


# --- Deploying ---------------------------------------------------------------------

MOCK_ORIGIN = "https://dbc-mock.cloud.databricks.com"
MOCK_TOKEN = "dapi-mock-0123456789abcdef"
DEPLOY_FOLDER = "/Workspace/Users/me@example.com/claims"
DEPLOY_SOURCES = {
    "load.sql": "SELECT order_id, amount FROM orders WHERE amount > 100\n",
    "legacy/report.sas": "data big; set orders; where amount > 100; run;\n",
}


class FakeDatabricks:
    """A workspace's REST API, as a route: records each request and answers
    with the CORS headers Databricks sends; ``answers`` overrides a path."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.answers: dict[str, tuple[int, dict]] = {}

    def __call__(self, route: Any) -> None:
        cors = {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "authorization, content-type",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        }
        request = route.request
        if request.method == "OPTIONS":
            route.fulfill(status=204, headers=cors)
            return
        path = urlsplit(request.url).path
        self.requests.append(
            {
                "path": path,
                "authorization": request.headers.get("authorization"),
                "body": request.post_data_json,
            }
        )
        status, answer = self.answers.get(path, (200, {}))
        route.fulfill(status=status, headers=cors, json=answer)


@pytest.fixture(scope="module")
def deploy_page(
    browser: Any, site_url: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[Any, FakeDatabricks]]:
    """A page with a converted folder, in its Deploy view, and a fake
    workspace at MOCK_ORIGIN."""
    root = tmp_path_factory.mktemp("deploy") / "claims"
    for name, text in DEPLOY_SOURCES.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    workspace = FakeDatabricks()
    page = open_page(browser, site_url)
    page.context.route(f"{MOCK_ORIGIN}/**", workspace)
    page.click("#tab-folder")
    choose(page, str(root))
    page.click("#tab-deploy")
    yield page, workspace
    page.context.close()


def fill_deploy_form(page: Any, **values: str) -> None:
    fields = {
        "host": MOCK_ORIGIN,
        "token": MOCK_TOKEN,
        "folder": DEPLOY_FOLDER,
        "read": "workspace.raw",
        "write": "workspace.claims",
        "job": "",
    }
    fields.update(values)
    for name, value in fields.items():
        page.fill(f"#deploy-{name}", value)


def deploy_and_wait(page: Any) -> None:
    page.click("#deploy")
    page.wait_for_selector(
        "#statusbar:not([data-state='busy'])", timeout=READY_TIMEOUT_MS
    )


def imported(workspace: FakeDatabricks) -> dict[str, tuple[str, bool]]:
    import base64

    return {
        request["body"]["path"]: (
            base64.b64decode(request["body"]["content"]).decode("utf-8"),
            request["body"]["overwrite"],
        )
        for request in workspace.requests
        if request["path"] == "/api/2.0/workspace/import"
    }


def test_deploying_imports_every_notebook_and_the_create_job_notebook(
    deploy_page: tuple[Any, FakeDatabricks],
) -> None:
    from sparkshift.jobs import create_job_notebook, job_settings

    page, workspace = deploy_page
    workspace.requests.clear()
    workspace.answers.clear()
    fill_deploy_form(page)
    assert page.text_content("#deploy") == "Deploy 2 notebooks"

    deploy_and_wait(page)

    assert page.text_content("#status") == (
        f"Deployed 2 notebooks and Create job to {MOCK_ORIGIN}."
    )
    report = f"{DEPLOY_FOLDER}/legacy/report"
    load = f"{DEPLOY_FOLDER}/load"
    assert [(r["path"], r["body"]["path"]) for r in workspace.requests] == [
        ("/api/2.0/workspace/mkdirs", DEPLOY_FOLDER),
        ("/api/2.0/workspace/mkdirs", f"{DEPLOY_FOLDER}/legacy"),
        ("/api/2.0/workspace/import", report),
        ("/api/2.0/workspace/import", load),
        ("/api/2.0/workspace/import", f"{DEPLOY_FOLDER}/Create job"),
    ]
    # The token goes in the header of each request to the workspace, and
    # nowhere else.
    assert {r["authorization"] for r in workspace.requests} == {f"Bearer {MOCK_TOKEN}"}
    locations = {
        "catalog": "workspace",
        "schema": "raw",
        "output_catalog": "workspace",
        "output_schema": "claims",
    }
    expected = {
        report: sparkshift.to_notebook(
            sparkshift.convert(DEPLOY_SOURCES["legacy/report.sas"], dialect="sas"),
            source="report.sas",
            dialect="sas",
            **locations,
        ),
        load: sparkshift.to_notebook(
            sparkshift.convert(DEPLOY_SOURCES["load.sql"]),
            source="load.sql",
            **locations,
        ),
        f"{DEPLOY_FOLDER}/Create job": create_job_notebook(
            job_settings(
                "sparkshift claims", DEPLOY_FOLDER, [report, load], **locations
            )
        ),
    }
    assert imported(workspace) == {
        path: (source, False) for path, source in expected.items()
    }
    # After a deployment the token is forgotten, and it was never stored.
    assert page.input_value("#deploy-token") == ""
    stored = page.evaluate(
        "() => JSON.stringify([{...localStorage}, {...sessionStorage}, location.href])"
    )
    assert MOCK_TOKEN not in stored
    assert page.is_visible("#deploy-next")
    # Every step shows that it is done, in words for screen readers too.
    steps = page.locator("#deploy-plan li")
    assert steps.count() == 4
    for index in range(4):
        assert steps.nth(index).get_attribute("data-state") == "ok"
        assert steps.nth(index).text_content().endswith(" (done)")
    assert page.text_content("#deploy-job-name") == "sparkshift claims"
    assert page.get_attribute("#deploy-link", "href") == (
        f"{MOCK_ORIGIN}/#workspace{DEPLOY_FOLDER}/Create%20job"
    )


def test_deploying_a_pipeline_imports_its_files_and_the_create_pipeline_notebook(
    deploy_page: tuple[Any, FakeDatabricks],
) -> None:
    from sparkshift.pipelines import create_pipeline_notebook, pipeline_settings

    page, workspace = deploy_page
    workspace.requests.clear()
    workspace.answers.clear()
    page.check("#deploy-kind-pipeline")
    try:
        fill_deploy_form(page)
        assert page.text_content("#deploy") == "Deploy 2 files"
        assert page.text_content("#deploy-job-label") == "Pipeline name"
        steps = page.locator("#deploy-plan li .step-title").all_text_contents()
        assert steps == [
            "Check the workspace",
            "Import legacy/report.py",
            "Import load.py",
            "Import Create pipeline",
        ]

        deploy_and_wait(page)

        assert page.text_content("#status") == (
            f"Deployed 2 files and Create pipeline to {MOCK_ORIGIN}."
        )
        report = f"{DEPLOY_FOLDER}/legacy/report.py"
        load = f"{DEPLOY_FOLDER}/load.py"
        creator = f"{DEPLOY_FOLDER}/Create pipeline"
        imports = [r for r in workspace.requests if r["path"].endswith("/import")]
        assert [(r["body"]["path"], r["body"]["format"]) for r in imports] == [
            (report, "RAW"),
            (load, "RAW"),
            (creator, "SOURCE"),
        ]
        # A file has no language; a notebook's is Python.
        assert ["language" in r["body"] for r in imports] == [False, False, True]
        reading = {"catalog": "workspace", "schema": "raw"}
        expected = {
            report: sparkshift.to_pipeline(
                sparkshift.convert(DEPLOY_SOURCES["legacy/report.sas"], dialect="sas"),
                source="report.sas",
                dialect="sas",
                **reading,
            ),
            load: sparkshift.to_pipeline(
                sparkshift.convert(DEPLOY_SOURCES["load.sql"]),
                source="load.sql",
                **reading,
            ),
            creator: create_pipeline_notebook(
                pipeline_settings(
                    "sparkshift claims",
                    DEPLOY_FOLDER,
                    [report, load],
                    catalog="workspace",
                    schema="claims",
                )
            ),
        }
        assert imported(workspace) == {
            path: (source, False) for path, source in expected.items()
        }
        assert page.text_content("#deploy-link") == "Create pipeline"
        assert page.get_attribute("#deploy-link", "href") == (
            f"{MOCK_ORIGIN}/#workspace{DEPLOY_FOLDER}/Create%20pipeline"
        )
        assert page.text_content("#deploy-what") == "pipeline"
        assert page.text_content("#deploy-job-name") == "sparkshift claims"
    finally:
        page.check("#deploy-kind-job")


def test_a_pipeline_needs_a_catalog_and_schema_to_keep_its_tables(
    deploy_page: tuple[Any, FakeDatabricks],
) -> None:
    page, _ = deploy_page
    page.check("#deploy-kind-pipeline")
    try:
        fill_deploy_form(page, write="claims")

        assert page.text_content("#deploy-write-hint") == (
            "A pipeline keeps its tables in catalog.schema: type both."
        )
        assert page.get_attribute("#deploy-write", "aria-invalid") == "true"
        assert page.is_disabled("#deploy")
    finally:
        page.check("#deploy-kind-job")
    # A job writes nothing without a schema, and only a schema is fine.
    assert page.get_attribute("#deploy-write", "aria-invalid") == "false"
    assert page.is_enabled("#deploy")


def test_a_refused_token_stops_before_any_import(
    deploy_page: tuple[Any, FakeDatabricks],
) -> None:
    page, workspace = deploy_page
    workspace.requests.clear()
    workspace.answers = {"/api/2.0/workspace/mkdirs": (403, {"message": "denied"})}
    fill_deploy_form(page)

    deploy_and_wait(page)

    assert "refused the token" in page.text_content("#status")
    assert page.get_attribute("#statusbar", "data-state") == "bad"
    first = page.locator("#deploy-plan li").first
    assert first.get_attribute("data-state") == "bad"
    assert "refused the token" in first.text_content()
    assert [r["path"] for r in workspace.requests] == ["/api/2.0/workspace/mkdirs"]
    assert MOCK_TOKEN not in page.text_content("body")


def test_an_existing_notebook_is_replaced_only_when_asked(
    deploy_page: tuple[Any, FakeDatabricks],
) -> None:
    page, workspace = deploy_page
    workspace.requests.clear()
    workspace.answers = {
        "/api/2.0/workspace/import": (
            400,
            {"error_code": "RESOURCE_ALREADY_EXISTS", "message": "exists"},
        )
    }
    fill_deploy_form(page)

    deploy_and_wait(page)

    assert "Replace notebooks that already exist" in page.text_content("#status")
    assert len(imported(workspace)) == 1
    workspace.answers.clear()
    workspace.requests.clear()
    page.check("#deploy-overwrite")
    fill_deploy_form(page)

    deploy_and_wait(page)

    assert page.text_content("#status").startswith("Deployed 2 notebooks")
    assert {overwrite for _, overwrite in imported(workspace).values()} == {True}
    page.uncheck("#deploy-overwrite")


@pytest.mark.parametrize(
    ("host", "valid"),
    [
        ("https://example.com", False),
        ("http://dbc-mock.cloud.databricks.com", False),
        ("https://dbc-mock.cloud.databricks.com.evil.com", False),
        ("https://dbc-mock.cloud.databricks.com:8443", False),
        ("https://dbc-mock.cloud.databricks.com/?o=1234#workspace", True),
        ("https://adb-1.2.azuredatabricks.net", True),
        ("https://1234.5.gcp.databricks.com/", True),
    ],
)
def test_only_databricks_workspaces_are_accepted(
    deploy_page: tuple[Any, FakeDatabricks], host: str, valid: bool
) -> None:
    page, _ = deploy_page
    fill_deploy_form(page, host=host)

    assert page.is_enabled("#deploy") is valid
    assert (page.get_attribute("#deploy-host", "aria-invalid") == "true") is not valid


def test_the_page_may_connect_to_no_other_site(
    deploy_page: tuple[Any, FakeDatabricks],
) -> None:
    page, _ = deploy_page
    # The other site would answer, CORS included: only the page's security
    # policy can stop the request, before it leaves the browser.
    reached: list[str] = []

    def other_site(route: Any) -> None:
        reached.append(route.request.url)
        route.fulfill(headers={"Access-Control-Allow-Origin": "*"}, body="ok")

    page.context.route("https://example.com/**", other_site)
    try:
        outcome = page.evaluate(
            """() => new Promise((resolve) => {
                let violation = null;
                document.addEventListener("securitypolicyviolation", (event) => {
                    violation = event.effectiveDirective;
                }, { once: true });
                fetch("https://example.com/").then(
                    () => resolve("sent"),
                    () => setTimeout(() => resolve(`blocked by ${violation}`), 50),
                );
            })"""
        )
    finally:
        page.context.unroute("https://example.com/**")

    assert outcome == "blocked by connect-src"
    assert reached == []


def test_an_unreachable_workspace_is_explained(
    deploy_page: tuple[Any, FakeDatabricks],
) -> None:
    page, _ = deploy_page
    page.context.route(f"{MOCK_ORIGIN}/**", lambda route: route.abort())
    try:
        fill_deploy_form(page)
        deploy_and_wait(page)
        assert f"Could not reach {MOCK_ORIGIN}" in page.text_content("#status")
    finally:
        page.context.unroute(f"{MOCK_ORIGIN}/**")
        page.context.route(f"{MOCK_ORIGIN}/**", deploy_page[1])


def test_deploying_needs_every_file_of_the_folder(folder_view: Any) -> None:
    page = folder_view
    choose(page, str(COVERAGE))

    page.click("#tab-deploy")
    fill_deploy_form(page)

    assert "do not convert" in page.text_content("#deploy-note")
    assert "A job needs every file" in page.text_content("#deploy-note")
    assert page.is_disabled("#deploy")
    assert page.locator("#deploy-plan li").count() == 0


def test_a_pipeline_is_not_deployed_when_two_files_define_a_dataset(
    folder_view: Any, tmp_path: Path
) -> None:
    page = folder_view
    root = tmp_path / "jobs"
    for name in ("a/daily.sql", "b/daily.sql"):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("SELECT order_id FROM orders", encoding="utf-8")
    # As plain code both files convert; as a pipeline both define daily.
    choose(page, str(root))
    workspace = FakeDatabricks()
    page.context.route(f"{MOCK_ORIGIN}/**", workspace)
    page.click("#tab-deploy")
    page.check("#deploy-kind-pipeline")
    try:
        fill_deploy_form(page)

        deploy_and_wait(page)

        assert page.text_content("#status") == (
            "Deployment stopped: a/daily.sql: its dataset daily is also defined "
            "by b/daily.sql; a pipeline defines each dataset once."
        )
        assert workspace.requests == []
    finally:
        page.check("#deploy-kind-job")
        page.context.unroute(f"{MOCK_ORIGIN}/**")


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_deploy_view_has_no_accessibility_violations(
    browser: Any, site_url: str, color_scheme: str, tmp_path: Path
) -> None:
    from axe_playwright_python.sync_playwright import Axe

    (tmp_path / "claims").mkdir()
    (tmp_path / "claims" / "load.sql").write_text("SELECT a FROM t", encoding="utf-8")
    page = open_page(browser, site_url, bypass_csp=True, color_scheme=color_scheme)
    page.click("#tab-folder")
    choose(page, str(tmp_path / "claims"))
    page.click("#tab-deploy")
    fill_deploy_form(page, host="https://not-databricks.example.com")
    assert page.locator("#deploy-plan li").count() == 3
    # The pipeline's form: its own labels and hints.
    page.check("#deploy-kind-pipeline")

    results = Axe().run(
        page, options={"runOnly": ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]}
    )

    assert results.violations_count == 0, results.generate_report()
    page.context.close()


def test_deploy_view_fits_a_phone_screen(browser: Any, site_url: str) -> None:
    page = open_page(browser, site_url, viewport={"width": 375, "height": 812})
    page.click("#tab-deploy")
    fill_deploy_form(page)

    scroll_width = page.evaluate("document.documentElement.scrollWidth")

    assert scroll_width <= 375
    page.context.close()


# --- The page itself ------------------------------------------------------------


def test_page_contacts_only_itself_and_the_pyodide_cdn(session: dict) -> None:
    assert session["origins"] <= ALLOWED_ORIGINS


def test_page_logs_no_errors(session: dict) -> None:
    assert session["errors"] == []


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
@pytest.mark.parametrize("with_warning", [False, True])
def test_page_has_no_accessibility_violations(
    browser: Any, site_url: str, color_scheme: str, with_warning: bool
) -> None:
    from axe_playwright_python.sync_playwright import Axe

    # Axe is injected as a script, which the page's security policy forbids;
    # bypassing it does not change what Axe checks.
    page = open_page(browser, site_url, bypass_csp=True, color_scheme=color_scheme)

    if with_warning:
        convert_in_page(page, WARNING_PROGRAM, "sas")
        assert page.is_visible("#warnings")

    results = Axe().run(
        page, options={"runOnly": ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]}
    )

    assert results.violations_count == 0, results.generate_report()
    page.context.close()


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_notebook_cells_have_no_accessibility_violations(
    browser: Any, site_url: str, color_scheme: str
) -> None:
    from axe_playwright_python.sync_playwright import Axe

    page = open_page(browser, site_url, bypass_csp=True, color_scheme=color_scheme)
    show_notebook(page, WARNING_PROGRAM, "sas")
    # One setup cell open, one folded.
    page.locator("#output-cells > details.setup summary").first.click()

    results = Axe().run(
        page, options={"runOnly": ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]}
    )

    assert results.violations_count == 0, results.generate_report()
    page.context.close()


def test_notebook_cells_fit_a_phone_screen(browser: Any, site_url: str) -> None:
    page = open_page(browser, site_url, viewport={"width": 375, "height": 812})

    show_notebook(page, WARNING_PROGRAM, "sas")
    page.locator("#output-cells > details.setup summary").first.click()
    scroll_width = page.evaluate("document.documentElement.scrollWidth")

    assert scroll_width <= 375
    page.context.close()


def test_page_fits_a_phone_screen(browser: Any, site_url: str) -> None:
    page = open_page(browser, site_url, viewport={"width": 375, "height": 812})

    convert_in_page(page, WARNING_PROGRAM, "sas")
    scroll_width = page.evaluate("document.documentElement.scrollWidth")

    assert scroll_width <= 375
    page.context.close()


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_folder_view_has_no_accessibility_violations(
    browser: Any, site_url: str, color_scheme: str
) -> None:
    from axe_playwright_python.sync_playwright import Axe

    page = open_page(browser, site_url, bypass_csp=True, color_scheme=color_scheme)
    page.click("#tab-folder")
    assert page.text_content("#status") == (
        "Choose a folder or files to convert them all at once."
    )
    choose(page, str(COVERAGE))
    # A file with errors, while the summary shows every state.
    file_button(page, "unfinished.sql").click()
    assert page.is_visible("#file-errors")

    results = Axe().run(
        page, options={"runOnly": ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]}
    )

    assert results.violations_count == 0, results.generate_report()
    page.context.close()


def test_folder_view_fits_a_phone_screen(browser: Any, site_url: str) -> None:
    page = open_page(browser, site_url, viewport={"width": 375, "height": 812})
    page.click("#tab-folder")

    choose(page, str(COVERAGE))
    scroll_width = page.evaluate("document.documentElement.scrollWidth")

    assert scroll_width <= 375
    page.context.close()


def test_page_without_javascript_says_it_needs_it(browser: Any, site_url: str) -> None:
    context = browser.new_context(java_script_enabled=False)
    page = context.new_page()
    page.goto(site_url)

    assert "needs JavaScript" in page.text_content("noscript")
    context.close()


def test_page_explains_when_python_cannot_load(browser: Any, site_url: str) -> None:
    context = browser.new_context()
    context.route("https://cdn.jsdelivr.net/**", lambda route: route.abort())
    page = context.new_page()
    page.goto(site_url)

    page.wait_for_selector("#status:has-text('could not start')")
    assert "cdn.jsdelivr.net" in page.text_content("#status")
    context.close()


# --- Warnings and recovery ----------------------------------------------------

WARNING_PROGRAM = "proc sort data=orders out=dedup nodupkey; by customer_id; run;"


def assert_failed_output_is_cleared(page: Any) -> None:
    assert page.is_visible("#errors")
    assert page.is_hidden("#output")
    assert page.is_hidden("#output-cells")
    assert page.is_disabled("#copy")
    assert page.text_content("#code") == ""
    assert page.is_hidden("#warnings")
    assert page.text_content("#warnings") == ""
    assert page.text_content("#status") == "Conversion failed."


def test_real_sas_warnings_are_shown_and_cleared(session: dict) -> None:
    page = session["page"]
    expected = sparkshift.convert(WARNING_PROGRAM, dialect="sas")
    assert expected.warnings
    convert_in_page(page, WARNING_PROGRAM, "sas")
    assert page.is_visible("#warnings")
    assert page.locator("#warnings li").all_text_contents() == [
        str(warning) for warning in expected.warnings
    ]
    assert page.text_content("#code") == expected.code
    assert page.is_enabled("#copy")
    assert "warning" in page.text_content("#status")
    convert_in_page(page, "SELECT id FROM t", "")
    assert page.is_hidden("#warnings")
    assert page.text_content("#warnings") == ""
    assert page.text_content("#status") == "Converted successfully."


def test_warning_output_is_cleared_after_an_unsupported_query(session: dict) -> None:
    page = session["page"]
    convert_in_page(page, WARNING_PROGRAM, "sas")
    convert_in_page(page, "SELECT * EXCEPT (secret) FROM t", "")
    assert_failed_output_is_cleared(page)


def test_deep_query_clears_previous_output_and_can_recover(session: dict) -> None:
    page = session["page"]
    convert_in_page(page, "SELECT id FROM t", "")
    sql = "SELECT id FROM orders WHERE " + " AND ".join(
        f"id != {n}" for n in range(1000)
    )
    convert_in_page(page, sql, "")
    assert_failed_output_is_cleared(page)
    assert "deeply nested" in page.text_content("#errors")
    convert_in_page(page, "SELECT name FROM customers", "")
    assert page.is_hidden("#errors")
    assert page.is_enabled("#copy")
    assert (
        page.text_content("#code")
        == sparkshift.convert("SELECT name FROM customers").code
    )


def test_browser_rejects_oversized_unicode_source_before_conversion(
    session: dict,
) -> None:
    page = session["page"]
    convert_in_page(page, WARNING_PROGRAM, "sas")
    # UTF-16 length is below 50,000, but UTF-8 bytes exceed the limit.
    sql = "SELECT id FROM t --" + "😀" * 12_500
    convert_in_page(page, sql, "")
    assert_failed_output_is_cleared(page)
    assert "50,000 UTF-8 bytes" in page.text_content("#errors")
    assert "CLI" in page.text_content("#errors")


@pytest.mark.parametrize("fault", ["exception", "json", "warning_markup"])
def test_runtime_boundary_recovers_without_showing_stale_output(
    browser: Any, site_url: str, fault: str
) -> None:
    # Inject failures in the Python/JavaScript boundary, without a production
    # test hook or access to app.js's private module state.
    context = browser.new_context()

    def bridge_fault(route: Any) -> None:
        response = route.fetch()
        injected = response.text() + "\n_original = convert_for_page\n"
        injected += "def convert_for_page(sql, dialect):\n"
        injected += "    if sql == 'SELECT forced_fault FROM t':\n"
        if fault == "exception":
            injected += "        raise RuntimeError('internal failure sentinel')\n"
        elif fault == "json":
            injected += "        return '{not-json'\n"
        else:
            injected += (
                '        return json.dumps({"ok": True, "code": "result = x\\n", '
                '"warnings": ["Review <img src=x onerror=alert(1)>", '
                '"Check row selection"]})\n'
            )
        injected += "    return _original(sql, dialect)\n"
        route.fulfill(response=response, body=injected)

    context.route("**/bridge.py", bridge_fault)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(site_url, wait_until="domcontentloaded")
        page.wait_for_selector(IDLE, timeout=READY_TIMEOUT_MS)
        convert_in_page(page, WARNING_PROGRAM, "sas")
        convert_in_page(page, "SELECT forced_fault FROM t", "")
        if fault == "warning_markup":
            assert page.is_visible("#warnings")
            assert "<img src=x" in page.text_content("#warnings")
            assert page.locator("#warnings img").count() == 0
            assert "2 warnings" in page.text_content("#status")
        else:
            assert_failed_output_is_cleared(page)
            assert "unexpectedly" in page.text_content("#errors")
            assert "internal failure sentinel" not in page.text_content("#errors")
        convert_in_page(page, "SELECT id FROM t", "")
        assert page.text_content("#code") == sparkshift.convert("SELECT id FROM t").code
        assert page.is_hidden("#errors")
        assert page.is_hidden("#warnings")
        assert page.is_enabled("#copy")
        assert errors == []
    finally:
        context.close()


@pytest.mark.parametrize("change", ["source", "dialect"])
def test_editing_input_invalidates_generated_code_and_warnings(
    session: dict, change: str
) -> None:
    page = session["page"]
    convert_in_page(page, WARNING_PROGRAM, "sas")
    assert page.is_enabled("#copy")
    if change == "source":
        page.fill("#sql", "data other; set orders; run;")
    else:
        page.select_option("#dialect", "")
    assert page.is_hidden("#output")
    assert page.is_hidden("#warnings")
    assert page.text_content("#code") == ""
    assert page.text_content("#warnings") == ""
    assert page.is_disabled("#copy")
    assert "Input changed" in page.text_content("#status")


def test_browser_accepts_exactly_the_unicode_byte_limit(session: dict) -> None:
    page = session["page"]
    prefix = "SELECT id FROM t --"
    sql = prefix + "é" * ((50_000 - len(prefix)) // 2)
    sql += " " * (50_000 - len(sql.encode("utf-8")))
    assert len(sql.encode("utf-8")) == 50_000
    convert_in_page(page, sql, "")
    assert page.is_hidden("#errors")
    assert page.is_enabled("#copy")
    assert page.text_content("#code") == sparkshift.convert(sql).code


def test_starting_converts_nothing(browser: Any, site_url: str) -> None:
    """Python converts only when the visitor presses Convert: starting the
    page, or a shared link, never calls it."""
    context = browser.new_context()

    def counted(route: Any) -> None:
        response = route.fetch()
        injected = response.text() + "\n_original = convert_for_page\n"
        injected += (
            "def convert_for_page(sql, dialect):\n"
            "    from js import console\n"
            "    console.log('conversion requested')\n"
            "    return _original(sql, dialect)\n"
        )
        route.fulfill(response=response, body=injected)

    context.route("**/bridge.py", counted)
    page = context.new_page()
    logged: list[str] = []
    page.on("console", lambda message: logged.append(message.text))
    try:
        page.goto(f"{site_url}#dialect=tsql&sql=SELECT+TOP+1+a+FROM+t")
        page.wait_for_selector(IDLE, timeout=READY_TIMEOUT_MS)
        page.wait_for_timeout(500)
        assert "conversion requested" not in logged
        assert page.input_value("#sql") == "SELECT TOP 1 a FROM t"
        assert page.is_hidden("#output")
        page.click("#convert")
        page.wait_for_selector(IDLE, timeout=READY_TIMEOUT_MS)
        assert logged.count("conversion requested") == 1
    finally:
        context.close()


def test_python_runtime_is_isolated_from_the_page(session: dict) -> None:
    page = session["page"]
    assert page.evaluate("typeof loadPyodide") == "undefined"
    assert page.locator("#cancel").count() == 1
    assert page.is_hidden("#cancel")


@pytest.fixture
def blocked_worker_page(browser: Any, site_url: str) -> Iterator[Any]:
    """Real Pyodide, with a CPU-bound Python fault on one specific input."""
    context = browser.new_context()

    def bridge_fault(route: Any) -> None:
        response = route.fetch()
        injected = response.text() + "\n_original = convert_for_page\n"
        injected += (
            "def convert_for_page(sql, dialect):\n"
            "    if sql == 'SELECT worker_busy FROM t':\n"
            "        from js import console\n"
            "        console.log('worker CPU fault entered')\n"
            "        while True:\n"
            "            pass\n"
            "    return _original(sql, dialect)\n"
        )
        route.fulfill(response=response, body=injected)

    context.route("**/bridge.py", bridge_fault)
    page = context.new_page()
    try:
        page.goto(site_url)
        page.wait_for_selector(IDLE, timeout=READY_TIMEOUT_MS)
        # Do not run the blocking fault if worker isolation regresses.
        assert page.evaluate("typeof loadPyodide") == "undefined"
        yield page
    finally:
        context.close()


def block_python(page: Any) -> None:
    convert_in_page(page, WARNING_PROGRAM, "sas")
    page.select_option("#dialect", "")
    page.fill("#sql", "SELECT worker_busy FROM t")
    with page.expect_console_message(
        predicate=lambda message: message.text == "worker CPU fault entered"
    ):
        page.click("#convert")
    assert page.is_visible("#cancel")
    assert page.is_disabled("#convert")
    assert page.is_hidden("#output")
    assert page.is_hidden("#warnings")
    assert page.is_disabled("#copy")
    # A timer still runs on the UI thread while Python is burning CPU.
    assert (
        page.evaluate(
            "() => new Promise(resolve => setTimeout(() => resolve('responsive'), 20))"
        )
        == "responsive"
    )


@pytest.mark.parametrize("action", ["cancel", "source", "dialect"])
def test_a_blocked_python_worker_can_be_cancelled_and_recovered(
    blocked_worker_page: Any, action: str
) -> None:
    page = blocked_worker_page
    block_python(page)

    def delay_reload(route: Any) -> None:
        response = route.fetch()
        injected = response.text().replace(
            '    if (type === "init") {',
            '    if (type === "init") {\n'
            "      await new Promise(resolve => setTimeout(resolve, 500));",
        )
        route.fulfill(response=response, body=injected)

    page.context.route("**/worker.js", delay_reload)
    if action == "cancel":
        page.click("#cancel")
    elif action == "source":
        page.fill("#sql", "SELECT name FROM customers")
    else:
        page.select_option("#dialect", "postgres")
    # Readiness must not be claimed while the replacement runtime is loading.
    assert page.is_disabled("#convert")
    assert "Reloading Python" in page.text_content("#status")
    assert "Ready" not in page.text_content("#status")
    page.wait_for_selector(IDLE, timeout=READY_TIMEOUT_MS)
    assert page.is_hidden("#cancel")
    assert page.is_hidden("#output")
    assert page.is_disabled("#copy")
    assert "cancelled" in page.text_content(
        "#status"
    ) or "Input changed" in page.text_content("#status")
    convert_in_page(page, "SELECT name FROM customers", "")
    assert (
        page.text_content("#code")
        == sparkshift.convert("SELECT name FROM customers").code
    )
    assert page.is_hidden("#errors")
    assert page.is_enabled("#copy")


def test_blocked_python_is_terminated_at_the_conversion_deadline(
    blocked_worker_page: Any,
) -> None:
    page = blocked_worker_page
    # Accelerate the real production deadline, rather than waiting ten seconds.
    page.clock.install()
    block_python(page)
    page.clock.fast_forward(10_001)
    page.wait_for_selector("#errors:has-text('10-second limit')")
    page.wait_for_selector(IDLE, timeout=READY_TIMEOUT_MS)
    assert page.is_hidden("#output")
    assert page.is_disabled("#copy")
    assert "CLI" in page.text_content("#errors")
    assert "Conversion failed" in page.text_content("#status")
    convert_in_page(page, "SELECT id FROM orders", "")
    assert (
        page.text_content("#code") == sparkshift.convert("SELECT id FROM orders").code
    )
    assert page.is_hidden("#errors")


def test_successful_requests_disarm_their_deadlines(blocked_worker_page: Any) -> None:
    page = blocked_worker_page
    page.clock.install()
    convert_in_page(page, "SELECT id FROM orders", "")
    page.clock.fast_forward(10_001)
    assert page.is_hidden("#errors")
    assert page.is_enabled("#convert")
    assert page.is_enabled("#copy")
    assert (
        page.text_content("#code") == sparkshift.convert("SELECT id FROM orders").code
    )
    convert_in_page(page, "SELECT name FROM customers", "")
    assert (
        page.text_content("#code")
        == sparkshift.convert("SELECT name FROM customers").code
    )


def busy_folder(tmp_path: Path) -> Path:
    """A folder whose second file blocks Python (see blocked_worker_page)."""
    folder = tmp_path / "busy"
    folder.mkdir()
    for name, sql in [
        ("a.sql", "SELECT a FROM t"),
        ("b.sql", "SELECT worker_busy FROM t"),
        ("c.sql", "SELECT c FROM t"),
    ]:
        (folder / name).write_text(sql, encoding="utf-8")
    return folder


def block_python_in_a_folder(page: Any, folder: Path) -> None:
    page.click("#tab-folder")
    with page.expect_console_message(
        predicate=lambda message: message.text == "worker CPU fault entered"
    ):
        page.set_input_files("#pick-folder", str(folder))
    # What decides the results stays fixed while the files convert.
    for control in ["#dialect", "#output-notebook", "#choose-folder", "#convert"]:
        assert page.is_disabled(control)
    assert page.is_visible("#cancel")


def test_a_file_past_the_deadline_does_not_stop_its_folder(
    blocked_worker_page: Any, tmp_path: Path
) -> None:
    page = blocked_worker_page
    page.clock.install()
    block_python_in_a_folder(page, busy_folder(tmp_path))

    page.clock.fast_forward(10_001)
    page.wait_for_selector(IDLE, timeout=READY_TIMEOUT_MS)

    assert page.text_content("#status") == (
        "Converted 2 of 3 files; 1 could not be converted."
    )
    file_button(page, "b.sql").click()
    assert "10-second limit" in page.text_content("#file-errors")
    _, files = downloaded_zip(page)
    assert files == {
        "busy_pyspark/a.py": sparkshift.convert("SELECT a FROM t").code.encode(),
        "busy_pyspark/c.py": sparkshift.convert("SELECT c FROM t").code.encode(),
    }


def test_cancelling_a_folder_stops_all_its_files(
    blocked_worker_page: Any, tmp_path: Path
) -> None:
    page = blocked_worker_page
    block_python_in_a_folder(page, busy_folder(tmp_path))

    page.click("#cancel")
    page.wait_for_selector(IDLE, timeout=READY_TIMEOUT_MS)

    assert "cancelled" in page.text_content("#status")
    assert page.locator("#files .tag").all_text_contents() == ["Waiting"] * 3
    assert page.is_hidden("#file-output")
    assert page.is_disabled("#download-zip")
    assert page.is_disabled("#copy")
    page.click("#tab-file")
    convert_in_page(page, "SELECT id FROM orders", "")
    assert (
        page.text_content("#code") == sparkshift.convert("SELECT id FROM orders").code
    )


def test_stale_worker_messages_cannot_replace_current_output(
    browser: Any, site_url: str
) -> None:
    context = browser.new_context()

    def stale_reply(route: Any) -> None:
        response = route.fetch()
        injected = response.text().replace(
            "self.postMessage({ id, result });",
            """if (sql === "SELECT id FROM orders") {
              self.postMessage({ id: id - 1, result: {
                ok: true, code: "stale result", warnings: []
              } });
              await new Promise(resolve => setTimeout(resolve, 200));
            }
            self.postMessage({ id, result });""",
        )
        route.fulfill(response=response, body=injected)

    context.route("**/worker.js", stale_reply)
    page = context.new_page()
    try:
        page.goto(site_url)
        page.wait_for_selector(IDLE, timeout=READY_TIMEOUT_MS)
        convert_in_page(page, "SELECT id FROM orders", "")
        assert (
            page.text_content("#code")
            == sparkshift.convert("SELECT id FROM orders").code
        )
        assert page.is_hidden("#errors")
    finally:
        context.close()


def test_worker_crash_clears_output_and_reloads_python(
    browser: Any, site_url: str
) -> None:
    context = browser.new_context()

    def crash(route: Any) -> None:
        response = route.fetch()
        injected = response.text().replace(
            "  try {",
            """  if (type === "convert" && sql === "SELECT forced_crash FROM t") {
              setTimeout(() => { throw new Error("private crash sentinel"); }, 0);
              return;
            }
            try {""",
            1,
        )
        route.fulfill(response=response, body=injected)

    context.route("**/worker.js", crash)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(site_url)
        page.wait_for_selector(IDLE, timeout=READY_TIMEOUT_MS)
        convert_in_page(page, WARNING_PROGRAM, "sas")
        convert_in_page(page, "SELECT forced_crash FROM t", "")
        assert page.is_hidden("#output")
        assert page.is_hidden("#warnings")
        assert page.is_disabled("#copy")
        assert "unexpectedly" in page.text_content("#errors")
        assert "private crash sentinel" not in page.text_content("#errors")
        convert_in_page(page, "SELECT id FROM orders", "")
        assert (
            page.text_content("#code")
            == sparkshift.convert("SELECT id FROM orders").code
        )
        assert errors == []
    finally:
        context.close()


def test_startup_failure_offers_a_working_retry(browser: Any, site_url: str) -> None:
    context = browser.new_context()

    def blocked(route: Any) -> None:
        route.abort()

    context.route("https://cdn.jsdelivr.net/**", blocked)
    page = context.new_page()
    try:
        page.goto(site_url)
        page.wait_for_selector("#status:has-text('could not start')")
        assert page.is_visible("#retry")
        assert page.is_disabled("#convert")
        assert page.is_disabled("#copy")
        context.unroute("https://cdn.jsdelivr.net/**", blocked)
        page.click("#retry")
        page.wait_for_selector(IDLE, timeout=READY_TIMEOUT_MS)
        assert page.is_hidden("#retry")
        convert_in_page(page, "SELECT id FROM orders", "")
        assert (
            page.text_content("#code")
            == sparkshift.convert("SELECT id FROM orders").code
        )
    finally:
        context.close()


def test_unresponsive_startup_is_bounded(browser: Any, site_url: str) -> None:
    context = browser.new_context()
    context.route(
        "**/worker.js",
        lambda route: route.fulfill(
            content_type="text/javascript", body="self.onmessage = () => {};"
        ),
    )

    def shorter_deadline(route: Any) -> None:
        response = route.fetch()
        route.fulfill(
            response=response,
            body=response.text().replace(
                "STARTUP_TIMEOUT_MS = 120_000", "STARTUP_TIMEOUT_MS = 500"
            ),
        )

    context.route("**/worker-client.js", shorter_deadline)
    page = context.new_page()
    try:
        page.goto(site_url)
        page.wait_for_selector("#status:has-text('could not start')")
        assert page.is_visible("#retry")
        assert page.is_disabled("#convert")
        assert page.is_disabled("#copy")
        assert page.is_hidden("#cancel")
    finally:
        context.close()


def test_worker_crash_while_idle_invalidates_output_and_recovers(
    browser: Any, site_url: str
) -> None:
    context = browser.new_context()

    def crash_after_reply(route: Any) -> None:
        response = route.fetch()
        injected = response.text().replace(
            "self.postMessage({ id, result });",
            """self.postMessage({ id, result });
            if (sql === "SELECT idle_crash FROM t") {
              setTimeout(() => { throw new Error("idle crash sentinel"); }, 200);
            }""",
        )
        route.fulfill(response=response, body=injected)

    context.route("**/worker.js", crash_after_reply)
    page = context.new_page()
    try:
        page.goto(site_url)
        page.wait_for_selector(IDLE, timeout=READY_TIMEOUT_MS)
        convert_in_page(page, "SELECT idle_crash FROM t", "")
        page.wait_for_selector("#errors:has-text('stopped unexpectedly')")
        page.wait_for_selector(IDLE, timeout=READY_TIMEOUT_MS)
        assert page.is_hidden("#output")
        assert page.is_disabled("#copy")
        assert "idle crash sentinel" not in page.text_content("#errors")
        convert_in_page(page, "SELECT id FROM orders", "")
        assert (
            page.text_content("#code")
            == sparkshift.convert("SELECT id FROM orders").code
        )
        assert page.is_hidden("#errors")
    finally:
        context.close()

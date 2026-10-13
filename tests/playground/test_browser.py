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

pytestmark = pytest.mark.browser

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from build_playground import build  # noqa: E402

playwright_api = pytest.importorskip("playwright.sync_api")

# Pyodide's first load downloads about 10 MB.
READY_TIMEOUT_MS = 180_000
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
    page.wait_for_selector("#convert:enabled", timeout=READY_TIMEOUT_MS)
    return page


@pytest.fixture(scope="module")
def session(browser: Any, site_url: str) -> Iterator[dict]:
    """One page, loaded once, shared by most tests (loading Pyodide is slow).
    Records every origin the page contacts and every console error."""
    context = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
    page = context.new_page()
    origins: set[str] = set()
    errors: list[str] = []
    page.on("request", lambda request: origins.add(urlsplit(request.url).hostname))
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on(
        "console",
        lambda message: (
            errors.append(message.text) if message.type == "error" else None
        ),
    )
    page.goto(site_url)
    page.wait_for_selector("#convert:enabled", timeout=READY_TIMEOUT_MS)
    yield {"page": page, "origins": origins, "errors": errors}
    context.close()


def convert_in_page(page: Any, sql: str, dialect: str) -> None:
    page.select_option("#dialect", dialect)
    page.fill("#sql", sql)
    page.click("#convert")


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

    assert (
        page.text_content("#code")
        == sparkshift.convert("SELECT name FROM customers").code
    )


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
    assert shared.text_content("#code") == sparkshift.convert(sql, dialect="tsql").code
    shared.context.close()


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


def test_page_fits_a_phone_screen(browser: Any, site_url: str) -> None:
    page = open_page(browser, site_url, viewport={"width": 375, "height": 812})

    convert_in_page(page, WARNING_PROGRAM, "sas")
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
        page.wait_for_selector("#convert:enabled", timeout=READY_TIMEOUT_MS)
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


def test_initial_conversion_failure_is_not_reported_as_ready(
    browser: Any, site_url: str
) -> None:
    context = browser.new_context()

    def bridge_fault(route: Any) -> None:
        response = route.fetch()
        injected = response.text() + "\n_original = convert_for_page\n_attempts = 0\n"
        injected += (
            "def convert_for_page(sql, dialect):\n"
            "    global _attempts\n"
            "    _attempts += 1\n"
            "    if _attempts == 1:\n"
            "        raise RuntimeError('startup conversion failure')\n"
            "    return _original(sql, dialect)\n"
        )
        route.fulfill(response=response, body=injected)

    context.route("**/bridge.py", bridge_fault)
    page = context.new_page()
    try:
        page.goto(site_url, wait_until="domcontentloaded")
        page.wait_for_selector("#convert:enabled", timeout=READY_TIMEOUT_MS)
        assert_failed_output_is_cleared(page)
        convert_in_page(page, "SELECT id FROM t", "")
        assert page.text_content("#code") == sparkshift.convert("SELECT id FROM t").code
        assert page.is_hidden("#errors")
        assert page.is_enabled("#copy")
    finally:
        context.close()

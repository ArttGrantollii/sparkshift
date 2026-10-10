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
    ]


def test_every_example_matches_the_command_line(session: dict) -> None:
    page = session["page"]
    examples = sorted((ROOT / "examples" / "sql").rglob("*.sql"))
    labels = page.locator("#example option").all_text_contents()[1:]
    assert len(labels) == len(examples)

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
def test_page_has_no_accessibility_violations(
    browser: Any, site_url: str, color_scheme: str
) -> None:
    from axe_playwright_python.sync_playwright import Axe

    # Axe is injected as a script, which the page's security policy forbids;
    # bypassing it does not change what Axe checks.
    page = open_page(browser, site_url, bypass_csp=True, color_scheme=color_scheme)

    results = Axe().run(
        page, options={"runOnly": ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]}
    )

    assert results.violations_count == 0, results.generate_report()
    page.context.close()


def test_page_fits_a_phone_screen(browser: Any, site_url: str) -> None:
    page = open_page(browser, site_url, viewport={"width": 375, "height": 812})

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

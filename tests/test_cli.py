"""Tests for the command line, used exactly as a shell user would."""

import importlib
import io
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import sparkshift
from sparkshift.cli import EXIT_CONVERTED, EXIT_NOT_CONVERTED, EXIT_USAGE, main
from sparkshift.diagnostics import ConversionResult, Diagnostic

SQL = "SELECT order_id, amount * 2 AS doubled FROM orders WHERE status = 'paid'"
CODE = sparkshift.convert(SQL).code


@pytest.fixture
def sql_file(tmp_path: Path) -> Path:
    path = tmp_path / "query.sql"
    path.write_text(SQL, encoding="utf-8")
    return path


def test_convert_prints_the_code(
    sql_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["convert", str(sql_file)]) == EXIT_CONVERTED

    out, err = capsys.readouterr()
    assert out == CODE
    assert err == ""


@pytest.mark.parametrize("dialect", ["tsql", "TSQL", "postgres", "bigquery"])
def test_convert_honors_the_dialect(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], dialect: str
) -> None:
    sql = "SELECT TOP 3 name FROM customers" if "tsql" in dialect.lower() else SQL
    path = tmp_path / "query.sql"
    path.write_text(sql, encoding="utf-8")

    assert main(["convert", str(path), "--dialect", dialect]) == EXIT_CONVERTED

    assert capsys.readouterr().out == sparkshift.convert(sql, dialect=dialect).code


def test_convert_reads_standard_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(SQL))

    assert main(["convert", "-"]) == EXIT_CONVERTED

    assert capsys.readouterr().out == CODE


def test_convert_writes_the_output_file(
    sql_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "query.py"

    assert main(["convert", str(sql_file), "-o", str(output)]) == EXIT_CONVERTED

    assert output.read_bytes() == CODE.encode()  # LF line endings everywhere
    assert capsys.readouterr() == ("", "")


def test_unsupported_sql_lists_every_issue_on_standard_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "bad.sql"
    path.write_text("SELECT my_udf(a) AS x, other_udf(b) AS y FROM t", encoding="utf-8")

    assert main(["convert", str(path)]) == EXIT_NOT_CONVERTED

    out, err = capsys.readouterr()
    assert out == ""
    assert err == (
        f"sparkshift: cannot convert {path}: 2 unsupported constructs:\n"
        "  - function MY_UDF: MY_UDF(a)\n"
        "  - function OTHER_UDF: OTHER_UDF(b)\n"
    )


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("SELECT (a FROM t", "line 1, column"),
        ("SELECT 1 FROM t; SELECT 2 FROM t", "statement"),
        ("", "no sql statement"),
    ],
)
def test_invalid_sql_is_not_converted(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    sql: str,
    message: str,
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(sql))

    assert main(["convert", "-"]) == EXIT_NOT_CONVERTED

    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("sparkshift: cannot convert standard input: ")
    assert message in err.lower()


def test_failed_conversion_writes_no_output_file(tmp_path: Path) -> None:
    path = tmp_path / "bad.sql"
    path.write_text("SELECT my_udf(a) AS x FROM t", encoding="utf-8")
    output = tmp_path / "bad.py"

    assert main(["convert", str(path), "-o", str(output)]) == EXIT_NOT_CONVERTED

    assert not output.exists()


def test_warnings_go_to_standard_error(
    sql_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # No conversion produces warnings yet; the command line must show them.
    warning = Diagnostic("fallback used", "x", "Review it.")
    monkeypatch.setattr(
        "sparkshift.cli.convert",
        lambda sql, dialect: ConversionResult("result = x\n", (warning,)),
    )

    assert main(["convert", str(sql_file)]) == EXIT_CONVERTED

    out, err = capsys.readouterr()
    assert out == "result = x\n"
    assert (
        err
        == f"sparkshift: warning in {sql_file}: fallback used: x. Hint: Review it.\n"
    )


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["convert", "missing.sql"], "cannot read missing.sql"),
        (["convert", "-", "--dialect", "sqlserver"], "invalid choice: 'sqlserver'"),
        ([], "required"),
        (["translate", "q.sql"], "invalid choice: 'translate'"),
    ],
)
def test_usage_errors_exit_with_2(
    capsys: pytest.CaptureFixture[str], arguments: list[str], message: str
) -> None:
    with pytest.raises(SystemExit) as caught:
        main(arguments)

    assert caught.value.code == EXIT_USAGE
    assert message in capsys.readouterr().err


def test_unreadable_and_unwritable_files_are_usage_errors(
    tmp_path: Path, sql_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    latin1 = tmp_path / "latin1.sql"
    latin1.write_bytes("SELECT 'café' AS x FROM t".encode("latin-1"))

    with pytest.raises(SystemExit) as caught:
        main(["convert", str(latin1)])
    assert caught.value.code == EXIT_USAGE
    assert "is not UTF-8 text" in capsys.readouterr().err

    with pytest.raises(SystemExit) as caught:
        main(["convert", str(sql_file), "-o", str(tmp_path)])  # a directory
    assert caught.value.code == EXIT_USAGE
    assert f"cannot write {tmp_path}" in capsys.readouterr().err


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["--version"])

    assert caught.value.code == 0
    assert capsys.readouterr().out == f"sparkshift {sparkshift.__version__}\n"


def test_python_dash_m_runs_the_command_line(
    sql_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["sparkshift", "convert", str(sql_file)])

    with pytest.raises(SystemExit) as caught:
        runpy.run_module("sparkshift", run_name="__main__")

    assert caught.value.code == EXIT_CONVERTED
    assert capsys.readouterr().out == CODE


def test_installed_script(sql_file: Path) -> None:
    # The `sparkshift` command the package installs, run as a real process.
    script = shutil.which("sparkshift", path=str(Path(sys.executable).parent))
    assert script is not None, "the sparkshift script is not installed"

    completed = subprocess.run(
        [script, "convert", str(sql_file)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == EXIT_CONVERTED
    assert completed.stdout == CODE
    assert completed.stderr == ""


def test_importing_main_does_not_run_the_command_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Tools that import every module, such as the architecture tests, must
    # not start the command line.
    sys.modules.pop("sparkshift.__main__", None)

    importlib.import_module("sparkshift.__main__")

    assert capsys.readouterr() == ("", "")


# --- Converting a directory --------------------------------------------------


@pytest.fixture
def queries(tmp_path: Path) -> Path:
    """A directory of queries: two convertible (one nested), one not, one
    not UTF-8, and a file that is not SQL."""
    root = tmp_path / "queries"
    (root / "reports").mkdir(parents=True)
    (root / "a_orders.sql").write_text(SQL, encoding="utf-8")
    (root / "reports" / "b_customers.sql").write_text(
        "SELECT name FROM customers", encoding="utf-8"
    )
    (root / "c_bad.sql").write_text("SELECT my_udf(a) AS x FROM t", encoding="utf-8")
    (root / "d_latin1.sql").write_bytes("SELECT 'café' AS x FROM t".encode("latin-1"))
    (root / "notes.txt").write_text("not a query", encoding="utf-8")
    return root


def test_directory_converts_each_sql_file_into_the_output_directory(
    queries: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "pyspark"

    assert main(["convert", str(queries), "-o", str(output)]) == EXIT_NOT_CONVERTED

    written = sorted(path.relative_to(output) for path in output.rglob("*"))
    assert written == [
        Path("a_orders.py"),
        Path("reports"),
        Path("reports/b_customers.py"),
    ]
    assert (output / "a_orders.py").read_text(encoding="utf-8") == CODE
    assert (output / "reports" / "b_customers.py").read_text(
        encoding="utf-8"
    ) == sparkshift.convert("SELECT name FROM customers").code

    out, err = capsys.readouterr()
    assert out == ""
    assert err == (
        f"sparkshift: cannot convert {queries / 'c_bad.sql'}: "
        "1 unsupported construct:\n"
        "  - function MY_UDF: MY_UDF(a)\n"
        f"sparkshift: cannot read {queries / 'd_latin1.sql'}: it is not UTF-8 text\n"
        "sparkshift: converted 2 of 4 files; 2 could not be converted\n"
    )


def test_directory_where_everything_converts_exits_with_0(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "queries"
    source.mkdir()
    (source / "only.sql").write_text(SQL, encoding="utf-8")

    assert main(["convert", str(source), "-o", str(tmp_path / "out")]) == EXIT_CONVERTED

    assert capsys.readouterr().err == "sparkshift: converted 1 of 1 file\n"


def test_directory_applies_the_dialect_to_every_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "queries"
    source.mkdir()
    (source / "top.sql").write_text(
        "SELECT TOP 2 name FROM customers", encoding="utf-8"
    )
    output = tmp_path / "out"

    assert main(["convert", str(source), "-o", str(output), "-d", "tsql"]) == 0

    assert (output / "top.py").read_text(encoding="utf-8") == sparkshift.convert(
        "SELECT TOP 2 name FROM customers", dialect="tsql"
    ).code


def test_unreadable_file_in_a_directory_is_counted_as_not_converted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "queries"
    source.mkdir()
    locked = source / "locked.sql"
    locked.write_text(SQL, encoding="utf-8")
    locked.chmod(0)
    try:
        if os.access(locked, os.R_OK):
            pytest.skip("this user can read files without read permission")
        exit_code = main(["convert", str(source), "-o", str(tmp_path / "out")])
    finally:
        locked.chmod(0o644)

    assert exit_code == EXIT_NOT_CONVERTED
    assert f"cannot read {locked}: Permission denied" in capsys.readouterr().err


def usage_error(arguments: list[str], capsys: pytest.CaptureFixture[str]) -> str:
    """Run the command line, check it exits with a usage error, and return
    what it printed on standard error."""
    with pytest.raises(SystemExit) as caught:
        main(arguments)
    assert caught.value.code == EXIT_USAGE
    return capsys.readouterr().err


def test_directory_needs_an_output_directory(
    queries: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    err = usage_error(["convert", str(queries)], capsys)

    assert "needs --output DIRECTORY" in err


def test_directory_output_cannot_be_a_file(
    queries: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "out.py"
    output.write_text("", encoding="utf-8")

    err = usage_error(["convert", str(queries), "-o", str(output)], capsys)

    assert f"--output {output} must be a directory, not a file" in err


def test_directory_without_sql_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    err = usage_error(["convert", str(empty), "-o", str(tmp_path / "out")], capsys)

    assert f"no .sql files in {empty}" in err


def test_unwritable_output_directory_is_a_usage_error(
    queries: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "out"
    output.mkdir()
    (output / "a_orders.py").mkdir()  # a directory where a file must go

    err = usage_error(["convert", str(queries), "-o", str(output)], capsys)

    assert f"cannot write {output / 'a_orders.py'}" in err

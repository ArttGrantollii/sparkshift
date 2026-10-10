"""Tests for the command line, used exactly as a shell user would."""

import importlib
import io
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
    assert err == "sparkshift: warning: fallback used: x. Hint: Review it.\n"


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

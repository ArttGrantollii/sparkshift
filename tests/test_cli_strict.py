"""Strict conversion rejects warned artifacts before writing them."""

import io
import sys
from pathlib import Path

import pytest

from sparkshift import convert, to_notebook
from sparkshift.cli import main

WARNED = "proc sort data=customers out=unique_customers nodupkey; by id; run;"
CLEAN = "SELECT id FROM customers"


@pytest.mark.parametrize("notebook", [False, True])
@pytest.mark.parametrize("strict", [False, True])
def test_warned_stdin_policy(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    notebook: bool,
    strict: bool,
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(WARNED))
    args = ["convert", "-", "-d", "sas"]
    if notebook:
        args.append("--notebook")
    if strict:
        args.append("--strict")
    assert main(args) == (1 if strict else 0)
    out, err = capsys.readouterr()
    result = convert(WARNED, dialect="sas")
    assert result.warnings
    expected = to_notebook(result, dialect="sas") if notebook else result.code
    assert out == ("" if strict else expected)
    for warning in result.warnings:
        assert str(warning) in err
    assert ("--strict rejected" in err) == strict


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("notebook", [False, True])
def test_rejection_does_not_create_or_overwrite_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    existing: bool,
    notebook: bool,
) -> None:
    source = tmp_path / "job.sas"
    source.write_text(WARNED)
    output = tmp_path / "output" / "job.py"
    if existing:
        output.parent.mkdir()
        output.write_bytes(b"previous output\n")
    args = ["convert", str(source), "--strict", "-o", str(output)]
    if notebook:
        args.append("--notebook")
    assert main(args) == 1
    assert capsys.readouterr().out == ""
    assert source.read_text() == WARNED
    if existing:
        assert output.read_bytes() == b"previous output\n"
    else:
        assert not output.parent.exists()


@pytest.mark.parametrize("notebook", [False, True])
def test_strict_batch_writes_only_warning_free_results(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    notebook: bool,
) -> None:
    source = tmp_path / "inputs"
    source.mkdir()
    (source / "warned.sas").write_text(WARNED)
    (source / "clean.sql").write_text(CLEAN)
    output = tmp_path / "outputs"
    output.mkdir()
    (output / "warned.py").write_bytes(b"previous output\n")
    args = ["convert", str(source), "--strict", "-o", str(output)]
    if notebook:
        args.append("--notebook")
    assert main(args) == 1
    out, err = capsys.readouterr()
    result = convert(CLEAN)
    expected = to_notebook(result, source="clean.sql") if notebook else result.code
    assert (output / "clean.py").read_text() == expected
    assert (output / "warned.py").read_bytes() == b"previous output\n"
    assert out == ""
    assert "--strict rejected" in err
    assert "converted 1 of 2 files; 1 could not be converted" in err


@pytest.mark.parametrize("sql", [CLEAN, "SELECT * FROM customers"])
def test_warning_free_stdin_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    sql: str,
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(sql))
    assert main(["convert", "-", "--strict"]) == 0
    assert capsys.readouterr() == (convert(sql).code, "")


def test_strict_still_reports_unsupported_input(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("SELECT unknown_function(id) FROM t"))
    assert main(["convert", "-", "--strict"]) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "cannot convert" in err
    assert "--strict rejected" not in err

"""Batch output names are checked before conversion or filesystem writes."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

import sparkshift
from sparkshift.cli import EXIT_CONVERTED, EXIT_USAGE, main

SQL = "SELECT id FROM orders"
SAS = "data output; set orders; run;"


def make_source(tmp_path: Path, names: list[str]) -> Path:
    source = tmp_path / "jobs"
    source.mkdir()
    for name in names:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(SAS if path.suffix.lower() == ".sas" else SQL, encoding="utf-8")
    return source


@pytest.mark.parametrize("notebook", [False, True])
@pytest.mark.parametrize("existing_output", [False, True])
def test_same_stem_sql_and_sas_abort_the_entire_batch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    notebook: bool,
    existing_output: bool,
) -> None:
    source = make_source(tmp_path, ["a_clean.sql", "nested/job.sql", "nested/job.sas"])
    output = tmp_path / "generated"
    before = {}
    if existing_output:
        (output / "nested").mkdir(parents=True)
        before = {"a_clean.py": b"old clean output\n", "nested/job.py": b"keep this\n"}
        for name, data in before.items():
            (output / name).write_bytes(data)
    source_before = {p: p.read_bytes() for p in source.rglob("*") if p.is_file()}
    calls = []
    original = sparkshift.convert

    def conversion(sql: str, *, dialect: str | None):
        calls.append(sql)
        return original(sql, dialect=dialect)

    monkeypatch.setattr("sparkshift.cli.convert", conversion)
    arguments = ["convert", str(source), "-o", str(output)]
    if notebook:
        arguments.append("--notebook")
    with pytest.raises(SystemExit) as caught:
        main(arguments)
    assert caught.value.code == EXIT_USAGE
    stdout, stderr = capsys.readouterr()
    assert stdout == ""
    assert "output path" in stderr and "collid" in stderr
    for name in ["nested/job.sql", "nested/job.sas"]:
        assert str(source / name) in stderr
    assert str(output / "nested/job.py") in stderr
    assert "No files were written" in stderr
    assert "Rename" in stderr
    assert "converted" not in stderr
    assert calls == []
    assert {p: p.read_bytes() for p in source_before} == source_before
    if existing_output:
        assert {
            str(p.relative_to(output)): p.read_bytes()
            for p in output.rglob("*")
            if p.is_file()
        } == before
    else:
        assert not output.exists()


@pytest.mark.parametrize(
    "names",
    [
        ["job.SQL", "job.sql"],
        ["Job.sql", "job.sas"],
        ["Reports/job.sql", "reports/job.sas"],
    ],
)
def test_case_variants_are_rejected_portably(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], names: list[str]
) -> None:
    source = make_source(tmp_path, names)
    output = tmp_path / "generated"
    with pytest.raises(SystemExit) as caught:
        main(["convert", str(source), "-o", str(output)])
    assert caught.value.code == EXIT_USAGE
    error = capsys.readouterr().err
    assert "collid" in error
    for name in names:
        assert str(source / name) in error
    assert not output.exists()


@pytest.mark.parametrize(
    "names",
    [
        ["a.sql", "a.py/job.sas"],
        ["a.sql", "A.PY/job.sas"],
    ],
)
def test_a_destination_cannot_also_be_another_outputs_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], names: list[str]
) -> None:
    source = make_source(tmp_path, names)
    output = tmp_path / "generated"
    with pytest.raises(SystemExit) as caught:
        main(["convert", str(source), "-o", str(output)])
    assert caught.value.code == EXIT_USAGE
    error = capsys.readouterr().err
    assert "collid" in error
    for name in names:
        assert str(source / name) in error
    assert not output.exists()


def test_all_collisions_are_reported_in_deterministic_order(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    names = ["z.sql", "z.sas", "nested/a.sql", "nested/a.sas", "clean.sql"]
    source = make_source(tmp_path, names)
    output = tmp_path / "generated"
    arguments = ["convert", str(source), "-o", str(output)]
    errors = []
    for _ in range(2):
        with pytest.raises(SystemExit) as caught:
            main(arguments)
        assert caught.value.code == EXIT_USAGE
        errors.append(capsys.readouterr().err)
    assert errors[0] == errors[1]
    pairs = [
        f"{source / name} -> {output / Path(name).with_suffix('.py')}"
        for name in sorted(names)
        if name != "clean.sql"
    ]
    positions = [errors[0].index(pair) for pair in pairs]
    assert positions == sorted(positions)
    assert str(source / "clean.sql") not in errors[0]
    assert not output.exists()


@pytest.mark.parametrize("notebook", [False, True])
def test_same_stem_in_separate_subfolders_is_allowed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], notebook: bool
) -> None:
    source = make_source(tmp_path, ["sql/job.sql", "sas/job.sas"])
    output = tmp_path / "generated"
    arguments = ["convert", str(source), "-o", str(output)]
    if notebook:
        arguments.append("--notebook")
    assert main(arguments) == EXIT_CONVERTED
    for name, text, dialect in [
        ("sql/job.sql", SQL, None),
        ("sas/job.sas", SAS, "sas"),
    ]:
        result = sparkshift.convert(text, dialect=dialect)
        expected = (
            sparkshift.to_notebook(result, source=Path(name).name, dialect=dialect)
            if notebook
            else result.code
        )
        assert (output / Path(name).with_suffix(".py")).read_text(
            encoding="utf-8"
        ) == expected
    assert capsys.readouterr() == ("", "sparkshift: converted 2 of 2 files\n")


def test_sas_only_selection_has_no_collision_with_an_excluded_sql_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = make_source(tmp_path, ["job.sql", "job.sas"])
    output = tmp_path / "generated"
    assert (
        main(["convert", str(source), "-d", "sas", "-o", str(output)]) == EXIT_CONVERTED
    )
    assert (output / "job.py").read_text(encoding="utf-8") == sparkshift.convert(
        SAS, dialect="sas"
    ).code
    assert capsys.readouterr() == ("", "sparkshift: converted 1 of 1 file\n")


def test_report_still_assesses_both_inputs_without_generated_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = make_source(tmp_path, ["job.sql", "job.sas"])
    assert main(["report", str(source), "-f", "json"]) == EXIT_CONVERTED
    result = json.loads(capsys.readouterr().out)
    assert {file["path"] for file in result["files"]} == {"job.sql", "job.sas"}
    assert all(file["status"] == "converted" for file in result["files"])
    assert sorted(p.name for p in source.iterdir()) == ["job.sas", "job.sql"]


def test_installed_process_exits_with_usage_error_and_no_output(
    tmp_path: Path,
) -> None:
    source = make_source(tmp_path, ["job.sql", "job.sas"])
    output = tmp_path / "generated"
    result = subprocess.run(
        [sys.executable, "-m", "sparkshift", "convert", str(source), "-o", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == EXIT_USAGE
    assert result.stdout == ""
    assert "collid" in result.stderr
    assert not output.exists()


def test_similar_component_prefixes_are_distinct_output_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = make_source(tmp_path, ["a.sql", "a.py_extra/job.sas"])
    output = tmp_path / "generated"
    assert main(["convert", str(source), "-o", str(output)]) == EXIT_CONVERTED
    assert (output / "a.py").read_text(encoding="utf-8") == sparkshift.convert(SQL).code
    assert (output / "a.py_extra/job.py").read_text(
        encoding="utf-8"
    ) == sparkshift.convert(SAS, dialect="sas").code
    assert capsys.readouterr() == ("", "sparkshift: converted 2 of 2 files\n")

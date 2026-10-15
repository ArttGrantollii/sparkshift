"""Tests for ``sparkshift upload``, with a fake workspace: no network."""

import base64
import sys
from pathlib import Path
from unittest import mock

import pytest

import sparkshift
from sparkshift import databricks
from sparkshift.cli import EXIT_CONVERTED, EXIT_NOT_CONVERTED, EXIT_USAGE, main
from sparkshift.notebook import to_notebook

FOLDER = "/Workspace/Users/me@example.com/migrated"
TOKEN = "dapi-fake-0123456789abcdef"


class FakeWorkspace:
    """Records calls; ``existing`` paths raise as Databricks would."""

    def __init__(self, existing: set[str] = frozenset()) -> None:  # type: ignore[assignment]
        self.existing = set(existing)
        self.folders: list[str] = []
        self.notebooks: dict[str, tuple[str, bool]] = {}

    def mkdirs(self, path: str) -> None:
        self.folders.append(path)

    def import_notebook(self, path: str, source: str, *, overwrite: bool) -> None:
        if path in self.existing and not overwrite:
            raise databricks.NotebookExistsError(
                "a notebook already exists there; pass --overwrite to replace it"
            )
        self.notebooks[path] = (source, overwrite)


@pytest.fixture
def workspace(monkeypatch: pytest.MonkeyPatch) -> FakeWorkspace:
    fake = FakeWorkspace()
    monkeypatch.setattr(databricks, "connect", lambda profile=None: fake)
    return fake


@pytest.fixture
def jobs(tmp_path: Path) -> Path:
    """A directory of jobs: two that convert, one that does not."""
    root = tmp_path / "jobs"
    (root / "daily").mkdir(parents=True)
    (root / "daily" / "orders.sql").write_text("SELECT order_id FROM orders")
    (root / "customers.sas").write_text("data us; set customers; run;")
    (root / "broken.sql").write_text("SELECT my_udf(x) AS y FROM t")
    return root


def upload(*arguments: str) -> int:
    return main(["upload", *arguments])


def test_every_converted_file_becomes_a_notebook_at_its_relative_path(
    workspace: FakeWorkspace, jobs: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = upload(str(jobs), "--to", FOLDER)

    assert status == EXIT_NOT_CONVERTED  # broken.sql
    assert sorted(workspace.notebooks) == [
        f"{FOLDER}/customers",
        f"{FOLDER}/daily/orders",
    ]
    # Each folder is created once, before its notebooks.
    assert workspace.folders == [FOLDER, f"{FOLDER}/daily"]
    source, overwrite = workspace.notebooks[f"{FOLDER}/daily/orders"]
    expected = to_notebook(
        sparkshift.convert("SELECT order_id FROM orders"), source="orders.sql"
    )
    assert (source, overwrite) == (expected, False)
    customers, _ = workspace.notebooks[f"{FOLDER}/customers"]
    assert "(SAS)" in customers  # .sas files are SAS programs
    errors = capsys.readouterr().err
    assert f"cannot convert {jobs / 'broken.sql'}" in errors
    assert errors.endswith(
        "uploaded 2 of 3 files; 1 could not be converted or uploaded\n"
    )


def test_unreadable_files_are_skipped_and_folders_created_once(
    workspace: FakeWorkspace, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "a.sql").write_text("SELECT 1 AS one FROM t")
    (tmp_path / "b.sql").write_text("SELECT 2 AS two FROM t")
    (tmp_path / "latin1.sql").write_bytes("SELECT 'café' AS c FROM t".encode("latin-1"))

    assert upload(str(tmp_path), "--to", FOLDER) == EXIT_NOT_CONVERTED
    assert sorted(workspace.notebooks) == [f"{FOLDER}/a", f"{FOLDER}/b"]
    assert workspace.folders == [FOLDER]
    assert "it is not UTF-8 text" in capsys.readouterr().err


def test_uploaded_notebooks_start_at_the_given_catalog_and_schema(
    workspace: FakeWorkspace, jobs: Path
) -> None:
    path = jobs / "daily" / "orders.sql"
    arguments = [str(path), "--to", FOLDER, "--catalog", "main", "--schema", "sales"]

    assert upload(*arguments) == EXIT_CONVERTED

    source, _ = workspace.notebooks[f"{FOLDER}/orders"]
    assert source == to_notebook(
        sparkshift.convert(path.read_text()),
        source="orders.sql",
        catalog="main",
        schema="sales",
    )


def test_a_single_file_goes_into_the_folder(
    workspace: FakeWorkspace, jobs: Path
) -> None:
    status = upload(str(jobs / "daily" / "orders.sql"), "--to", FOLDER + "/")

    assert status == EXIT_CONVERTED
    assert list(workspace.notebooks) == [f"{FOLDER}/orders"]


def test_an_existing_notebook_is_kept_unless_overwrite_is_given(
    monkeypatch: pytest.MonkeyPatch, jobs: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeWorkspace(existing={f"{FOLDER}/customers"})
    monkeypatch.setattr(databricks, "connect", lambda profile=None: fake)
    (jobs / "broken.sql").unlink()

    assert upload(str(jobs), "--to", FOLDER) == EXIT_NOT_CONVERTED
    # The other notebook is still imported.
    assert list(fake.notebooks) == [f"{FOLDER}/daily/orders"]
    assert "pass --overwrite to replace it" in capsys.readouterr().err

    assert upload(str(jobs), "--to", FOLDER, "--overwrite") == EXIT_CONVERTED
    assert fake.notebooks[f"{FOLDER}/customers"][1] is True


def test_strict_mode_uploads_no_warned_conversion(
    workspace: FakeWorkspace, tmp_path: Path
) -> None:
    (tmp_path / "dedupe.sas").write_text(
        "proc sort data=customers out=unique nodupkey; by country; run;"
    )

    assert upload(str(tmp_path), "--to", FOLDER, "--strict") == EXIT_NOT_CONVERTED
    assert workspace.notebooks == {}


def test_profile_is_passed_to_the_connection(
    monkeypatch: pytest.MonkeyPatch, jobs: Path
) -> None:
    profiles = []

    def connect(profile: str | None = None) -> FakeWorkspace:
        profiles.append(profile)
        return FakeWorkspace()

    monkeypatch.setattr(databricks, "connect", connect)
    upload(str(jobs / "customers.sas"), "--to", FOLDER, "--profile", "free")

    assert profiles == ["free"]


def test_dry_run_lists_paths_without_connecting(
    monkeypatch: pytest.MonkeyPatch, jobs: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(databricks, "connect", mock.Mock(side_effect=AssertionError))
    # Not even the SDK is needed.
    monkeypatch.setitem(sys.modules, "databricks.sdk", None)

    status = upload(str(jobs), "--to", FOLDER, "--dry-run")

    assert status == EXIT_NOT_CONVERTED  # broken.sql
    output = capsys.readouterr()
    assert output.out.splitlines() == [
        f"{jobs / 'customers.sas'} -> {FOLDER}/customers",
        f"{jobs / 'daily' / 'orders.sql'} -> {FOLDER}/daily/orders",
    ]
    assert output.err.endswith("would upload 2 of 3 files; 1 could not be converted\n")


@pytest.mark.parametrize("folder", ["migrated", "/Workspace/../Shared", ""])
def test_the_folder_must_be_an_absolute_workspace_path(
    workspace: FakeWorkspace, jobs: Path, folder: str
) -> None:
    with pytest.raises(SystemExit) as caught:
        upload(str(jobs), "--to", folder)

    assert caught.value.code == EXIT_USAGE
    assert workspace.notebooks == {}


def test_a_missing_input_is_a_usage_error(
    workspace: FakeWorkspace, tmp_path: Path
) -> None:
    with pytest.raises(SystemExit) as caught:
        upload(str(tmp_path / "nothing.sql"), "--to", FOLDER)

    assert caught.value.code == EXIT_USAGE


def test_colliding_notebook_paths_upload_nothing(
    workspace: FakeWorkspace, tmp_path: Path
) -> None:
    # job.sql and job.sas would both be the notebook "job".
    (tmp_path / "job.sql").write_text("SELECT 1 AS one FROM t")
    (tmp_path / "job.sas").write_text("data a; set t; run;")

    with pytest.raises(SystemExit) as caught:
        upload(str(tmp_path), "--to", FOLDER)

    assert caught.value.code == EXIT_USAGE
    assert workspace.notebooks == {}


def test_a_failed_folder_is_retried_for_the_next_notebook(
    monkeypatch: pytest.MonkeyPatch, jobs: Path
) -> None:
    class FlakyWorkspace(FakeWorkspace):
        def mkdirs(self, path: str) -> None:
            super().mkdirs(path)
            if len(self.folders) == 1:
                raise databricks.UploadError("temporarily unavailable")

    fake = FlakyWorkspace()
    monkeypatch.setattr(databricks, "connect", lambda profile=None: fake)
    (jobs / "broken.sql").unlink()
    (jobs / "more.sql").write_text("SELECT 1 AS one FROM t")

    assert upload(str(jobs), "--to", FOLDER) == EXIT_NOT_CONVERTED
    # customers.sas fails with the folder; more.sql, in the same folder, retries it.
    assert fake.folders == [FOLDER, f"{FOLDER}/daily", FOLDER]
    assert sorted(fake.notebooks) == [f"{FOLDER}/daily/orders", f"{FOLDER}/more"]


# --- Connecting, and the Databricks SDK ---------------------------------------


def test_without_the_extra_upload_explains_how_to_install_it(
    monkeypatch: pytest.MonkeyPatch, jobs: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(sys.modules, "databricks.sdk", None)

    assert upload(str(jobs / "customers.sas"), "--to", FOLDER) == EXIT_USAGE
    assert "pip install 'sparkshift[databricks]'" in capsys.readouterr().err


def test_a_connection_error_never_shows_the_token(
    monkeypatch: pytest.MonkeyPatch, jobs: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABRICKS_TOKEN", TOKEN)

    def refuse(**settings: object) -> None:
        raise ValueError(f"cannot use token {TOKEN} for this host")

    monkeypatch.setattr("databricks.sdk.WorkspaceClient", refuse)

    assert upload(str(jobs / "customers.sas"), "--to", FOLDER) == EXIT_USAGE
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err
    assert "cannot use token *** for this host" in output.err


def test_connect_uses_the_sdk_with_the_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    client = mock.Mock()
    workspace_client = mock.Mock(return_value=client)
    monkeypatch.setattr("databricks.sdk.WorkspaceClient", workspace_client)

    workspace = databricks.connect("free")

    workspace_client.assert_called_once_with(profile="free")
    workspace.mkdirs("/Workspace/a")
    client.workspace.mkdirs.assert_called_once_with("/Workspace/a")


def test_a_notebook_is_imported_as_python_source() -> None:
    from databricks.sdk.service.workspace import ImportFormat, Language

    client = mock.Mock()
    workspace = databricks._SdkWorkspace(client)
    source = "# Databricks notebook source\nresult = spark.table('t')\n"

    workspace.import_notebook("/Workspace/a/job", source, overwrite=True)

    client.workspace.import_.assert_called_once_with(
        "/Workspace/a/job",
        content=base64.b64encode(source.encode()).decode(),
        format=ImportFormat.SOURCE,
        language=Language.PYTHON,
        overwrite=True,
    )


def test_sdk_errors_become_safe_upload_errors() -> None:
    from databricks.sdk.errors import DatabricksError, ResourceAlreadyExists

    client = mock.Mock()
    client.config.token = TOKEN
    workspace = databricks._SdkWorkspace(client)

    client.workspace.import_.side_effect = ResourceAlreadyExists("exists")
    with pytest.raises(databricks.NotebookExistsError, match="--overwrite"):
        workspace.import_notebook("/Workspace/a/job", "x", overwrite=False)

    client.workspace.import_.side_effect = DatabricksError(f"bad token {TOKEN}")
    with pytest.raises(databricks.UploadError) as caught:
        workspace.import_notebook("/Workspace/a/job", "x", overwrite=False)
    assert str(caught.value) == "bad token ***"

    client.workspace.mkdirs.side_effect = DatabricksError("no permission")
    with pytest.raises(databricks.UploadError, match="no permission"):
        workspace.mkdirs("/Workspace/a")


def test_redact_replaces_every_secret_and_ignores_tiny_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "s3cr3t-value")
    monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)

    text = "token abcdef-long, prefix abcdef, secret s3cr3t-value, digit 1"

    assert databricks.redact(text, ["abcdef-long", "abcdef", "1"]) == (
        "token ***, prefix ***, secret ***, digit 1"
    )

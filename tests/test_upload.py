"""Tests for ``sparkshift upload``, with a fake workspace: no network."""

import base64
import sys
from pathlib import Path
from unittest import mock

import pytest

import sparkshift
from sparkshift import databricks
from sparkshift.cli import EXIT_CONVERTED, EXIT_NOT_CONVERTED, EXIT_USAGE, main
from sparkshift.jobs import job_settings
from sparkshift.notebook import to_notebook
from sparkshift.pipelines import pipeline_settings, to_pipeline

FOLDER = "/Workspace/Users/me@example.com/migrated"
TOKEN = "dapi-fake-0123456789abcdef"


class FakeWorkspace:
    """Records calls; ``existing`` paths raise as Databricks would."""

    def __init__(self, existing: set[str] = frozenset()) -> None:  # type: ignore[assignment]
        self.existing = set(existing)
        self.folders: list[str] = []
        self.notebooks: dict[str, tuple[str, bool]] = {}
        self.files: dict[str, tuple[str, bool]] = {}
        self.jobs = {}
        self.pipelines = {}

    def mkdirs(self, path: str) -> None:
        self.folders.append(path)

    def import_notebook(self, path: str, source: str, *, overwrite: bool) -> None:
        if path in self.existing and not overwrite:
            raise databricks.NotebookExistsError(
                "a notebook already exists there; pass --overwrite to replace it"
            )
        self.notebooks[path] = (source, overwrite)

    def import_file(self, path: str, source: str, *, overwrite: bool) -> None:
        if path in self.existing and not overwrite:
            raise databricks.UploadError(
                "a file already exists there; pass --overwrite to replace it"
            )
        self.files[path] = (source, overwrite)

    # Pipelines, as jobs: ``pipelines`` maps IDs to settings.
    pipelines: dict[str, dict]

    def pipelines_named(self, name: str) -> list[str]:
        self.fail_if_asked()
        return [
            key for key, settings in self.pipelines.items() if settings["name"] == name
        ]

    def create_pipeline(self, settings: dict) -> str:
        self.fail_if_asked()
        pipeline_id = f"p{len(self.pipelines) + 1}"
        self.pipelines[pipeline_id] = settings
        return pipeline_id

    def edit_pipeline(self, pipeline_id: str, settings: dict) -> None:
        self.fail_if_asked()
        self.pipelines[pipeline_id] = settings

    # Jobs: ``jobs`` maps IDs to settings; ``job_error`` fails every call.
    jobs: dict[int, dict]
    job_error: str | None = None

    def jobs_named(self, name: str) -> list[int]:
        self.fail_if_asked()
        return [
            job_id for job_id, settings in self.jobs.items() if settings["name"] == name
        ]

    def create_job(self, settings: dict) -> int:
        self.fail_if_asked()
        job_id = 100 + len(self.jobs)
        self.jobs[job_id] = settings
        return job_id

    def reset_job(self, job_id: int, settings: dict) -> None:
        self.fail_if_asked()
        self.jobs[job_id] = settings

    def fail_if_asked(self) -> None:
        if self.job_error:
            raise databricks.UploadError(self.job_error)


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
    arguments += ["--output-catalog", "lake", "--output-schema", "reports"]

    assert upload(*arguments) == EXIT_CONVERTED

    source, _ = workspace.notebooks[f"{FOLDER}/orders"]
    assert source == to_notebook(
        sparkshift.convert(path.read_text()),
        source="orders.sql",
        catalog="main",
        schema="sales",
        output_catalog="lake",
        output_schema="reports",
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


# --- Jobs ------------------------------------------------------------------------


@pytest.fixture
def converting(jobs: Path) -> Path:
    """The jobs directory without the file that does not convert."""
    (jobs / "broken.sql").unlink()
    return jobs


def test_a_job_runs_the_uploaded_notebooks_in_order(
    workspace: FakeWorkspace, converting: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    arguments = [str(converting), "--to", FOLDER, "--job"]
    arguments += ["--schema", "raw", "--output-schema", "claims"]

    assert upload(*arguments) == EXIT_CONVERTED

    assert workspace.jobs == {
        100: job_settings(
            "sparkshift migrated",
            FOLDER,
            [f"{FOLDER}/customers", f"{FOLDER}/daily/orders"],
            schema="raw",
            output_schema="claims",
        )
    }
    assert capsys.readouterr().err.endswith(
        "created the job 'sparkshift migrated' (ID 100): runs 2 notebooks in order\n"
    )


def test_a_job_can_be_named(workspace: FakeWorkspace, converting: Path) -> None:
    assert upload(str(converting), "--to", FOLDER, "--job", "nightly claims") == 0

    assert [settings["name"] for settings in workspace.jobs.values()] == [
        "nightly claims"
    ]


def test_no_job_without_every_file(
    workspace: FakeWorkspace, jobs: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert upload(str(jobs), "--to", FOLDER, "--job") == EXIT_NOT_CONVERTED

    # The notebooks that convert are uploaded; a job missing a step is not made.
    assert len(workspace.notebooks) == 2
    assert workspace.jobs == {}
    assert "no job created: it needs every file converted and uploaded" in (
        capsys.readouterr().err
    )


def test_an_existing_job_is_kept_unless_overwrite_is_given(
    workspace: FakeWorkspace, converting: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace.jobs[7] = {"name": "sparkshift migrated", "tasks": []}

    assert upload(str(converting), "--to", FOLDER, "--job") == EXIT_NOT_CONVERTED
    assert workspace.jobs[7]["tasks"] == []
    assert "a job named 'sparkshift migrated' already exists (ID 7); pass " in (
        capsys.readouterr().err
    )

    assert upload(str(converting), "--to", FOLDER, "--job", "--overwrite") == 0
    assert list(workspace.jobs) == [7]
    assert len(workspace.jobs[7]["tasks"]) == 2
    assert "updated the job 'sparkshift migrated' (ID 7)" in capsys.readouterr().err


def test_no_guess_between_jobs_of_the_same_name(
    workspace: FakeWorkspace, converting: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace.jobs.update({1: {"name": "x"}, 2: {"name": "x"}})

    arguments = [str(converting), "--to", FOLDER, "--job", "x", "--overwrite"]

    assert upload(*arguments) == EXIT_NOT_CONVERTED
    assert workspace.jobs == {1: {"name": "x"}, 2: {"name": "x"}}
    assert "2 jobs are named 'x'" in capsys.readouterr().err


def test_a_job_that_cannot_be_created_is_reported(
    workspace: FakeWorkspace, converting: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace.job_error = "permission denied"

    assert upload(str(converting), "--to", FOLDER, "--job") == EXIT_NOT_CONVERTED
    assert (
        "cannot create the job 'sparkshift migrated': permission denied"
        in capsys.readouterr().err
    )


def test_dry_run_shows_the_job(
    monkeypatch: pytest.MonkeyPatch,
    converting: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(databricks, "connect", mock.Mock(side_effect=AssertionError))

    status = upload(str(converting), "--to", FOLDER, "--job", "--dry-run")

    assert status == EXIT_CONVERTED
    assert capsys.readouterr().out.splitlines()[-1] == (
        "job 'sparkshift migrated': customers -> daily_orders"
    )


def test_jobs_are_created_with_the_create_body() -> None:
    client = mock.Mock()
    client.api_client.do.return_value = {"job_id": 12}
    workspace = databricks._SdkWorkspace(client)
    settings = {"name": "x", "tasks": []}

    assert workspace.create_job(settings) == 12
    workspace.reset_job(12, settings)

    assert client.api_client.do.call_args_list == [
        mock.call("POST", "/api/2.2/jobs/create", body=settings),
        mock.call(
            "POST", "/api/2.2/jobs/reset", body={"job_id": 12, "new_settings": settings}
        ),
    ]


def test_jobs_are_matched_by_their_exact_name() -> None:
    client = mock.Mock()
    named = [
        mock.Mock(job_id=1, settings=mock.Mock()),
        mock.Mock(job_id=2, settings=mock.Mock()),
    ]
    named[0].settings.name = "Claims"
    named[1].settings.name = "claims"
    client.jobs.list.return_value = named

    assert databricks._SdkWorkspace(client).jobs_named("claims") == [2]
    client.jobs.list.assert_called_once_with(name="claims")


def test_job_errors_never_show_the_token() -> None:
    from databricks.sdk.errors import DatabricksError

    client = mock.Mock()
    client.config.token = TOKEN
    client.api_client.do.side_effect = DatabricksError(f"denied for {TOKEN}")
    client.jobs.list.side_effect = DatabricksError(f"denied for {TOKEN}")
    workspace = databricks._SdkWorkspace(client)

    for call in (lambda: workspace.create_job({}), lambda: workspace.jobs_named("x")):
        with pytest.raises(databricks.UploadError) as caught:
            call()
        assert str(caught.value) == "denied for ***"


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


# --- Pipelines ------------------------------------------------------------------------

# Tables are read from raw: the fixture orders.sql reads a table named orders,
# which in a pipeline would otherwise be its own dataset.
PIPELINE_TARGET = ["--schema", "raw", "--output-catalog", "main"]
PIPELINE_TARGET += ["--output-schema", "claims"]


def test_a_pipeline_is_made_of_the_uploaded_source_files(
    workspace: FakeWorkspace, converting: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    arguments = [str(converting), "--to", FOLDER, "--pipeline"]

    assert upload(*arguments, *PIPELINE_TARGET) == EXIT_CONVERTED

    customers = sparkshift.convert("data us; set customers; run;", dialect="sas")
    orders = sparkshift.convert("SELECT order_id FROM orders")
    assert workspace.notebooks == {}
    assert workspace.files == {
        f"{FOLDER}/customers.py": (
            to_pipeline(customers, source="customers.sas", dialect="sas", schema="raw"),
            False,
        ),
        f"{FOLDER}/daily/orders.py": (
            to_pipeline(orders, source="orders.sql", schema="raw"),
            False,
        ),
    }
    assert workspace.pipelines == {
        "p1": pipeline_settings(
            "sparkshift migrated",
            FOLDER,
            [f"{FOLDER}/customers.py", f"{FOLDER}/daily/orders.py"],
            catalog="main",
            schema="claims",
        )
    }
    assert capsys.readouterr().err.endswith(
        "created the pipeline 'sparkshift migrated' (ID p1): 2 source files, tables "
        "in main.claims; start it under Jobs & Pipelines\n"
    )


@pytest.mark.parametrize(
    ("options", "message"),
    [
        (["--pipeline"], "--pipeline needs --output-catalog and --output-schema"),
        (
            ["--pipeline", "--output-schema", "claims"],
            "--pipeline needs --output-catalog and --output-schema",
        ),
        (
            [
                "--pipeline",
                "--catalog",
                "c",
                "--output-catalog",
                "c",
                "--output-schema",
                "s",
            ],
            "--catalog needs --schema",
        ),
        (["--pipeline", "--job", *PIPELINE_TARGET], "not allowed with argument"),
    ],
)
def test_pipeline_upload_options(
    converting: Path,
    capsys: pytest.CaptureFixture[str],
    options: list[str],
    message: str,
) -> None:
    with pytest.raises(SystemExit) as caught:
        upload(str(converting), "--to", FOLDER, *options)

    assert caught.value.code == EXIT_USAGE
    assert message in capsys.readouterr().err


def test_no_pipeline_without_every_file(
    workspace: FakeWorkspace, jobs: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = upload(str(jobs), "--to", FOLDER, "--pipeline", *PIPELINE_TARGET)

    assert status == EXIT_NOT_CONVERTED
    assert len(workspace.files) == 2
    assert workspace.pipelines == {}
    assert "no pipeline created: it needs every file converted and uploaded" in (
        capsys.readouterr().err
    )


def test_files_that_define_the_same_dataset_make_no_pipeline(
    workspace: FakeWorkspace, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "jobs"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir()
    (root / "a" / "daily.sql").write_text("SELECT x FROM t")
    (root / "b" / "daily.sql").write_text("SELECT y FROM u")

    status = upload(str(root), "--to", FOLDER, "--pipeline", *PIPELINE_TARGET)

    assert status == EXIT_NOT_CONVERTED
    assert workspace.files == {}
    assert workspace.pipelines == {}
    assert "its dataset daily is also defined by" in capsys.readouterr().err


def test_an_existing_pipeline_is_kept_unless_overwrite_is_given(
    workspace: FakeWorkspace, converting: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace.pipelines["old"] = {"name": "sparkshift migrated", "libraries": []}
    arguments = [str(converting), "--to", FOLDER, "--pipeline", *PIPELINE_TARGET]

    assert upload(*arguments) == EXIT_NOT_CONVERTED
    assert workspace.pipelines["old"]["libraries"] == []
    assert "a pipeline named 'sparkshift migrated' already exists (ID old)" in (
        capsys.readouterr().err
    )

    assert upload(*arguments, "--overwrite") == EXIT_CONVERTED
    assert list(workspace.pipelines) == ["old"]
    assert len(workspace.pipelines["old"]["libraries"]) == 2
    assert "updated the pipeline 'sparkshift migrated' (ID old)" in (
        capsys.readouterr().err
    )


def test_no_guess_between_pipelines_of_the_same_name(
    workspace: FakeWorkspace, converting: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace.pipelines.update({"1": {"name": "x"}, "2": {"name": "x"}})
    arguments = [str(converting), "--to", FOLDER, "--pipeline", "x", "--overwrite"]

    assert upload(*arguments, *PIPELINE_TARGET) == EXIT_NOT_CONVERTED
    assert workspace.pipelines == {"1": {"name": "x"}, "2": {"name": "x"}}
    assert "2 pipelines are named 'x'" in capsys.readouterr().err


def test_a_pipeline_that_cannot_be_created_is_reported(
    workspace: FakeWorkspace, converting: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace.job_error = "permission denied"
    arguments = [str(converting), "--to", FOLDER, "--pipeline", *PIPELINE_TARGET]

    assert upload(*arguments) == EXIT_NOT_CONVERTED
    assert (
        "cannot create the pipeline 'sparkshift migrated': permission denied"
        in capsys.readouterr().err
    )


def test_a_program_that_cannot_be_a_pipeline_is_not_uploaded(
    workspace: FakeWorkspace, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "report.sas"
    path.write_text("proc print data=t; run;")

    status = upload(str(path), "--to", FOLDER, "--pipeline", *PIPELINE_TARGET)

    assert status == EXIT_NOT_CONVERTED
    assert workspace.files == {}
    assert "a pipeline of a program that creates no data set" in capsys.readouterr().err


def test_dry_run_shows_the_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    converting: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(databricks, "connect", mock.Mock(side_effect=AssertionError))
    arguments = [str(converting), "--to", FOLDER, "--pipeline", "--dry-run"]

    assert upload(*arguments, *PIPELINE_TARGET) == EXIT_CONVERTED
    assert capsys.readouterr().out.splitlines() == [
        f"{converting / 'customers.sas'} -> {FOLDER}/customers.py",
        f"{converting / 'daily' / 'orders.sql'} -> {FOLDER}/daily/orders.py",
        "pipeline 'sparkshift migrated': 2 source files, tables in main.claims",
    ]


def test_pipelines_are_created_and_edited_with_the_create_body() -> None:
    client = mock.Mock()
    client.api_client.do.return_value = {"pipeline_id": "abc"}
    workspace = databricks._SdkWorkspace(client)
    settings = {"name": "x", "libraries": []}

    assert workspace.create_pipeline(settings) == "abc"
    workspace.edit_pipeline("abc", settings)

    assert client.api_client.do.call_args_list == [
        mock.call("POST", "/api/2.0/pipelines", body=settings),
        mock.call("PUT", "/api/2.0/pipelines/abc", body={**settings, "id": "abc"}),
    ]


def test_pipelines_are_matched_by_their_exact_name() -> None:
    client = mock.Mock()
    found = [mock.Mock(pipeline_id="1"), mock.Mock(pipeline_id="2")]
    found[0].name = "Claims"
    found[1].name = "claims"
    client.pipelines.list_pipelines.return_value = found

    assert databricks._SdkWorkspace(client).pipelines_named("claims") == ["2"]


def test_pipeline_source_files_are_imported_as_files() -> None:
    from databricks.sdk.errors import ResourceAlreadyExists
    from databricks.sdk.service.workspace import ImportFormat

    client = mock.Mock()
    workspace = databricks._SdkWorkspace(client)

    workspace.import_file("/a/b.py", "x = 1\n", overwrite=True)

    client.workspace.import_.assert_called_once_with(
        "/a/b.py",
        content=base64.b64encode(b"x = 1\n").decode("ascii"),
        format=ImportFormat.RAW,
        overwrite=True,
    )
    client.workspace.import_.side_effect = ResourceAlreadyExists("exists")
    with pytest.raises(databricks.UploadError, match="a file already exists there"):
        workspace.import_file("/a/b.py", "x = 1\n", overwrite=False)


def test_pipeline_errors_never_show_the_token() -> None:
    from databricks.sdk.errors import DatabricksError

    client = mock.Mock()
    client.config.token = TOKEN
    client.api_client.do.side_effect = DatabricksError(f"denied for {TOKEN}")
    client.pipelines.list_pipelines.side_effect = DatabricksError(f"denied for {TOKEN}")
    client.workspace.import_.side_effect = DatabricksError(f"denied for {TOKEN}")
    workspace = databricks._SdkWorkspace(client)

    for call in (
        lambda: workspace.create_pipeline({}),
        lambda: workspace.edit_pipeline("x", {}),
        lambda: workspace.pipelines_named("x"),
        lambda: workspace.import_file("/a.py", "", overwrite=False),
    ):
        with pytest.raises(databricks.UploadError) as caught:
            call()
        assert str(caught.value) == "denied for ***"

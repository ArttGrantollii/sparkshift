"""Tests for the job that runs uploaded notebooks, and the notebook that
creates it in a workspace (no network: the Databricks SDK is faked)."""

import ast
import sys
import types
from typing import Any

import pytest

import sparkshift
from sparkshift.jobs import (
    create_job_notebook,
    default_job_name,
    job_settings,
    task_keys,
)
from sparkshift.notebook import CELL_SEPARATOR

FOLDER = "/Workspace/Users/me@example.com/claims"
NOTEBOOKS = [f"{FOLDER}/01_load", f"{FOLDER}/02_flag", f"{FOLDER}/legacy/03 report"]


def test_the_job_runs_each_notebook_after_the_one_before() -> None:
    settings = job_settings(
        "sparkshift claims",
        FOLDER,
        NOTEBOOKS,
        catalog="workspace",
        schema="raw",
        output_schema="claims",
    )

    assert settings == {
        "name": "sparkshift claims",
        "tasks": [
            {
                "task_key": "01_load",
                "notebook_task": {"notebook_path": NOTEBOOKS[0], "source": "WORKSPACE"},
                "environment_key": "serverless",
            },
            {
                "task_key": "02_flag",
                "notebook_task": {"notebook_path": NOTEBOOKS[1], "source": "WORKSPACE"},
                "environment_key": "serverless",
                "depends_on": [{"task_key": "01_load"}],
            },
            {
                "task_key": "legacy_03_report",
                "notebook_task": {"notebook_path": NOTEBOOKS[2], "source": "WORKSPACE"},
                "environment_key": "serverless",
                "depends_on": [{"task_key": "02_flag"}],
            },
        ],
        # Version 4 is the first serverless environment with PySpark 4.
        "environments": [
            {"environment_key": "serverless", "spec": {"environment_version": "4"}}
        ],
        "parameters": [
            {"name": "catalog", "default": "workspace"},
            {"name": "schema", "default": "raw"},
            {"name": "output_catalog", "default": ""},
            {"name": "output_schema", "default": "claims"},
        ],
        "max_concurrent_runs": 1,
        "tags": {"sparkshift": sparkshift.__version__},
    }


def test_the_default_name_is_the_folders() -> None:
    assert default_job_name(FOLDER) == "sparkshift claims"


def test_task_keys_are_valid_and_unique() -> None:
    long = "x" * 120
    notebooks = [
        f"{FOLDER}/a b",
        f"{FOLDER}/a_b",
        f"{FOLDER}/ünï/çode",
        f"{FOLDER}/{long}",
        f"{FOLDER}/{long}y",
    ]

    keys = task_keys(FOLDER, notebooks)

    assert keys[:3] == ["a_b", "a_b_2", "_n___ode"]
    assert keys[3] == "x" * 100
    assert keys[4] == "x" * 98 + "_2"
    assert len(set(keys)) == len(keys)


# --- The notebook that creates the job -----------------------------------------


class FakeJob:
    def __init__(self, job_id: int, name: str) -> None:
        self.job_id = job_id
        self.settings = types.SimpleNamespace(name=name)


class FakeClient:
    """The few WorkspaceClient calls the notebook makes."""

    def __init__(self, jobs: list[FakeJob]) -> None:
        self.requests: list[tuple[str, str, dict]] = []
        self.jobs = types.SimpleNamespace(
            list=lambda name: [
                job for job in jobs if job.settings.name.lower() == name.lower()
            ]
        )
        self.api_client = types.SimpleNamespace(do=self.do)

    def do(self, method: str, path: str, body: dict) -> dict:
        self.requests.append((method, path, body))
        return {"job_id": 77} if path.endswith("/create") else {}


def run_create_job_notebook(
    monkeypatch: pytest.MonkeyPatch, notebook: str, jobs: list[FakeJob]
) -> tuple[FakeClient, str]:
    client = FakeClient(jobs)
    sdk = types.ModuleType("databricks.sdk")
    sdk.WorkspaceClient = lambda: client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "databricks", types.ModuleType("databricks"))
    monkeypatch.setitem(sys.modules, "databricks.sdk", sdk)
    printed: list[str] = []
    scope: dict[str, Any] = {"print": printed.append}
    exec(compile(notebook, "Create job", "exec"), scope)
    return client, "\n".join(printed)


SETTINGS = job_settings("sparkshift claims", FOLDER, NOTEBOOKS)


def test_the_notebook_holds_exactly_the_jobs_settings() -> None:
    notebook = create_job_notebook(SETTINGS)

    cells = notebook.split(f"\n\n{CELL_SEPARATOR}\n\n")
    assert cells[1].startswith("settings = ")
    assert ast.literal_eval(cells[1].removeprefix("settings = ")) == SETTINGS
    # The description lists the notebooks, in order.
    for number, path in enumerate(NOTEBOOKS, 1):
        assert f"# MAGIC {number}. `{path}`" in cells[0]


def test_the_notebook_creates_the_job(monkeypatch: pytest.MonkeyPatch) -> None:
    client, printed = run_create_job_notebook(
        monkeypatch,
        create_job_notebook(SETTINGS),
        # Same name in other case: the API returns it, but it is another job.
        [FakeJob(5, "SparkShift Claims")],
    )

    assert client.requests == [("POST", "/api/2.2/jobs/create", SETTINGS)]
    assert printed == "Created the job 'sparkshift claims' (job ID 77)."


def test_the_notebook_updates_a_job_of_the_same_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, printed = run_create_job_notebook(
        monkeypatch, create_job_notebook(SETTINGS), [FakeJob(9, "sparkshift claims")]
    )

    assert client.requests == [
        ("POST", "/api/2.2/jobs/reset", {"job_id": 9, "new_settings": SETTINGS})
    ]
    assert printed == "Updated the job 'sparkshift claims' (job ID 9)."


def test_the_notebook_does_not_guess_between_jobs_of_the_same_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    twins = [FakeJob(1, "sparkshift claims"), FakeJob(2, "sparkshift claims")]

    with pytest.raises(RuntimeError, match="2 jobs are named 'sparkshift claims'"):
        run_create_job_notebook(monkeypatch, create_job_notebook(SETTINGS), twins)


@pytest.mark.parametrize(
    "name",
    [
        "line\nbreak",
        f"x\n{CELL_SEPARATOR}\nraise RuntimeError('injected')",
        "quote ' and \" and `tick`",
    ],
)
def test_any_name_stays_a_value(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    settings = job_settings(name, FOLDER, [f"{FOLDER}/{name}"])

    notebook = create_job_notebook(settings)

    # A separator only starts a cell on a line of its own.
    assert notebook.splitlines().count(CELL_SEPARATOR) == 2
    client, _ = run_create_job_notebook(monkeypatch, notebook, [])
    assert client.requests == [("POST", "/api/2.2/jobs/create", settings)]

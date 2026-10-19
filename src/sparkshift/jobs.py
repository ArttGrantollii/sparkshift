"""A Databricks job that runs uploaded notebooks in order (``upload --job``).

The job is described once, as the JSON body of the Jobs API's ``create``
request (:func:`job_settings`), and reaches Databricks in one of two ways:
``sparkshift upload --job`` sends it, or :func:`create_job_notebook` writes a
notebook that sends it when run in the workspace. The playground uses the
notebook, because browsers may not call the Jobs API: Databricks does not
allow it from other sites (CORS).

The job runs one task per notebook, each after the one before, on serverless
compute in environment version 4, the first with a PySpark 4 client (see
docs/databricks-validation.md). The notebooks' widgets are also the job's
parameters, so a run can read or write other tables. Nothing here imports
the Databricks SDK.
"""

import pprint
import re
from collections.abc import Sequence
from pathlib import PurePosixPath

from sparkshift.notebook import CELL_SEPARATOR, HEADER, code_span, markdown_cell

ENVIRONMENT_VERSION = "4"
_ENVIRONMENT_KEY = "serverless"
_PARAMETERS = ("catalog", "schema", "output_catalog", "output_schema")
# The Jobs API's limit on task keys.
_MAX_TASK_KEY = 100


def default_job_name(folder: str) -> str:
    """The job's name when none is given: after the workspace folder."""
    return f"sparkshift {PurePosixPath(folder).name}"


def job_settings(
    name: str,
    folder: str,
    notebooks: Sequence[str],
    *,
    catalog: str = "",
    schema: str = "",
    output_catalog: str = "",
    output_schema: str = "",
) -> dict:
    """The Jobs API ``create`` body for a job named ``name`` that runs the
    ``notebooks`` (workspace paths under ``folder``) in order. The widget
    values become the job's parameters."""
    from sparkshift import __version__

    keys = task_keys(folder, notebooks)
    tasks: list[dict] = []
    for position, (path, key) in enumerate(zip(notebooks, keys, strict=True)):
        task: dict = {
            "task_key": key,
            "notebook_task": {"notebook_path": path, "source": "WORKSPACE"},
            "environment_key": _ENVIRONMENT_KEY,
        }
        if position:
            task["depends_on"] = [{"task_key": keys[position - 1]}]
        tasks.append(task)
    values = (catalog, schema, output_catalog, output_schema)
    return {
        "name": name,
        "tasks": tasks,
        "environments": [
            {
                "environment_key": _ENVIRONMENT_KEY,
                "spec": {"environment_version": ENVIRONMENT_VERSION},
            }
        ],
        "parameters": [
            {"name": parameter, "default": value}
            for parameter, value in zip(_PARAMETERS, values, strict=True)
        ],
        # Runs write tables: one at a time.
        "max_concurrent_runs": 1,
        "tags": {"sparkshift": __version__},
    }


def task_keys(folder: str, notebooks: Sequence[str]) -> list[str]:
    """A task key for each notebook: its path under ``folder``, with
    characters other than letters, digits, ``_`` and ``-`` replaced by ``_``,
    unique and at most 100 characters long."""
    keys: list[str] = []
    for path in notebooks:
        relative = PurePosixPath(path).relative_to(folder).as_posix()
        base = re.sub(r"[^A-Za-z0-9_-]", "_", relative)[:_MAX_TASK_KEY]
        key, number = base, 2
        while key in keys:
            suffix = f"_{number}"
            key = base[: _MAX_TASK_KEY - len(suffix)] + suffix
            number += 1
        keys.append(key)
    return keys


def create_job_notebook(settings: dict) -> str:
    """A notebook that creates the job, or updates the one of the same name,
    when run in the workspace, with the notebook's own credentials."""
    name = settings["name"]
    notebooks = [task["notebook_task"]["notebook_path"] for task in settings["tasks"]]
    description = [
        f"# Create job: {name}",
        "",
        "Run this notebook once (**Run all**) to create the job "
        f"{code_span(name)}, which runs these notebooks in order on serverless "
        f"compute (environment version {ENVIRONMENT_VERSION}). If a job of that "
        "name exists, it is updated instead. The job then appears under "
        "**Jobs & Pipelines**, where you can run it or give it a schedule.",
        "",
        *(f"{number}. {code_span(path)}" for number, path in enumerate(notebooks, 1)),
        "",
        "Written by SparkShift. The job's parameters `catalog` and `schema` "
        "choose where the notebooks read tables, and `output_catalog` and "
        "`output_schema` where they write their results.",
    ]
    settings_cell = "settings = " + pprint.pformat(settings, sort_dicts=False, width=88)
    create_cell = "\n".join(
        [
            "from databricks.sdk import WorkspaceClient",
            "",
            "client = WorkspaceClient()",
            'name = settings["name"]',
            "# The API matches names ignoring case: keep the exact ones.",
            "existing = [",
            "    job.job_id",
            "    for job in client.jobs.list(name=name)",
            "    if job.settings is None or job.settings.name == name",
            "]",
            "if len(existing) > 1:",
            "    raise RuntimeError(",
            '        f"{len(existing)} jobs are named {name!r}: delete or rename all "',
            '        "but one, then run this notebook again."',
            "    )",
            "if existing:",
            "    job_id = existing[0]",
            "    reset = {'job_id': job_id, 'new_settings': settings}",
            '    client.api_client.do("POST", "/api/2.2/jobs/reset", body=reset)',
            '    print(f"Updated the job {name!r} (job ID {job_id}).")',
            "else:",
            "    created = client.api_client.do(",
            '        "POST", "/api/2.2/jobs/create", body=settings',
            "    )",
            '    job_id = created["job_id"]',
            '    print(f"Created the job {name!r} (job ID {job_id}).")',
        ]
    )
    cells = [markdown_cell(description), settings_cell, create_cell]
    body = f"\n\n{CELL_SEPARATOR}\n\n".join(cells)
    return f"{HEADER}\n{body}\n"

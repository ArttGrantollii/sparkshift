"""Generated code as a pipeline source file, for Lakeflow pipelines on
Databricks (see docs/adr/0013-pipelines-from-the-same-plan.md). Apache
Spark's own Spark Declarative Pipelines run these files too, but cannot yet
run them safely: see the ADR.

A pipeline source file defines datasets with the decorators of
``pyspark.pipelines``, imported as ``dp``: each function returns a DataFrame,
and the pipeline keeps it as a materialized view (a table), or as a temporary
view that only the pipeline reads. The pipeline works out the order to
compute them from the tables each function reads, and gives the file a
SparkSession named ``spark``.

The datasets are the tables a notebook would write (see
:mod:`sparkshift.notebook`): each data set a SAS program creates, as a
materialized view of the same name, or a SQL query's result, named after its
file. A data set the program creates again, as PROC SORT without OUT= does,
is first a temporary view, which the next version reads.

Each function computes its dataset as the plain code does, except that it
reads the program's other data sets by name, ``spark.table("name")``, which
the pipeline resolves to its own datasets. Other tables are read from the
pipeline's default catalog and schema, or from the ``catalog`` and ``schema``
given here, which qualify each name that does not name its own.

A pipeline is described once, as the JSON body of the Pipelines API's
``create`` request (:func:`pipeline_settings`), which reaches Databricks as
a job's does (see :mod:`sparkshift.jobs`): ``sparkshift upload --pipeline``
sends it, or :func:`create_pipeline_notebook` writes a notebook that sends it
from inside the workspace, for the playground, which browsers may not send.
"""

import pprint
import textwrap
from collections import Counter
from collections.abc import Sequence
from pathlib import PurePath, PurePosixPath

from sparkshift import ir
from sparkshift.diagnostics import ConversionResult, Diagnostic
from sparkshift.dialects import is_sas
from sparkshift.emit import PipelineDataset, emit_pipeline
from sparkshift.errors import UnsupportedSQLError
from sparkshift.notebook import (
    CELL_SEPARATOR,
    HEADER,
    code_span,
    language_name,
    markdown_cell,
    table_name,
)

_WIDTH = 88


def to_pipeline(
    result: ConversionResult,
    *,
    source: str | None = None,
    dialect: str | None = None,
    catalog: str = "",
    schema: str = "",
) -> str:
    """A pipeline source file that defines the datasets of ``result``, a
    result of :func:`sparkshift.convert`.

    ``source`` names the file the code was converted from, which names a SQL
    query's dataset, and ``dialect`` is the language it was converted from,
    as given to ``convert`` ("sas" for SAS). Tables the program reads without
    a schema are read from ``schema`` (in ``catalog``, if given), or from the
    pipeline's default schema.

    Raises:
        UnsupportedSQLError: the program cannot be a pipeline: it creates no
            data set, or it reads a table of the same name as a dataset it
            defines, which in a pipeline would be the dataset itself.
        ValueError: ``catalog`` without ``schema``, or a ``result`` that does
            not come from ``convert``.
    """
    datasets, outside = _datasets(result, source, dialect, catalog, schema)
    _check(datasets, outside)
    header = _header(result, source, dialect, datasets, catalog, schema)
    return header + "\n\n" + emit_pipeline(datasets)


def pipeline_tables(
    result: ConversionResult, *, source: str | None = None, dialect: str | None = None
) -> list[str]:
    """The names of the datasets the pipeline source file of ``result``
    defines, in order: its materialized views and temporary views."""
    datasets, _ = _datasets(result, source, dialect, "", "")
    return [dataset.name for dataset in datasets]


def _datasets(
    result: ConversionResult,
    source: str | None,
    dialect: str | None,
    catalog: str,
    schema: str,
) -> tuple[list[PipelineDataset], set[str]]:
    """The datasets of ``result``'s pipeline, and the unqualified names of
    the tables they read from outside it, lower case."""
    if catalog and not schema:
        raise ValueError("a catalog needs a schema to read tables from")
    if is_sas(dialect) and not result._plans and result._printed is not None:
        raise UnsupportedSQLError(
            [
                Diagnostic(
                    "a pipeline of a program that creates no data set",
                    PurePath(source).name if source else "the program",
                    "A pipeline keeps data sets as tables, and PROC PRINT only "
                    "shows one: create it with a DATA step, or with OUT= on a "
                    "procedure.",
                )
            ]
        )
    if not result._plans:
        raise ValueError("to_pipeline needs a result returned by sparkshift.convert")

    # Unqualified names of the tables read from outside the pipeline.
    outside: set[str] = set()

    def read(node: ir.Relation) -> ir.Relation | None:
        if isinstance(node, ir.Named) and node in names:
            return ir.TableScan((names[node],))
        if isinstance(node, ir.TableScan):
            parts = _qualified(node.name_parts, catalog, schema)
            if len(parts) == 1:
                outside.add(parts[0].lower())
            return ir.TableScan(parts)
        return None

    names: dict[ir.Named, str] = {}
    if is_sas(dialect):
        plans = [plan for plan in result._plans if isinstance(plan, ir.Named)]
        names = _dataset_names(plans)
        datasets = [
            PipelineDataset(
                name,
                ir.map_relations(plan.source, read),
                temporary=name != plan.name,
            )
            for plan, name in names.items()
        ]
    else:
        [plan] = result._plans
        datasets = [PipelineDataset(table_name(source), ir.map_relations(plan, read))]
    return datasets, outside


def _check(datasets: list[PipelineDataset], outside: set[str]) -> None:
    """Reject datasets a pipeline cannot run as the plain code does."""
    # Run-time helper names come from the input's columns (ADR 0008), which
    # a pipeline may not look at while it defines a dataset.
    looking = [dataset.name for dataset in datasets if _binds_helpers(dataset.relation)]
    if looking:
        raise UnsupportedSQLError(
            [
                Diagnostic(
                    "a helper column named after the input's columns, in a pipeline",
                    name,
                    "A pipeline may not look at a DataFrame's columns while it "
                    "defines a dataset, and SparkShift cannot see them. In SAS, "
                    "list the SET data set's columns with KEEP=; in SQL, select "
                    "columns by name rather than with *.",
                )
                for name in looking
            ]
        )

    defined = {dataset.name.lower() for dataset in datasets}
    conflicts = sorted(outside & defined)
    if conflicts:
        raise UnsupportedSQLError(
            [
                Diagnostic(
                    f"a pipeline that defines {name} and reads a table named {name}",
                    name,
                    f"In a pipeline, {name} is the pipeline's own dataset. Read "
                    "the table from its schema (convert with --schema), or give "
                    "the data set another name.",
                )
                for name in conflicts
            ]
        )


def _dataset_names(plans: list[ir.Named]) -> dict[ir.Named, str]:
    """The pipeline name of each data set a SAS program creates, in order:
    its own name for the last version of each name, and ``name_v1``,
    ``name_v2``, ... for the versions it replaces. Equal data sets are one."""
    last = {plan.name: plan for plan in plans}
    taken = set(last)
    versions: Counter[str] = Counter()
    names: dict[ir.Named, str] = {}
    for plan in plans:
        if plan in names:
            continue
        name = plan.name
        if last[name] == plan:
            names[plan] = name
            continue
        versions[name] += 1
        candidate, number = f"{name}_v{versions[name]}", 2
        while candidate in taken:
            candidate, number = f"{name}_v{versions[name]}_{number}", number + 1
        taken.add(candidate)
        names[plan] = candidate
    return names


def _binds_helpers(relation: ir.Relation) -> bool:
    """Whether the code that computes a relation binds helper names when it
    runs, from the columns of its input."""
    found: list[ir.Relation] = []
    ir.map_relations(
        relation,
        lambda node: (
            found.append(node) if isinstance(node, ir.AllocateHelpers) else None
        ),
    )
    return bool(found)


def _qualified(parts: tuple[str, ...], catalog: str, schema: str) -> tuple[str, ...]:
    """A table's name parts, read from ``schema`` and ``catalog`` when it
    names neither."""
    if not schema:
        return parts
    if len(parts) == 1:
        return (catalog, schema, *parts) if catalog else (schema, *parts)
    if len(parts) == 2 and catalog:
        return (catalog, *parts)
    return parts


def _header(
    result: ConversionResult,
    source: str | None,
    dialect: str | None,
    datasets: list[PipelineDataset],
    catalog: str,
    schema: str,
) -> str:
    from sparkshift import __version__

    origin = PurePath(source).name if source else "code"
    views = [dataset.name for dataset in datasets if not dataset.temporary]
    temporary = [dataset.name for dataset in datasets if dataset.temporary]
    defines = f"It defines the materialized {_noun('view', views)} {_names(views)}"
    if temporary:
        defines += (
            f", and the temporary {_noun('view', temporary)} {_names(temporary)}, "
            "which only the pipeline reads"
        )
    location = ".".join(part for part in (catalog, schema) if part)
    reads = (
        f"Tables without a schema are read from {location}."
        if location
        else "Tables without a schema are read from the pipeline's default "
        "catalog and schema."
    )
    paragraphs = [
        f"Pipeline source converted from {origin} ({language_name(dialect)}) by "
        f"SparkShift {__version__}.",
        f"{defines}. Run it in a Lakeflow pipeline on Databricks "
        "(docs/pipelines.md in SparkShift's repository). " + reads,
    ]
    if result._printed is not None:
        paragraphs.append(
            "PROC PRINT shows a data set and creates none, so a pipeline has "
            "nothing to keep from it: query the materialized views instead."
        )
    lines: list[str] = []
    for paragraph in paragraphs:
        lines += [*_comment(paragraph), "#"]
    if result.warnings:
        lines.append("# Review before running:")
        for warning in result.warnings:
            lines += _comment(f"- {warning}", indent="#   ")
        lines.append("#")
    return "\n".join(lines[:-1])


def _comment(text: str, indent: str = "# ") -> list[str]:
    """Text as comment lines: every line of it, newlines included, starts
    with ``#``."""
    return textwrap.wrap(
        " ".join(text.split()),
        width=_WIDTH,
        initial_indent="# ",
        subsequent_indent=indent,
        break_long_words=False,
        break_on_hyphens=False,
    )


def _noun(noun: str, names: list[str]) -> str:
    return noun if len(names) == 1 else f"{noun}s"


def _names(names: list[str]) -> str:
    if len(names) <= 2:
        return " and ".join(names)
    return ", ".join(names[:-1]) + ", and " + names[-1]


# --- Deploying ------------------------------------------------------------------------


def default_pipeline_name(folder: str) -> str:
    """The pipeline's name when none is given: after the workspace folder."""
    return f"sparkshift {PurePosixPath(folder).name}"


def pipeline_settings(
    name: str, folder: str, files: Sequence[str], *, catalog: str, schema: str
) -> dict:
    """The Pipelines API ``create`` body for a pipeline named ``name`` whose
    source files are ``files`` (workspace paths under ``folder``), keeping
    its tables in ``catalog``.``schema``, on serverless compute. It runs when
    started, not continuously."""
    from sparkshift import __version__

    return {
        "name": name,
        "catalog": catalog,
        "schema": schema,
        "serverless": True,
        "continuous": False,
        "libraries": [{"file": {"path": path}} for path in files],
        "root_path": folder,
        "tags": {"sparkshift": __version__},
    }


def create_pipeline_notebook(settings: dict) -> str:
    """A notebook that creates the pipeline, or updates the one of the same
    name, when run in the workspace, with the notebook's own credentials."""
    name = settings["name"]
    files = [library["file"]["path"] for library in settings["libraries"]]
    target = f"{settings['catalog']}.{settings['schema']}"
    description = [
        f"# Create pipeline: {name}",
        "",
        "Run this notebook once (**Run all**) to create the Lakeflow pipeline "
        f"{code_span(name)}, which keeps its tables in {code_span(target)} and "
        "runs on serverless compute. If a pipeline of that name exists, it is "
        "updated instead. The pipeline then appears under **Jobs & Pipelines**, "
        "where you can start it or give it a schedule.",
        "",
        "Its source files:",
        "",
        *(f"{number}. {code_span(path)}" for number, path in enumerate(files, 1)),
        "",
        "Written by SparkShift.",
    ]
    settings_cell = "settings = " + pprint.pformat(settings, sort_dicts=False, width=88)
    create_cell = "\n".join(
        [
            "from databricks.sdk import WorkspaceClient",
            "",
            "client = WorkspaceClient()",
            'name = settings["name"]',
            "existing = [",
            "    pipeline.pipeline_id",
            "    for pipeline in client.pipelines.list_pipelines()",
            "    if pipeline.name == name",
            "]",
            "if len(existing) > 1:",
            "    raise RuntimeError(",
            '        f"{len(existing)} pipelines are named {name!r}: delete or "',
            '        "rename all but one, then run this notebook again."',
            "    )",
            "if existing:",
            "    pipeline_id = existing[0]",
            "    edit = {**settings, 'id': pipeline_id}",
            '    path = f"/api/2.0/pipelines/{pipeline_id}"',
            '    client.api_client.do("PUT", path, body=edit)',
            '    print(f"Updated the pipeline {name!r} (ID {pipeline_id}).")',
            "else:",
            '    path = "/api/2.0/pipelines"',
            '    created = client.api_client.do("POST", path, body=settings)',
            '    pipeline_id = created["pipeline_id"]',
            '    print(f"Created the pipeline {name!r} (ID {pipeline_id}).")',
        ]
    )
    cells = [markdown_cell(description), settings_cell, create_cell]
    body = f"\n\n{CELL_SEPARATOR}\n\n".join(cells)
    return f"{HEADER}\n{body}\n"

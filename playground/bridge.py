"""The playground's link to SparkShift, run in the browser by Pyodide.

The page calls ``convert_for_page`` and gets JSON it can show without knowing
SparkShift's exception types. Errors carry the same text the command line
prints, so the playground and the CLI always agree.

For many files, ``plan_for_page`` chooses the files and their output paths
with the command line's rules, the page converts them one at a time, and
``zip_for_page`` packs the results. To deploy them, ``notebook_for_page``
writes each notebook with its widgets' values and ``job_for_page`` the
notebook that creates their job, as ``sparkshift upload --job`` would.
"""

import base64
import io
import json
import zipfile
from pathlib import PurePosixPath

import sparkshift
from sparkshift.batch import colliding, dialect_of, source_suffixes
from sparkshift.dialects import DIALECT_CHOICES
from sparkshift.jobs import create_job_notebook, default_job_name, job_settings
from sparkshift.notebook import markdown_text, notebook_cells

# Playground policies, not limits imposed by the library or CLI.
MAX_SOURCE_BYTES = 50_000
MAX_BATCH_FILES = 200

# Every entry gets this time, so the same files always make the same zip.
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


def convert_for_page(sql: str, dialect: str) -> str:
    """Convert ``sql`` (``dialect`` "" for generic SQL, "sas" for a SAS
    program) and return JSON:

    ``{"ok": true, "code": ..., "warnings": [...]}`` on success, or
    ``{"ok": false, "error": ..., "issues": [...]}`` when it cannot be
    converted; ``issues`` lists each unsupported construct separately.
    """
    return _convert(sql, dialect, notebook=False)


def notebook_for_page(
    sql: str,
    dialect: str,
    source: str | None = None,
    catalog: str = "",
    schema: str = "",
    output_catalog: str = "",
    output_schema: str = "",
) -> str:
    """As ``convert_for_page``, with the code written as a Databricks
    notebook, exactly as ``sparkshift convert --notebook`` writes it;
    ``source`` is the name of the file it came from, for the title, and the
    other values are the initial values of its widgets. The JSON also has
    ``cells``, each ``{"kind": ..., "title": ..., "text": ...}``, where
    ``text`` is a Markdown cell's Markdown, or the code of another."""
    locations = {
        "catalog": catalog,
        "schema": schema,
        "output_catalog": output_catalog,
        "output_schema": output_schema,
    }
    return _convert(sql, dialect, notebook=True, source=source, locations=locations)


def job_for_page(
    name: str,
    folder: str,
    notebooks: str,
    catalog: str,
    schema: str,
    output_catalog: str,
    output_schema: str,
) -> str:
    """The notebook that creates the job running ``notebooks`` (a JSON list
    of workspace paths under ``folder``) in order, as ``sparkshift upload
    --job`` would create it; ``name`` empty for the default name. Return JSON
    ``{"ok": true, "name": ..., "notebook": ...}``, or ``{"ok": false,
    "error": ..., "issues": []}``."""
    paths = json.loads(notebooks)
    inside = folder + "/"
    if not (
        folder.startswith("/")
        and _is_relative_path(folder[1:])
        and all(
            isinstance(path, str)
            and path.startswith(inside)
            and _is_relative_path(path[len(inside) :])
            for path in paths
        )
    ):
        return _failure(
            "The workspace folder must be an absolute path, such as "
            "/Workspace/Users/you@example.com/sparkshift."
        )
    settings = job_settings(
        name.strip() or default_job_name(folder),
        folder,
        paths,
        catalog=catalog,
        schema=schema,
        output_catalog=output_catalog,
        output_schema=output_schema,
    )
    return json.dumps(
        {
            "ok": True,
            "name": settings["name"],
            "notebook": create_job_notebook(settings),
        }
    )


def plan_for_page(paths: str, dialect: str, notebook: bool) -> str:
    """Choose the files to convert from ``paths``, a JSON list of the
    relative paths of the files a visitor chose, as the command line would
    for a folder. Return JSON:

    ``{"ok": true, "folder": ..., "files": [...], "skipped": n}``, where
    ``folder`` names the zip and the folder in it, and each file has the
    ``index`` of its path in ``paths``, its ``name`` and ``output`` paths
    relative to that folder, and its ``dialect``; ``skipped`` counts the
    files that are not converted (other suffixes). Or ``{"ok": false,
    "error": ..., "issues": [], "collisions": [...]}``, where
    ``collisions`` lists the files whose outputs would have the same path.
    """
    chosen = json.loads(paths)
    if not all(_is_relative_path(path) for path in chosen):
        return _failure("Some of the chosen files have paths that cannot be used.")
    parts = [path.split("/") for path in chosen]
    # A chosen folder: every path starts with its name.
    root = parts[0][0] if parts else None
    if not all(len(path) > 1 and path[0] == root for path in parts):
        root = None
    names = [PurePosixPath(*(path[1:] if root else path)) for path in parts]

    suffixes = source_suffixes(dialect or None)
    selected = sorted(
        (index for index, name in enumerate(names) if name.suffix.lower() in suffixes),
        key=lambda index: names[index],
    )
    if not selected:
        return _failure(f"There are no {' or '.join(suffixes)} files to convert.")
    if len(selected) > MAX_BATCH_FILES:
        return _failure(
            f"The browser demo converts up to {MAX_BATCH_FILES} files at a time; "
            f"{len(selected)} were chosen. Use the CLI for larger folders."
        )
    outputs = [names[index].with_suffix(".py") for index in selected]
    conflicts = colliding(outputs)
    if conflicts:
        return _failure(
            "Some files would be converted to the same path, ignoring case. "
            "Nothing was converted. Rename the inputs or place them in separate "
            "folders so their output paths are distinct.",
            collisions=[
                {"name": str(names[index]), "output": str(outputs[position])}
                for position, index in enumerate(selected)
                if position in conflicts
            ],
        )
    kind = "notebooks" if notebook else "pyspark"
    return json.dumps(
        {
            "ok": True,
            "folder": f"{root or 'converted'}_{kind}",
            "files": [
                {
                    "index": index,
                    "name": str(names[index]),
                    "output": str(outputs[position]),
                    "dialect": dialect_of(names[index], dialect or None) or "",
                }
                for position, index in enumerate(selected)
            ],
            "skipped": len(chosen) - len(selected),
        }
    )


def zip_for_page(folder: str, files: str) -> str:
    """A zip of converted files: ``files`` is a JSON list of ``{"output":
    ..., "code": ...}``, each stored at ``folder/output`` as UTF-8, as the
    command line writes it. Return JSON ``{"ok": true, "zip": ...}`` with the
    zip in base64, or ``{"ok": false, "error": ..., "issues": []}``."""
    entries = json.loads(files)
    paths = [f"{folder}/{entry['output']}" for entry in entries]
    if not all(_is_relative_path(path) for path in paths):
        return _failure("Some of the converted files have paths that cannot be used.")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, entry in zip(paths, entries, strict=True):
            info = zipfile.ZipInfo(path, date_time=_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, entry["code"].encode("utf-8"))
    return json.dumps(
        {"ok": True, "zip": base64.b64encode(buffer.getvalue()).decode("ascii")}
    )


def _is_relative_path(path: object) -> bool:
    """Whether ``path`` is a plain relative path: no empty, "." or ".."
    components, which could escape the folder a zip is extracted into."""
    return isinstance(path, str) and all(
        part not in ("", ".", "..") and "\\" not in part for part in path.split("/")
    )


def _failure(error: str, **details: object) -> str:
    return json.dumps({"ok": False, "error": error, "issues": [], **details})


def _convert(
    sql: str,
    dialect: str,
    *,
    notebook: bool,
    source: str | None = None,
    locations: dict[str, str] | None = None,
) -> str:
    if len(sql.encode("utf-8")) > MAX_SOURCE_BYTES:
        return json.dumps(
            {
                "ok": False,
                "error": f"The browser demo accepts up to {MAX_SOURCE_BYTES:,} "
                "UTF-8 bytes of source code. Use the CLI for larger files.",
                "issues": [],
            }
        )
    try:
        result = sparkshift.convert(sql, dialect=dialect or None)
    except RecursionError:
        return json.dumps(
            {
                "ok": False,
                "error": "This input is too deeply nested for the browser converter. "
                "Simplify expressions or split it into smaller queries.",
                "issues": [],
            }
        )
    except sparkshift.SparkShiftError as error:
        issues = getattr(error, "issues", ())
        return json.dumps(
            {
                "ok": False,
                "error": str(error),
                "issues": [_issue(issue) for issue in issues],
            }
        )
    converted: dict[str, object] = {
        "ok": True,
        "code": result.code,
        "warnings": [str(warning) for warning in result.warnings],
    }
    if notebook:
        options = {"source": source, "dialect": dialect or None, **(locations or {})}
        converted["code"] = sparkshift.to_notebook(result, **options)
        converted["cells"] = [
            {
                "kind": cell.kind,
                "title": cell.title,
                "text": markdown_text(cell.source)
                if cell.kind == "markdown"
                else cell.source,
            }
            for cell in notebook_cells(result, **options)
        ]
    return json.dumps(converted)


def about() -> str:
    """JSON with the SparkShift version and the dialects the page can offer,
    SAS last."""
    return json.dumps(
        {
            "version": sparkshift.__version__,
            "dialects": list(DIALECT_CHOICES),
            "max_source_bytes": MAX_SOURCE_BYTES,
            "max_batch_files": MAX_BATCH_FILES,
        }
    )


def _issue(issue: sparkshift.Diagnostic) -> dict[str, str | None]:
    return {"message": issue.message, "sql": issue.sql, "hint": issue.hint}

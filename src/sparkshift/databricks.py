"""Import generated notebooks, or pipeline source files, into a Databricks
workspace (``sparkshift upload``), and create their job or pipeline.

This needs the optional extra: ``pip install 'sparkshift[databricks]'``. Only
this module uses the Databricks SDK, and only inside its functions, so the rest
of SparkShift never imports it.

Credentials come only from the SDK's unified authentication: the
``DATABRICKS_HOST`` and ``DATABRICKS_TOKEN`` environment variables, a profile
in ``~/.databrickscfg``, or another method the SDK supports. SparkShift never
takes a token as an argument, stores one, or prints one: every message from
the SDK passes through :func:`redact` first.
"""

import base64
import os
from collections.abc import Iterable
from typing import Any, Protocol

INSTALL_HINT = "pip install 'sparkshift[databricks]'"

# Environment variables that can hold a Databricks secret.
_SECRET_VARIABLES = (
    "DATABRICKS_TOKEN",
    "DATABRICKS_CLIENT_SECRET",
    "DATABRICKS_PASSWORD",
)


class UploadError(Exception):
    """An upload failed; the message is safe to print."""


class MissingExtraError(UploadError):
    """The Databricks SDK is not installed."""


class NotebookExistsError(UploadError):
    """A notebook is already at the path, and overwriting was not requested."""


class Workspace(Protocol):
    """What ``sparkshift upload`` needs from a workspace."""

    def mkdirs(self, path: str) -> None:
        """Create a folder and its parents; nothing happens if it exists."""

    def import_notebook(self, path: str, source: str, *, overwrite: bool) -> None:
        """Import a source-format Python notebook at ``path``."""

    def import_file(self, path: str, source: str, *, overwrite: bool) -> None:
        """Import a file, such as a pipeline source file, at ``path``."""

    def jobs_named(self, name: str) -> list[int]:
        """The IDs of the jobs named ``name``."""

    def create_job(self, settings: dict) -> int:
        """Create a job from a Jobs API ``create`` body; its ID."""

    def reset_job(self, job_id: int, settings: dict) -> None:
        """Replace a job's settings with a Jobs API ``create`` body."""

    def pipelines_named(self, name: str) -> list[str]:
        """The IDs of the pipelines named ``name``."""

    def create_pipeline(self, settings: dict) -> str:
        """Create a pipeline from a Pipelines API ``create`` body; its ID."""

    def edit_pipeline(self, pipeline_id: str, settings: dict) -> None:
        """Replace a pipeline's settings with a Pipelines API ``create`` body."""


def connect(profile: str | None = None) -> Workspace:
    """A workspace, authenticated by the SDK's unified authentication, or the
    named profile of ``~/.databrickscfg``."""
    try:
        from databricks.sdk import WorkspaceClient
    except ImportError:
        raise MissingExtraError(
            f"uploading needs the Databricks SDK: {INSTALL_HINT}"
        ) from None
    try:
        client = WorkspaceClient(profile=profile)
    except Exception as error:  # the SDK raises several types for bad settings
        raise UploadError(
            f"cannot connect to Databricks: {redact(str(error))}"
        ) from None
    return _SdkWorkspace(client)


class _SdkWorkspace:
    """A :class:`Workspace` backed by the Databricks SDK."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def mkdirs(self, path: str) -> None:
        from databricks.sdk.errors import DatabricksError

        try:
            self.client.workspace.mkdirs(path)
        except DatabricksError as error:
            raise UploadError(self.message(error)) from None

    def import_notebook(self, path: str, source: str, *, overwrite: bool) -> None:
        from databricks.sdk.errors import DatabricksError, ResourceAlreadyExists
        from databricks.sdk.service.workspace import ImportFormat, Language

        try:
            self.client.workspace.import_(
                path,
                content=base64.b64encode(source.encode("utf-8")).decode("ascii"),
                format=ImportFormat.SOURCE,
                language=Language.PYTHON,
                overwrite=overwrite,
            )
        except ResourceAlreadyExists:
            raise NotebookExistsError(
                "a notebook already exists there; pass --overwrite to replace it"
            ) from None
        except DatabricksError as error:
            raise UploadError(self.message(error)) from None

    def import_file(self, path: str, source: str, *, overwrite: bool) -> None:
        from databricks.sdk.errors import DatabricksError, ResourceAlreadyExists
        from databricks.sdk.service.workspace import ImportFormat

        try:
            self.client.workspace.import_(
                path,
                content=base64.b64encode(source.encode("utf-8")).decode("ascii"),
                format=ImportFormat.RAW,
                overwrite=overwrite,
            )
        except ResourceAlreadyExists:
            raise UploadError(
                "a file already exists there; pass --overwrite to replace it"
            ) from None
        except DatabricksError as error:
            raise UploadError(self.message(error)) from None

    def jobs_named(self, name: str) -> list[int]:
        from databricks.sdk.errors import DatabricksError

        try:
            # The API matches names exactly, ignoring case: keep exact ones.
            return [
                job.job_id
                for job in self.client.jobs.list(name=name)
                if job.settings is None or job.settings.name == name
            ]
        except DatabricksError as error:
            raise UploadError(self.message(error)) from None

    def create_job(self, settings: dict) -> int:
        created = self.request("/api/2.2/jobs/create", settings)
        return int(created["job_id"])

    def reset_job(self, job_id: int, settings: dict) -> None:
        self.request(
            "/api/2.2/jobs/reset", {"job_id": job_id, "new_settings": settings}
        )

    def pipelines_named(self, name: str) -> list[str]:
        from databricks.sdk.errors import DatabricksError

        try:
            return [
                pipeline.pipeline_id
                for pipeline in self.client.pipelines.list_pipelines()
                if pipeline.name == name
            ]
        except DatabricksError as error:
            raise UploadError(self.message(error)) from None

    def create_pipeline(self, settings: dict) -> str:
        created = self.request("/api/2.0/pipelines", settings)
        return str(created["pipeline_id"])

    def edit_pipeline(self, pipeline_id: str, settings: dict) -> None:
        body = {**settings, "id": pipeline_id}
        self.request(f"/api/2.0/pipelines/{pipeline_id}", body, method="PUT")

    def request(self, path: str, body: dict, method: str = "POST") -> Any:
        """Send ``body`` to a REST API path: the same JSON the notebook that
        creates a job or a pipeline sends (see sparkshift.jobs and
        sparkshift.pipelines)."""
        from databricks.sdk.errors import DatabricksError

        try:
            return self.client.api_client.do(method, path, body=body)
        except DatabricksError as error:
            raise UploadError(self.message(error)) from None

    def message(self, error: Exception) -> str:
        token = getattr(getattr(self.client, "config", None), "token", None)
        return redact(str(error), [token] if isinstance(token, str) else [])


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """``text`` with every known secret replaced by ``***``: the given ones,
    and those in the environment variables Databricks reads."""
    values = {*secrets, *(os.environ.get(name, "") for name in _SECRET_VARIABLES)}
    for value in sorted(values, key=len, reverse=True):
        # Ignore tiny values: replacing "1" everywhere would garble the text.
        if len(value) >= 4:
            text = text.replace(value, "***")
    return text

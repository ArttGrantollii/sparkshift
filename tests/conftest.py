"""Shared pytest fixtures."""

import os
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pyspark.sql import SparkSession

# SPARKSHIFT_SPARK_MODE=connect runs the Spark tests through Spark Connect, as
# Databricks serverless compute runs code; it needs `uv sync --group connect`.
SPARK_MODES = ("classic", "connect")


def spark_mode() -> str:
    mode = os.environ.get("SPARKSHIFT_SPARK_MODE", "classic")
    if mode not in SPARK_MODES:
        raise ValueError(f"SPARKSHIFT_SPARK_MODE must be one of {SPARK_MODES}")
    return mode


@pytest.fixture(scope="session")
def spark() -> Iterator["SparkSession"]:
    """One local SparkSession shared by the whole test run.

    Starting Spark launches a JVM and takes several seconds, so it is created
    once per session. The configuration favors fast, deterministic tests:

    - local[1]: a single worker thread, so execution is predictable.
    - shuffle.partitions=1: the default (200) is tuned for clusters and only
      adds overhead on tiny test datasets.
    - ui.enabled=false: no web UI server or port conflicts (classic mode).
    - session.timeZone=UTC: timestamp results do not depend on the machine.

    In connect mode, a local Spark Connect server runs the same engine, and
    the tests talk to it through the Spark Connect client.
    """
    # Imported here, not at module level, so `pytest -m "not spark"` never
    # loads PySpark or starts a JVM.
    from pyspark.sql import SparkSession

    builder = (
        SparkSession.builder.remote("local[1]")
        if spark_mode() == "connect"
        else SparkSession.builder.master("local[1]")
    )
    builder = (
        builder.appName("sparkshift-tests")
        .config("spark.sql.shuffle.partitions", "1")
        .config("spark.sql.session.timeZone", "UTC")
    )
    if spark_mode() == "classic":
        # A Spark Connect client cannot change this server setting.
        builder = builder.config("spark.ui.enabled", "false")
    session = builder.getOrCreate()
    yield session
    session.stop()

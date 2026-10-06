"""Shared pytest fixtures."""

from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pyspark.sql import SparkSession


@pytest.fixture(scope="session")
def spark() -> Iterator["SparkSession"]:
    """One local SparkSession shared by the whole test run.

    Starting Spark launches a JVM and takes several seconds, so it is created
    once per session. The configuration favors fast, deterministic tests:

    - local[1]: a single worker thread, so execution is predictable.
    - shuffle.partitions=1: the default (200) is tuned for clusters and only
      adds overhead on tiny test datasets.
    - ui.enabled=false: no web UI server or port conflicts.
    - session.timeZone=UTC: timestamp results do not depend on the machine.
    """
    # Imported here, not at module level, so `pytest -m "not spark"` never
    # loads PySpark or starts a JVM.
    from pyspark.sql import SparkSession

    session = (
        SparkSession.builder.master("local[1]")
        .appName("sparkshift-tests")
        .config("spark.sql.shuffle.partitions", "1")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.session.timeZone", "UTC")
        .getOrCreate()
    )
    yield session
    session.stop()

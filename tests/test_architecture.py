"""Architecture rules that must hold for the whole package."""

import subprocess
import sys
import textwrap

# Modules the core must never import. SparkShift generates PySpark code; it
# never runs Spark. Keeping these out of the core keeps the runtime small and
# lets the package run in a browser (Pyodide), where no JVM exists.
FORBIDDEN_RUNTIME_MODULES = ("pyspark", "py4j")


def test_core_does_not_import_spark() -> None:
    # Runs in a fresh interpreter: other tests in this pytest process may have
    # already imported pyspark, which would make an in-process check depend on
    # test order.
    script = textwrap.dedent(
        f"""
        import importlib
        import pkgutil
        import sys

        import sparkshift

        for module in pkgutil.walk_packages(sparkshift.__path__, "sparkshift."):
            importlib.import_module(module.name)

        loaded = sorted(
            {{
                name.split(".")[0]
                for name in sys.modules
                if name.split(".")[0] in {FORBIDDEN_RUNTIME_MODULES!r}
            }}
        )
        print(",".join(loaded))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "", (
        f"sparkshift imported forbidden runtime modules: {result.stdout.strip()}"
    )

"""Architecture rules that must hold for the whole package."""

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

import sparkshift

PACKAGE_DIR = Path(sparkshift.__file__).parent

# Modules allowed to import SQLGlot directly. Keeping SQLGlot confined to the
# frontend limits the impact of its API changes (see ADR 0001 and ADR 0003).
SQLGLOT_FRONTEND = frozenset({"parsing", "translate"})

# Modules that must not depend on SQLGlot at all, even indirectly. The emitter
# and IR are shared by every frontend, including the planned SAS frontend.
SQLGLOT_FREE = frozenset({"ir", "emit", "diagnostics", "errors"})


def imported_modules(module: str) -> set[str]:
    """Return the modules a sparkshift module imports, read from its source."""
    tree = ast.parse((PACKAGE_DIR / f"{module}.py").read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module == "sparkshift":
            # "from sparkshift import ir" imports the module sparkshift.ir.
            names.update(f"sparkshift.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def all_modules() -> list[str]:
    return sorted(path.stem for path in PACKAGE_DIR.glob("*.py"))


def test_only_the_frontend_imports_sqlglot() -> None:
    importers = {
        module
        for module in all_modules()
        if any(name.split(".")[0] == "sqlglot" for name in imported_modules(module))
    }

    assert importers <= SQLGLOT_FRONTEND


def test_ir_and_emitter_never_depend_on_sqlglot() -> None:
    for module in SQLGLOT_FREE:
        for name in imported_modules(module):
            assert name.split(".")[0] != "sqlglot", f"{module} imports {name}"
            if name.startswith("sparkshift."):
                dependency = name.removeprefix("sparkshift.")
                assert dependency in SQLGLOT_FREE, (
                    f"{module} imports {dependency}, which may depend on SQLGlot"
                )


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

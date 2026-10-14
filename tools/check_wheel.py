"""Check a built wheel before it could be published.

Usage: python tools/check_wheel.py dist/sparkshift-*.whl

Checks what a development checkout cannot show: that the wheel's only runtime
dependency is SQLGlot (PySpark is never required, see ADR 0002) and its only
optional one the Databricks SDK, in the ``databricks`` extra (ADR 0010), that
it contains the package, its type information, its license, and the
``sparkshift`` command, and nothing from the repository besides the package.
Prints each problem and exits with 1 if there are any.
"""

import re
import sys
import zipfile
from email.parser import Parser

ALLOWED_DEPENDENCIES = ["sqlglot"]
ALLOWED_EXTRAS = {"databricks": ["databricks-sdk"]}
REQUIRED_FILES = [
    "sparkshift/__init__.py",
    "sparkshift/cli.py",
    "sparkshift/databricks.py",
    "sparkshift/py.typed",
]
COMMAND = "sparkshift = sparkshift.cli:main"
_EXTRA = re.compile(r"""extra\s*==\s*["']([^"']+)["']""")


def problems(wheel_path: str) -> list[str]:
    with zipfile.ZipFile(wheel_path) as wheel:
        names = wheel.namelist()
        dist_info = next(name.split("/")[0] for name in names if ".dist-info/" in name)
        metadata = Parser().parsestr(wheel.read(f"{dist_info}/METADATA").decode())
        entry_points = wheel.read(f"{dist_info}/entry_points.txt").decode()

    found = []
    requirements = metadata.get_all("Requires-Dist") or []
    dependencies: list[str] = []
    extras: dict[str, list[str]] = {}
    for item in requirements:
        name = re.match(r"[A-Za-z0-9._-]+", item)[0]
        extra = _EXTRA.search(item)
        if extra is None:
            dependencies.append(name)
        else:
            extras.setdefault(extra[1], []).append(name)
    if dependencies != ALLOWED_DEPENDENCIES:
        found.append(
            f"runtime dependencies should be {ALLOWED_DEPENDENCIES}: {requirements}"
        )
    if extras != ALLOWED_EXTRAS:
        found.append(f"optional dependencies should be {ALLOWED_EXTRAS}: {extras}")
    found += [f"missing {name}" for name in REQUIRED_FILES if name not in names]
    if not any(name.endswith(".dist-info/licenses/LICENSE") for name in names):
        found.append("missing the LICENSE file")
    if COMMAND not in entry_points:
        found.append(f"missing the console script: {COMMAND}")
    stray = [name for name in names if not name.startswith(("sparkshift/", dist_info))]
    if stray:
        found.append(f"files outside the package: {stray}")
    return found


def main(paths: list[str]) -> int:
    if not paths:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    failures = [f"{path}: {problem}" for path in paths for problem in problems(path)]
    for failure in failures:
        print(failure, file=sys.stderr)
    if not failures:
        print(f"{len(paths)} wheel(s) OK")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

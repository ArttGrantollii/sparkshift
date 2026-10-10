"""Regenerate examples/pyspark/ from examples/sql/ with the sparkshift command.

Run from the repository root:

    uv run python examples/regenerate.py

Each folder under sql/ is named after the dialect of its queries ("generic"
for generic SQL). Tests fail when a committed example differs from what
SparkShift generates today, so run this after changing the converter.
"""

from pathlib import Path

from sparkshift.cli import main

EXAMPLES = Path(__file__).parent


def regenerate(output: Path = EXAMPLES / "pyspark") -> int:
    """Convert every example folder into ``output``; return the worst exit
    code."""
    worst = 0
    for folder in sorted((EXAMPLES / "sql").iterdir()):
        arguments = ["convert", str(folder), "-o", str(output / folder.name)]
        if folder.name != "generic":
            arguments += ["--dialect", folder.name]
        worst = max(worst, main(arguments))
    return worst


if __name__ == "__main__":
    raise SystemExit(regenerate())

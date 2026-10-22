"""Rules for converting many files at once, shared by the command line and
the playground: which files are converted, the dialect of each, which
output paths collide, and, for a pipeline, which files define the same
dataset.

Every input keeps its relative path, with a new suffix. Outputs are refused
before anything is written when their paths would collide on a file system
that ignores case, such as those of Windows and macOS: two outputs with the
same path, or an output whose path is a folder of another output.
"""

from collections.abc import Sequence
from pathlib import PurePath

from sparkshift.dialects import SAS, is_sas


def source_suffixes(dialect: str | None) -> tuple[str, ...]:
    """The suffixes of the files to convert: .sql and .sas files, or only
    .sas files with the SAS dialect."""
    return (".sas",) if is_sas(dialect) else (".sql", ".sas")


def dialect_of(path: PurePath, dialect: str | None) -> str | None:
    """The dialect to convert a file in: a .sas file is a SAS program."""
    return SAS if path.suffix.lower() == ".sas" else dialect


def shared_datasets(definitions: Sequence[tuple[str, Sequence[str]]]) -> dict[int, str]:
    """The files of a pipeline that define a dataset another file defines
    too, by position, each with the reason, given each file's name and the
    datasets it defines: a pipeline defines each dataset once."""
    definers: dict[str, list[int]] = {}
    for position, (_, tables) in enumerate(definitions):
        for table in tables:
            definers.setdefault(table.lower(), []).append(position)
    reasons: dict[int, str] = {}
    for table, positions in definers.items():
        if len(positions) < 2:
            continue
        for position in positions:
            others = ", ".join(
                definitions[other][0] for other in positions if other != position
            )
            reasons.setdefault(
                position,
                f"its dataset {table} is also defined by {others}; a pipeline "
                "defines each dataset once",
            )
    return reasons


def colliding(outputs: Sequence[PurePath]) -> set[int]:
    """The positions in ``outputs``, relative paths, whose paths collide."""
    by_name: dict[tuple[str, ...], list[int]] = {}
    for index, output in enumerate(outputs):
        # Compare whole components, so a.py_extra is not a child of a.py.
        # Case-fold even on Linux: batches should remain portable to Windows.
        name = tuple(part.casefold() for part in output.parts)
        by_name.setdefault(name, []).append(index)

    conflicts: set[int] = set()
    for name, indexes in by_name.items():
        if len(indexes) > 1:
            conflicts.update(indexes)
        for depth in range(1, len(name)):
            if ancestors := by_name.get(name[:depth]):
                conflicts.update(ancestors)
                conflicts.update(indexes)
    return conflicts

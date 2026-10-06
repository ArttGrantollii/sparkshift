from importlib.metadata import version

import sparkshift


def test_version_matches_installed_metadata() -> None:
    # The version is defined once, in sparkshift/__init__.py, and hatchling
    # copies it into the package metadata at build time. If these ever differ,
    # the single source of truth is broken.
    assert sparkshift.__version__ == version("sparkshift")

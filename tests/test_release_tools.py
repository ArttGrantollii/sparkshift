"""Tests for tools/release.py and the description check in tools/check_wheel.py."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

TOOLS = Path(__file__).parents[1] / "tools"


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release = load("release")
check_wheel = load("check_wheel")

CHANGELOG = """# Changelog

## [Unreleased]

## [0.2.0] - 2026-11-01

### Fixed

- Something.

## [0.1.0] - 2026-10-20

### Added

- Everything.
"""


def project(tmp_path: Path, version: str, changelog: str = CHANGELOG) -> Path:
    package = tmp_path / "src" / "sparkshift"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(f'"""Doc."""\n\n__version__ = "{version}"\n')
    (tmp_path / "CHANGELOG.md").write_text(changelog)
    return tmp_path


@pytest.mark.parametrize(
    ("version", "prerelease"),
    [
        ("0.1.0", False),
        ("1.2.10", False),
        ("0.1.0rc1", True),
        ("0.1.0a2", True),
        ("0.1.0b1", True),
        ("0.1.0.dev0", True),
    ],
)
def test_prereleases_are_recognized(version: str, prerelease: bool) -> None:
    assert release.is_prerelease(version) is prerelease


def test_the_version_is_read_from_the_package() -> None:
    assert release.package_version() == load("release").package_version()
    assert release.package_version().count(".") >= 2


def test_a_matching_release_with_notes_passes(tmp_path: Path) -> None:
    assert release.problems("v0.1.0", project(tmp_path, "0.1.0")) == []


def test_a_tag_must_match_the_version(tmp_path: Path) -> None:
    root = project(tmp_path, "0.1.0")

    assert release.problems("v0.1.1", root) == [
        "tag v0.1.1 does not match the package version 0.1.0"
    ]
    assert release.problems("0.1.0", root)  # the v is required


def test_a_final_release_needs_changelog_notes(tmp_path: Path) -> None:
    root = project(tmp_path, "0.3.0")

    assert release.problems("v0.3.0", root) == [
        "CHANGELOG.md has no section '## [0.3.0] - date'"
    ]


def test_a_prerelease_needs_no_notes(tmp_path: Path) -> None:
    assert release.problems("v0.3.0rc1", project(tmp_path, "0.3.0rc1")) == []


def test_notes_are_one_section_of_the_changelog() -> None:
    assert release.notes("0.2.0", CHANGELOG) == "### Fixed\n\n- Something."
    assert release.notes("0.1.0", CHANGELOG) == "### Added\n\n- Everything."
    assert release.notes("0.1", CHANGELOG) is None  # not a prefix match
    assert release.notes("9.9.9", CHANGELOG) is None


def test_relative_links_in_the_description_are_found() -> None:
    description = (
        "See [the guide](docs/cli.md), [a section](#install), "
        "[GitHub](https://github.com/x/y), and [mail](mailto:a@b.c)."
    )

    assert check_wheel.relative_links(description) == ["docs/cli.md", "#install"]

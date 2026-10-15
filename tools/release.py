"""Checks and notes for a release, used by .github/workflows/release.yml.

Usage:

    python3 tools/release.py check v0.1.0     # the tag matches the package
    python3 tools/release.py notes 0.1.0      # the CHANGELOG section

`check` fails unless the tag is "v" followed by the version in
src/sparkshift/__init__.py and, for a final release, CHANGELOG.md has a
section for that version. It prints `prerelease=true` or `prerelease=false`
for the workflow: a pre-release (such as 0.1.0rc1) is published to TestPyPI
only. Uses only the standard library.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_VERSION = re.compile(r'^__version__ = "([^"]+)"$', re.MULTILINE)
# PEP 440 pre-releases and development releases: 0.1.0a1, 0.1.0rc1, 0.1.0.dev0.
_PRERELEASE = re.compile(r"(a|b|rc|\.dev)\d+$")


def package_version(root: Path = ROOT) -> str:
    match = _VERSION.search((root / "src" / "sparkshift" / "__init__.py").read_text())
    if match is None:
        raise SystemExit("no __version__ in src/sparkshift/__init__.py")
    return match[1]


def is_prerelease(version: str) -> bool:
    return _PRERELEASE.search(version) is not None


def notes(version: str, changelog: str) -> str | None:
    """The text of the CHANGELOG section `## [version] - date`, or None."""
    heading = re.compile(rf"^## \[{re.escape(version)}\].*$", re.MULTILINE)
    match = heading.search(changelog)
    if match is None:
        return None
    following = re.compile(r"^## ", re.MULTILINE).search(changelog, match.end())
    end = following.start() if following else len(changelog)
    return changelog[match.end() : end].strip()


def problems(tag: str, root: Path = ROOT) -> list[str]:
    """Why `tag` cannot be released from `root`; empty if it can."""
    version = package_version(root)
    found = []
    changelog = (root / "CHANGELOG.md").read_text()
    if tag != f"v{version}":
        found.append(f"tag {tag} does not match the package version {version}")
    elif not is_prerelease(version) and notes(version, changelog) is None:
        found.append(f"CHANGELOG.md has no section '## [{version}] - date'")
    return found


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in ("check", "notes"):
        print("\n".join(__doc__.strip().splitlines()[2:6]), file=sys.stderr)
        return 2
    command, value = argv
    if command == "notes":
        text = notes(value, (ROOT / "CHANGELOG.md").read_text())
        if text is None:
            print(f"CHANGELOG.md has no section for {value}", file=sys.stderr)
            return 1
        print(text)
        return 0
    found = problems(value)
    for problem in found:
        print(problem, file=sys.stderr)
    if found:
        return 1
    print(f"prerelease={str(is_prerelease(package_version())).lower()}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

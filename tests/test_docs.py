"""Every relative link in the documentation leads somewhere: the file exists,
and an anchor (`#section`) names a heading in it."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
DOCUMENTS = sorted(
    path
    for pattern in ("*.md", "docs/**/*.md", "examples/**/*.md", "playground/*.md")
    for path in ROOT.glob(pattern)
)
# [text](target), skipping images' `!` prefix and code spans.
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*$", re.MULTILINE)
FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)


def anchors(path: Path) -> set[str]:
    """The anchors GitHub gives a Markdown file's headings."""
    text = FENCE.sub("", path.read_text(encoding="utf-8"))
    found = set()
    for heading in HEADING.findall(text):
        slug = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        found.add(slug)
    return found


def links(path: Path) -> list[str]:
    text = FENCE.sub("", path.read_text(encoding="utf-8"))
    text = re.sub(r"`[^`\n]*`", "", text)  # links shown as code are not links
    return LINK.findall(text)


def test_documents_are_found() -> None:
    names = {path.relative_to(ROOT).as_posix() for path in DOCUMENTS}
    assert {
        "README.md",
        "docs/cli.md",
        "docs/adr/0010-upload-notebooks-with-the-databricks-sdk.md",
    } <= names


@pytest.mark.parametrize(
    "document", DOCUMENTS, ids=lambda path: path.relative_to(ROOT).as_posix()
)
def test_relative_links_resolve(document: Path) -> None:
    broken = []
    for target in links(document):
        if re.match(r"[a-z]+:", target):  # https:, mailto:
            continue
        file_part, _, anchor = target.partition("#")
        linked = (document.parent / file_part).resolve() if file_part else document
        if not linked.exists():
            broken.append(f"{target}: no such file")
        elif anchor and linked.suffix == ".md" and anchor not in anchors(linked):
            broken.append(f"{target}: no heading #{anchor}")
    assert broken == []

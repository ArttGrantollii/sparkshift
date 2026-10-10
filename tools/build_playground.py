"""Build the browser playground into a static site.

Usage: uv run python tools/build_playground.py [OUTPUT]   (default: _site)

The site holds the playground's files, the SparkShift wheel built from this
checkout, the SQLGlot wheel pinned in uv.lock (checked against its recorded
hash), and manifest.json, which lists the wheels and the examples gallery. It
needs no server: any static host, or ``python -m http.server``, can serve it.
"""

import hashlib
import json
import shutil
import subprocess
import sys
import tomllib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLAYGROUND_FILES = ["index.html", "style.css", "app.js", "bridge.py"]


def build(output: Path) -> None:
    if output.exists():
        shutil.rmtree(output)
    wheels = output / "wheels"
    wheels.mkdir(parents=True)
    for name in PLAYGROUND_FILES:
        shutil.copy2(ROOT / "playground" / name, output / name)

    subprocess.run(
        ["uv", "build", "--wheel", "--quiet", "--out-dir", str(wheels)],
        cwd=ROOT,
        check=True,
    )
    _download_locked_wheel("sqlglot", wheels)

    # SQLGlot first: SparkShift imports it.
    ordered = [*wheels.glob("sqlglot-*.whl"), *wheels.glob("sparkshift-*.whl")]
    manifest = {
        "wheels": [f"wheels/{wheel.name}" for wheel in ordered],
        "examples": _examples(),
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


def _download_locked_wheel(package: str, directory: Path) -> None:
    """Download the pure-Python wheel of ``package`` that uv.lock pins, and
    check it against the hash uv.lock records."""
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    [entry] = [item for item in lock["package"] if item["name"] == package]
    [wheel] = [
        item for item in entry["wheels"] if item["url"].endswith("-none-any.whl")
    ]
    with urllib.request.urlopen(wheel["url"]) as response:
        content = response.read()
    algorithm, expected = wheel["hash"].split(":")
    if hashlib.new(algorithm, content).hexdigest() != expected:
        raise SystemExit(f"{wheel['url']}: hash does not match uv.lock")
    (directory / wheel["url"].rsplit("/", 1)[1]).write_bytes(content)


def _examples() -> list[dict[str, str]]:
    """The examples gallery, generic SQL first, then by dialect."""
    paths = sorted(
        (ROOT / "examples" / "sql").rglob("*.sql"),
        key=lambda path: (path.parent.name != "generic", path.parent.name, path.name),
    )
    return [
        {
            "name": path.stem,
            "dialect": "" if path.parent.name == "generic" else path.parent.name,
            "sql": path.read_text(encoding="utf-8"),
        }
        for path in paths
    ]


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "_site"
    build(target.resolve())
    print(f"Built the playground in {target}")

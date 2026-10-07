# SparkShift

[![CI](https://github.com/ArttGrantollii/sparkshift/actions/workflows/ci.yml/badge.svg)](https://github.com/ArttGrantollii/sparkshift/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Convert SQL into readable, idiomatic, tested PySpark DataFrame code.

> **Status: early development.** SparkShift does not convert anything yet.
> This repository currently contains only the project foundation.
> See [ROADMAP.md](ROADMAP.md) for planned scope.

## Goal

Given a SQL query, SparkShift aims to generate PySpark DataFrame API code that a
data engineer would be comfortable maintaining — and to prove the generated code
is correct by executing both the original SQL and the generated PySpark against
the same data on real Apache Spark.

## Development setup

SparkShift is developed on Linux (WSL2 on Windows works). You need:

- [uv](https://docs.astral.sh/uv/) — manages Python and project dependencies
- Git
- Java 17, 21, or 25 (development and CI use 21) — only for running the Spark tests

```bash
git clone https://github.com/ArttGrantollii/sparkshift.git
cd sparkshift
uv sync
uv run pre-commit install
```

`uv sync` installs the Python version pinned in `.python-version` if needed,
creates a `.venv`, and installs SparkShift in editable mode together with the
development tools. PySpark is a development dependency only — SparkShift
generates PySpark code but does not need Spark to run.

### Running checks

```bash
uv run pytest                  # all tests, including real Spark
uv run pytest -m "not spark"   # fast tests only; does not start Spark
uv run pytest --cov            # with coverage report
uv run ruff check .            # lint
uv run ruff format --check .   # formatting
```

## Design documents

- [Architecture](docs/architecture.md) — how the core is structured and the rules that keep it portable
- [Architecture Decision Records](docs/adr/) — significant decisions and their trade-offs

## License

[Apache License 2.0](LICENSE)

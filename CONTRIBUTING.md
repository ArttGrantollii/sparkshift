# Contributing

Thank you for your interest in SparkShift. This page explains how to set up a
development environment, run the checks, and add support for a construct.

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

## Running checks

```bash
uv run pytest                  # all tests, including real Spark
uv run pytest -m "not spark"   # fast tests only; does not start Spark
uv run pytest --cov            # with coverage report
uv run ruff check .            # lint
uv run ruff format --check .   # formatting
```

To run the Spark tests through Spark Connect, as Databricks serverless compute
does: `uv sync --group connect`, then
`SPARKSHIFT_SPARK_MODE=connect uv run pytest -m spark`.

CI runs these on Python 3.11 and 3.14, the Spark tests through Spark Connect,
the PostgreSQL and MySQL scenarios on real database servers, the playground
in a real browser, and checks of the built package. See
[docs/testing.md](docs/testing.md) for what each layer proves.

## Adding a construct

SparkShift prefers a smaller set of constructs that are proven correct over a
larger set that is not. A construct is supported only when:

1. the translator (`src/sparkshift/translate.py`, or `sas_translate.py` for
   SAS) turns it into the IR, and rejects every variant it cannot translate
   exactly, with a message and a hint;
2. the emitter writes readable PySpark for it;
3. unit tests cover the translation, the generated code, and each rejection;
4. an equivalence scenario runs the original SQL and the generated PySpark
   on Spark and compares the results (for SAS, against rows derived by hand
   from SAS's documentation, citing the rule in [docs/sas.md](docs/sas.md));
5. the docs list it: [docs/supported-sql.md](docs/supported-sql.md) or
   [docs/sas.md](docs/sas.md), and the CHANGELOG.

When a dialect means something different from Spark, emulate it only if an
exact PySpark equivalent exists; otherwise reject it and document why.
Significant design decisions get an ADR in [docs/adr/](docs/adr/).

## Commits and pull requests

- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/),
  such as `fix(translator): ...` or `feat(cli): ...`.
- Keep each change focused, with its tests and docs in the same commit.
- Every CI job must pass.
- Never commit credentials: tokens, passwords, `.env` files. See
  [SECURITY.md](SECURITY.md).

Releases are published by pushing a version tag; see
[docs/releasing.md](docs/releasing.md).

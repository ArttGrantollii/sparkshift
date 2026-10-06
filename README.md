# SparkShift

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

```bash
git clone https://github.com/ArttGrantollii/sparkshift.git
cd sparkshift
uv sync
uv run python -c "import sparkshift; print(sparkshift.__version__)"
```

`uv sync` installs the Python version pinned in `.python-version` if needed,
creates a `.venv`, and installs SparkShift in editable mode.

## License

[Apache License 2.0](LICENSE)

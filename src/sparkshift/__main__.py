"""``python -m sparkshift``: the same command line as the ``sparkshift`` script."""

from sparkshift.cli import main

# Only when run, not when imported (tools such as the architecture tests
# import every module).
if __name__ == "__main__":
    raise SystemExit(main())

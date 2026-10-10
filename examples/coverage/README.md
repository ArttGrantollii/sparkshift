# Coverage report sample

A small, deliberately mixed workload to try `sparkshift report` on:

```bash
sparkshift report examples/coverage
```

Two queries convert (one uses `QUALIFY`). Four are blocked: by a
user-defined function, by `OFFSET`, or by holding two statements in one file.
One is invalid SQL. Unlike [the gallery](../README.md), these queries are not
all meant to convert; the main README shows the report they produce, and a
test keeps it up to date.

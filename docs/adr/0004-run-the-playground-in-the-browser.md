# 4. Run the playground in the browser with Pyodide

Date: 2026-10-10

## Status

Accepted

## Context

SparkShift needs a public demo that anyone can try without installing
anything. A demo could call a server that runs SparkShift, or run SparkShift
in the visitor's browser.

A server costs money to keep running, needs operating and securing, and
receives every query visitors paste, which may be proprietary. SparkShift's
core, however, is pure Python with one pure-Python dependency (SQLGlot) and
never imports PySpark (ADR 0002), so it can run anywhere Python runs —
including Pyodide, CPython compiled to WebAssembly.

## Decision

The playground is a static site that runs SparkShift in the browser:

- Pyodide is loaded from its official CDN at a pinned version.
- The SparkShift wheel built from the same commit, and the SQLGlot wheel pinned
  in `uv.lock` (checked against its recorded hash), are served by the site
  itself and unpacked into Pyodide; nothing is installed from PyPI at runtime.
- A small bridge (`playground/bridge.py`) returns results and errors as JSON,
  with the same error text as the command line.
- Plain HTML, CSS, and JavaScript, with no framework or build step.
- A Content Security Policy allows scripts only from the site and the Pyodide
  CDN, and no `eval`.

## Consequences

- No server, no cost, no secrets to protect, and visitors' SQL never leaves
  their browser.
- The first visit downloads about 10 MB (Pyodide); later visits use the
  browser cache.
- The playground can only use pure-Python dependencies. Optional features that
  need native code or network access (such as deploying to Databricks) stay
  out of it.
- A browser test in CI converts every example in headless Chromium and
  requires output identical to the command line's, so the two cannot drift
  apart.

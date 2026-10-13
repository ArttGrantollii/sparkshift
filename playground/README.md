# Playground

Live at **https://arttgrantollii.github.io/sparkshift/**.

A static web page that runs SparkShift in the browser with
[Pyodide](https://pyodide.org) (Python compiled to WebAssembly). There is no
server: the SQL a visitor types never leaves their browser. See
[ADR 0004](../docs/adr/0004-run-the-playground-in-the-browser.md) for why.

## Warnings, errors and input limits

Successful conversions show the same warnings as the Python API alongside the
output. They do not disappear merely because code was generated. For example,
SAS NODUPKEY warns that the retained row can differ from SAS's first-row behavior.
Warnings are inserted as text, so source fragments cannot become HTML.

Every conversion clears old code, errors and warnings before calling Python.
Editing source or dialect also clears the result and disables Copy. Unsupported
input and excessive nesting show actionable messages; an unexpected Python or
JSON failure shows a generic error. A later valid conversion restores output
and Copy. Internal exception tracebacks are not displayed to visitors.

The demo accepts at most **50,000 UTF-8 bytes** of input, checked both before the
JavaScript-to-Python call and in the bridge. The bridge publishes the limit in
`about()` so the UI and Python use the same policy. The CLI and library do not
inherit this demo restriction. This is a conservative input policy, not a
measured performance guarantee: conversion still runs on the main thread and
has no cancellation or execution timeout. Worker isolation remains future work.

## Files

| File | Purpose |
|---|---|
| `index.html`, `style.css` | The page |
| `app.js` | Loads Pyodide and the wheels, and connects the page to the bridge |
| `bridge.py` | Runs inside Pyodide: converts SQL and returns JSON with the code or the issues |

`tools/build_playground.py` assembles the site: these files, the SparkShift
wheel built from the checkout, the SQLGlot wheel pinned in `uv.lock`, and
`manifest.json` listing the wheels and the [examples](../examples/README.md).

## Run it locally

```bash
uv run python tools/build_playground.py
python3 -m http.server --directory _site 8000
```

Then open <http://localhost:8000>. Pyodide is loaded from its CDN, so the page
needs an internet connection.

## Deployment

`.github/workflows/pages.yml` builds the site and publishes it to GitHub
Pages after CI passes on `main`, from the exact commit CI tested.

## Tests

- `tests/test_playground_bridge.py` tests the bridge with CPython.
- `tests/playground/test_browser.py` builds the site and drives it in headless
  Chromium: every example must convert exactly as on the command line, errors
  must list the same issues, warnings must be visible, failures and input edits
  must clear stale results, and shared links must restore the query. The page
  must contact no other sites, fit a phone screen, and pass an axe-core
  accessibility audit (WCAG 2.1 AA) in light and dark mode, with and without
  warnings. Fault injection covers Python failures and malformed JSON; tests
  also exercise Unicode byte limits and recovery. Run it
  with `uv run playwright install chromium` once, then
  `uv run pytest -m browser`.

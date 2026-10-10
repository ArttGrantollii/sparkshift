# Playground

Live at **https://arttgrantollii.github.io/sparkshift/**.

A static web page that runs SparkShift in the browser with
[Pyodide](https://pyodide.org) (Python compiled to WebAssembly). There is no
server: the SQL a visitor types never leaves their browser. See
[ADR 0004](../docs/adr/0004-run-the-playground-in-the-browser.md) for why.

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
  must list the same issues, shared links must restore the query, the page
  must contact no other sites, fit a phone screen, and pass an axe-core
  accessibility audit (WCAG 2.1 AA) in light and dark mode. Run it
  with `uv run playwright install chromium` once, then
  `uv run pytest -m browser`.

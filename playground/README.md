# Playground

Live at **https://arttgrantollii.github.io/sparkshift/**.

A static web page that runs SparkShift in the browser with
[Pyodide](https://pyodide.org) (Python compiled to WebAssembly). There is no
server: the SQL a visitor types never leaves their browser. See
[ADR 0004](../docs/adr/0004-run-the-playground-in-the-browser.md) for why.

## Using it

Pick a source dialect (or SAS), type or load an example, and press **Convert**
(or Ctrl+Enter). The **PySpark / Notebook** switch chooses between the plain
code and the Databricks notebook that `sparkshift convert --notebook` writes.
Both editors show line numbers and syntax highlighting; the numbers are drawn
by CSS, so **Copy** and **Download .py** give exactly the generated code.
**Copy link** puts the query in the link's `#` fragment, which browsers do not
send to the server. The theme button switches between dark and light; the
choice is remembered in the browser's local storage, otherwise the page follows
the system setting. Fonts (IBM Plex Sans and JetBrains Mono, under the SIL Open
Font License, see `fonts/OFL-*.txt`) are served by the site itself.

## Folders

The **Folder** tab converts many files at once: drop a folder or files on the
page, or use **Choose folder** or **Choose files**. Files are read in the
browser and never uploaded. The page follows the command line's rules for
`sparkshift convert FOLDER -o OUT`, from the same code (`sparkshift.batch`):

- every `.sql` and `.sas` file is converted, in sorted order (with the SAS
  dialect, only `.sas` files); a `.sas` file is always a SAS program, and
  `.sql` files use the chosen dialect; other files are skipped and counted;
- files are read as strict UTF-8 with any line ending read as `\n`, as Python
  reads them; a file that is not UTF-8 text is reported, not guessed at;
- each output keeps its input's relative path, with `.py`; if two outputs would
  have the same path ignoring case (`report.sql` and `report.sas`), nothing is
  converted and the colliding files are listed.

The list shows every file's state, and selecting one shows its source, its
code, and its warnings or errors. **Download .zip** saves the converted files
in a folder named after the chosen one (`claims_pyspark/`, or
`claims_notebooks/` for notebooks); files that cannot be converted get no
output, as on the command line, so the zip holds exactly what the CLI would
write. Changing the dialect or the output kind converts the files again.

Each file converts in its own worker request, with the same limits and
deadline as a single file. A file that reaches the deadline is reported, Python
reloads, and the next file converts; **Cancel conversion** stops the whole
batch. The demo converts at most **200 files** at a time.

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
measured performance guarantee. Python and SQLGlot run in a module Web Worker;
the page stays available while conversion runs. Each conversion has a
**10-second deadline** and a **Cancel conversion** button. Editing source or
dialect during conversion also cancels it and invalidates its result. A deadline,
cancellation or worker crash terminates the worker and reloads Python; it does
not automatically rerun the cancelled query. Loading Python has a **120-second
deadline**; failed startup offers **Retry loading Python**. These are demo
policies, not limits on the Python library or CLI.

Restarting a worker reloads its Python runtime and packages, so recovery can take
several seconds depending on the connection and browser cache. Conversion timing
starts after Python is ready. Results are matched to their request and input
revision so late replies cannot replace the current query's output. See
[ADR 0009](../docs/adr/0009-isolate-browser-conversion-in-a-worker.md).

## Files

| File | Purpose |
|---|---|
| `index.html`, `style.css` | The page |
| `theme.js` | Applies a remembered theme before the page is drawn, so it does not flash |
| `fonts/` | Self-hosted WOFF2 fonts and their licenses |
| `app.js` | Owns UI state, renders results and handles cancellation/recovery |
| `highlight.js` | Highlights code and numbers its lines without changing its text |
| `files.js` | Collects chosen or dropped files and reads them as the command line does |
| `worker-client.js` | Matches request IDs, bounds loading/conversion time, and terminates workers |
| `worker.js` | Loads Pyodide and the wheels and invokes the Python bridge off the UI thread |
| `bridge.py` | Runs inside Pyodide: converts SQL and returns JSON with the code or the issues; plans folders and packs their zip |

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
  must clear stale results, and shared links must restore the query. Notebook
  output must match the command line, highlighting and line numbers must not
  change the text, downloads must contain exactly the code, and the theme choice
  must be remembered. A chosen folder must give a zip identical to what the
  command line writes, including files with Windows line endings or that are
  not UTF-8; colliding outputs, dropped folders, a file past the deadline and
  cancelling a folder are covered too. The page
  must contact no other sites, fit a phone screen, and pass an axe-core
  accessibility audit (WCAG 2.1 AA) in light and dark mode, with and without
  warnings. Fault injection covers Python failures and malformed JSON; tests
  also exercise Unicode byte limits and recovery. Worker tests block real Python
  with a CPU loop and check page timers, cancellation, input edits, deadlines,
  successful recovery, stale reply rejection and crash handling. A virtual page
  clock accelerates conversion deadlines; a stub worker checks a shorter injected
  startup deadline. Production limits are unchanged by the tests. Run it
  with `uv run playwright install chromium` once, then
  `uv run pytest -m browser`.

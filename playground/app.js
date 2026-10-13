// The page owns the UI; a cancellable worker owns Python and conversion.
import { ConversionWorker, STARTUP_TIMEOUT_MS, CONVERSION_TIMEOUT_MS } from "./worker-client.js";

const DIALECT_NAMES = {
  "": "Generic SQL",
  tsql: "T-SQL (SQL Server)",
  postgres: "PostgreSQL",
  mysql: "MySQL",
  snowflake: "Snowflake",
  bigquery: "BigQuery",
  oracle: "Oracle",
  sas: "SAS",
};

const elements = Object.fromEntries(
  [
    "dialect",
    "example",
    "convert",
    "cancel",
    "retry",
    "share",
    "status",
    "sql",
    "language",
    "output",
    "code",
    "copy",
    "errors",
    "warnings",
    "input-limit",
  ].map((id) => [id, document.getElementById(id)]),
);

let runtime = null;
let ready = false;
let busy = false;
let revision = 0;
let populated = false;
let examples = [];
let maxSourceBytes = null;

function setStatus(text) {
  elements.status.textContent = text;
}

function updateControls() {
  elements.convert.disabled = !ready || busy;
  elements.cancel.hidden = !busy;
  elements.retry.hidden = runtime !== null;
  elements.example.disabled = !populated || busy;
  elements.dialect.disabled = !populated;
  elements.share.disabled = !populated;
}

async function start(first = false, recoveryStatus = "Ready to convert again.") {
  runtime?.close();
  runtime = null;
  ready = false;
  busy = false;
  elements.retry.hidden = true;
  elements.convert.disabled = true;
  elements.cancel.hidden = true;
  const reason = recoveryStatus.startsWith("Ready") ? "" : `${recoveryStatus.split(".")[0]}. `;
  setStatus(first ? "Loading Python…" : `${reason}Reloading Python…`);
  let candidate;
  try {
    candidate = new ConversionWorker(() => {
      if (runtime !== candidate) return;
      conversionFailed({
        error: "The Python worker stopped unexpectedly. Reloading before another conversion.",
        issues: [],
      });
      void start(false, "Conversion failed. Ready to try again.");
    });
    runtime = candidate;
    const { about, manifest, pyodideVersion } = await candidate.request("init", {}, STARTUP_TIMEOUT_MS);
    if (runtime !== candidate) return;
    maxSourceBytes = about.max_source_bytes;
    elements["input-limit"].textContent =
      `Browser demo limit: ${maxSourceBytes.toLocaleString("en-US")} UTF-8 bytes of source. ` +
      "Conversion deadline: 10 seconds. Use the CLI for larger files.";
    if (!populated) {
      for (const dialect of about.dialects) {
        elements.dialect.add(new Option(DIALECT_NAMES[dialect] ?? dialect, dialect));
      }
      examples = manifest.examples;
      examples.forEach((example, index) => {
        const label = `${DIALECT_NAMES[example.dialect]}: ${example.name.replaceAll("_", " ")}`;
        elements.example.add(new Option(label, String(index)));
      });
      populated = true;
    }
    ready = true;
    updateControls();
    if (first) {
      const shared = new URLSearchParams(location.hash.slice(1)).has("sql");
      const success = await (shared ? loadSharedQuery() : loadExample(0));
      if (success && !shared && runtime === candidate) {
        setStatus(`Ready. SparkShift ${about.version}, running on Pyodide ${pyodideVersion}.`);
      }
    } else {
      setStatus(recoveryStatus);
    }
  } catch {
    if (candidate && runtime !== candidate) return;
    candidate?.close();
    runtime = null;
    ready = false;
    updateControls();
    setStatus("The playground could not start. Check your connection to cdn.jsdelivr.net " +
      "and this site, then choose Retry loading Python.");
  }
}

// --- Shared links -----------------------------------------------------------
//
// A link to a query keeps it after the "#", which browsers never send to a
// server: the SQL still stays on the visitor's machine.

function sharedQueryLink() {
  const parameters = new URLSearchParams({
    dialect: elements.dialect.value,
    sql: elements.sql.value,
  });
  return `${location.origin}${location.pathname}#${parameters}`;
}

function loadSharedQuery() {
  const parameters = new URLSearchParams(location.hash.slice(1));
  const sql = parameters.get("sql");
  if (sql === null) {
    return false;
  }
  const dialect = parameters.get("dialect") ?? "";
  elements.dialect.value = dialect in DIALECT_NAMES ? dialect : "";
  elements.sql.value = sql;
  return convert();
}

// --- Converting -------------------------------------------------------------

function loadExample(index) {
  const example = examples[index];
  if (example === undefined) {
    return;
  }
  elements.sql.value = example.sql;
  elements.dialect.value = example.dialect;
  return convert();
}

// The input pane is named after its language: SQL, or a SAS program.
function showInputLanguage() {
  elements.language.textContent = elements.dialect.value === "sas" ? "SAS program" : "SQL";
}

// Reset first: a Python exception, invalid JSON or a rendering error must
// never leave a previous query's output visible or available to Copy.
function clearOutput() {
  elements.copy.disabled = true;
  elements.output.hidden = true;
  elements.code.replaceChildren();
  for (const panel of [elements.errors, elements.warnings]) {
    panel.replaceChildren();
    panel.hidden = true;
  }
}

function inputChanged() {
  showInputLanguage();
  clearOutput();
  if (busy) {
    cancelConversion("Input changed. Convert to update the PySpark code.");
  } else if (populated) {
    setStatus("Input changed. Convert to update the PySpark code.");
  }
}

function conversionFailed(result) {
  clearOutput();
  elements.errors.hidden = false;
  showErrors(result);
  setStatus("Conversion failed.");
}

function cancelConversion(message = "Conversion cancelled. Ready to convert again.") {
  if (!busy) return;
  revision += 1;
  clearOutput();
  // Termination is immediate even if Python cannot process another message.
  runtime.close();
  void start(false, message);
}

async function convert() {
  if (!ready || busy) return false;
  showInputLanguage();
  clearOutput();
  const candidate = runtime;
  const currentRevision = ++revision;
  busy = true;
  updateControls();
  setStatus("Converting…");
  try {
    // UTF-8 bytes, matching the Python bridge (not UTF-16 string length).
    if (new TextEncoder().encode(elements.sql.value).length > maxSourceBytes) {
      conversionFailed({
        error: `The browser demo accepts up to ${maxSourceBytes.toLocaleString("en-US")} ` +
          "UTF-8 bytes of source code. Use the CLI for larger files.",
        issues: [],
      });
      return false;
    }
    const result = await candidate.request("convert", {
      sql: elements.sql.value,
      dialect: elements.dialect.value,
    }, CONVERSION_TIMEOUT_MS);
    // Input edits and cancellation invalidate results, even if they arrive late.
    if (runtime !== candidate || revision !== currentRevision) return false;
    if (!result.ok) {
      conversionFailed(result);
      return false;
    }
    elements.code.replaceChildren(highlight(result.code));
    showWarnings(result.warnings);
    elements.output.hidden = false;
    elements.copy.disabled = false;
    setStatus(result.warnings.length
      ? `Converted with ${result.warnings.length} warning${result.warnings.length === 1 ? "" : "s"}. Review before running.`
      : "Converted successfully.");
    return true;
  } catch (error) {
    if (runtime !== candidate || revision !== currentRevision) return false;
    conversionFailed({
      error: error.name === "TimeoutError"
        ? "Conversion exceeded the browser demo's 10-second limit. Try a smaller input or use the CLI."
        : "Conversion failed unexpectedly. Try a smaller input, or report a reproducible example on GitHub.",
      issues: [],
    });
    if (candidate.closed) {
      void start(false, "Conversion failed. Ready to try again.");
    }
    return false;
  } finally {
    if (runtime === candidate && revision === currentRevision) {
      busy = false;
      updateControls();
    }
  }
}

function showWarnings(warnings) {
  if (warnings.length === 0) {
    return;
  }
  const heading = document.createElement("p");
  heading.textContent = "Warnings — review before running";
  const list = document.createElement("ul");
  for (const warning of warnings) {
    const item = document.createElement("li");
    item.textContent = warning;
    list.append(item);
  }
  elements.warnings.append(heading, list);
  elements.warnings.hidden = false;
}

function showErrors(result) {
  const heading = document.createElement("p");
  // The first line of the error, e.g. "2 unsupported constructs:"; the
  // issues are listed below it.
  heading.textContent = result.issues.length ? result.error.split("\n")[0] : result.error;
  elements.errors.append(heading);
  if (result.issues.length) {
    const list = document.createElement("ul");
    for (const issue of result.issues) {
      const item = document.createElement("li");
      const sql = document.createElement("code");
      sql.textContent = issue.sql;
      item.append(`${issue.message}: `, sql);
      if (issue.hint) {
        const hint = document.createElement("p");
        hint.className = "hint";
        hint.textContent = issue.hint;
        item.append(hint);
      }
      list.append(item);
    }
    elements.errors.append(list);
  }
}

// --- Highlighting -----------------------------------------------------------
//
// Generated code uses a small part of Python, so a few patterns are enough.
// Highlighting only wraps text in spans: the code's text, and what Copy
// copies, stay exactly the generated code.

const TOKENS = /("(?:[^"\\\n]|\\.)*")|\b(from|import|as|True|False|None)\b|\b((?:F|Window)\.\w+)|\b(\d+(?:\.\d+)?)\b/g;
const TOKEN_CLASSES = ["string", "keyword", "function", "number"];

function highlight(code) {
  const fragment = document.createDocumentFragment();
  let end = 0;
  for (const match of code.matchAll(TOKENS)) {
    fragment.append(code.slice(end, match.index));
    const span = document.createElement("span");
    span.className = TOKEN_CLASSES[match.slice(1).findIndex((group) => group !== undefined)];
    span.textContent = match[0];
    fragment.append(span);
    end = match.index + match[0].length;
  }
  fragment.append(code.slice(end));
  return fragment;
}

// --- Events -----------------------------------------------------------------

elements.convert.addEventListener("click", convert);
elements.cancel.addEventListener("click", () => cancelConversion());
elements.retry.addEventListener("click", () => { void start(); });
elements.dialect.addEventListener("change", inputChanged);
elements.sql.addEventListener("input", inputChanged);
elements.example.addEventListener("change", () => {
  loadExample(Number(elements.example.value));
  elements.example.value = "";
});
elements.sql.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    convert();
  }
});
elements.copy.addEventListener("click", async () => {
  await navigator.clipboard.writeText(elements.code.textContent);
  setStatus("Copied the PySpark code.");
});
elements.share.addEventListener("click", async () => {
  const link = sharedQueryLink();
  history.replaceState(null, "", link);
  await navigator.clipboard.writeText(link);
  setStatus("Copied a link to this query.");
});

void start(true);

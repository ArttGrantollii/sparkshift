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
    "statusbar",
    "sql",
    "sql-view",
    "language",
    "output",
    "output-empty",
    "output-label",
    "output-code",
    "output-notebook",
    "code",
    "copy",
    "download",
    "errors",
    "warnings",
    "input-limit",
    "input-lines",
    "output-lines",
    "theme",
  ].map((id) => [id, document.getElementById(id)]),
);

let runtime = null;
let ready = false;
let busy = false;
let revision = 0;
let populated = false;
let examples = [];
let maxSourceBytes = null;
let notebook = false;

// The dot beside the status: busy, ready, ok, warn, or bad.
function setStatus(text, state = "ready") {
  elements.status.textContent = text;
  elements.statusbar.dataset.state = state;
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
  setStatus(first ? "Loading Python…" : `${reason}Reloading Python…`, "busy");
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
        setStatus(`Ready. SparkShift ${about.version}, running on Pyodide ${pyodideVersion}.`, "ok");
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
      "and this site, then choose Retry loading Python.", "bad");
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
  setSource(sql);
  return convert();
}

// --- Converting -------------------------------------------------------------

function loadExample(index) {
  const example = examples[index];
  if (example === undefined) {
    return;
  }
  setSource(example.sql);
  elements.dialect.value = example.dialect;
  return convert();
}

function setSource(text) {
  elements.sql.value = text;
  showSource();
}

// The input pane is named after its language: SQL, or a SAS program.
function showInputLanguage() {
  elements.language.textContent = elements.dialect.value === "sas" ? "SAS program" : "SQL";
}

// Reset first: a Python exception, invalid JSON or a rendering error must
// never leave a previous query's output visible or available to Copy.
function clearOutput() {
  elements.copy.disabled = true;
  elements.download.disabled = true;
  elements.output.hidden = true;
  elements["output-empty"].hidden = false;
  elements["output-lines"].textContent = "";
  elements.code.replaceChildren();
  for (const panel of [elements.errors, elements.warnings]) {
    panel.replaceChildren();
    panel.hidden = true;
  }
}

function inputChanged() {
  showInputLanguage();
  showSource();
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
  setStatus("Conversion failed.", "bad");
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
  setStatus("Converting…", "busy");
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
      notebook,
    }, CONVERSION_TIMEOUT_MS);
    // Input edits and cancellation invalidate results, even if they arrive late.
    if (runtime !== candidate || revision !== currentRevision) return false;
    if (!result.ok) {
      conversionFailed(result);
      return false;
    }
    elements.code.replaceChildren(highlightLines(result.code, "python"));
    elements["output-lines"].textContent = lineCount(result.code);
    showWarnings(result.warnings);
    elements["output-empty"].hidden = true;
    elements.output.hidden = false;
    elements.copy.disabled = false;
    elements.download.disabled = false;
    setStatus(result.warnings.length
      ? `Converted with ${result.warnings.length} warning${result.warnings.length === 1 ? "" : "s"}. Review before running.`
      : "Converted successfully.", result.warnings.length ? "warn" : "ok");
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
// Each line becomes a span whose number CSS draws (::before), and tokens are
// wrapped in spans by kind. Only spans are added: the text, and what Copy
// copies, stay exactly the generated code. A few patterns are enough for the
// small part of each language involved.

const PATTERNS = {
  python: /("(?:[^"\\\n]|\\.)*")|\b(from|import|as|if|True|False|None)\b|\b((?:F|Window|spark|dbutils)\.\w+|display)\b|\b(\d+(?:\.\d+)?)\b/g,
  sql: /('(?:[^'\n]|'')*'|"[^"\n]*")|\b(SELECT|FROM|WHERE|JOIN|INNER|LEFT|RIGHT|FULL|OUTER|CROSS|ON|USING|GROUP|BY|ORDER|HAVING|LIMIT|TOP|AS|AND|OR|NOT|IN|IS|NULL|BETWEEN|LIKE|CASE|WHEN|THEN|ELSE|END|DISTINCT|UNION|ALL|EXCEPT|INTERSECT|WITH|OVER|PARTITION|QUALIFY|ASC|DESC|CAST|EXISTS|FETCH|FIRST|ROWS|ONLY|NULLS|LAST)\b|()(\b\d+(?:\.\d+)?\b)/gi,
  sas: /('(?:[^'\n]|'')*'|"[^"\n]*")|\b(data|set|run|quit|proc|where|keep|drop|rename|if|then|else|do|end|length|by|class|var|output|out|tables|create|table|select|from|group|order|as|and|or|not|in|sort|means|summary|freq|sql|nodupkey|noprint|nway|descending)\b|()(\b\d+(?:\.\d+)?\b)/gi,
};
const CLASSES = ["string", "keyword", "function", "number"];
const COMMENT = { python: /^\s*#/, sql: /^\s*--/, sas: /^\s*(\*|\/\*)/ };

function highlightLine(line, language) {
  const fragment = document.createDocumentFragment();
  if (COMMENT[language].test(line)) {
    const span = document.createElement("span");
    span.className = "comment";
    span.textContent = line;
    fragment.append(span);
    return fragment;
  }
  let end = 0;
  for (const match of line.matchAll(PATTERNS[language])) {
    if (match[0] === "") continue;
    fragment.append(line.slice(end, match.index));
    const span = document.createElement("span");
    span.className = CLASSES[match.slice(1).findIndex((group) => group)];
    span.textContent = match[0];
    fragment.append(span);
    end = match.index + match[0].length;
  }
  fragment.append(line.slice(end));
  return fragment;
}

// Lines joined by "\n" text, so textContent is exactly ``text``. A final
// newline gets no numbered line, unless ``numberLastLine`` (the editor,
// where the caret can be on it).
function highlightLines(text, language, numberLastLine = false) {
  const fragment = document.createDocumentFragment();
  const lines = text.split("\n");
  lines.forEach((line, index) => {
    if (index > 0) fragment.append("\n");
    if (index === lines.length - 1 && line === "" && !numberLastLine) return;
    const span = document.createElement("span");
    span.className = "ln";
    span.append(highlightLine(line, language));
    fragment.append(span);
  });
  return fragment;
}

function lineCount(text) {
  const lines = text.replace(/\n$/, "").split("\n").length;
  return `${lines} line${lines === 1 ? "" : "s"}`;
}

function showSource() {
  const language = elements.dialect.value === "sas" ? "sas" : "sql";
  const text = elements.sql.value;
  elements["sql-view"].replaceChildren(highlightLines(text, language, true));
  elements["input-lines"].textContent = text ? lineCount(text) : "";
  syncScroll();
}

function syncScroll() {
  elements["sql-view"].scrollTop = elements.sql.scrollTop;
  elements["sql-view"].scrollLeft = elements.sql.scrollLeft;
}

// --- Output kind and theme --------------------------------------------------

function chooseOutput(asNotebook) {
  if (notebook === asNotebook) return;
  notebook = asNotebook;
  elements["output-code"].setAttribute("aria-checked", String(!asNotebook));
  elements["output-notebook"].setAttribute("aria-checked", String(asNotebook));
  elements["output-label"].textContent = asNotebook ? "Notebook" : "PySpark";
  elements.download.textContent = "Download .py";
  if (ready && !busy) {
    void convert();
  } else {
    clearOutput();
  }
}

function effectiveTheme() {
  const chosen = document.documentElement.dataset.theme;
  if (chosen) return chosen;
  return matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

function showThemeButton() {
  const other = effectiveTheme() === "dark" ? "light" : "dark";
  elements.theme.setAttribute("aria-label", `Switch to ${other} theme`);
  elements.theme.title = `Switch to ${other} theme`;
}

function toggleTheme() {
  const theme = effectiveTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem("sparkshift-theme", theme);
  } catch {
    // Storage can be blocked; the choice then lasts until the page closes.
  }
  showThemeButton();
}

function download() {
  const blob = new Blob([elements.code.textContent], { type: "text/x-python" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = notebook ? "converted_notebook.py" : "converted.py";
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
  setStatus(`Downloaded ${link.download}.`, "ok");
}

// --- Events -----------------------------------------------------------------

elements.convert.addEventListener("click", convert);
elements.cancel.addEventListener("click", () => cancelConversion());
elements.retry.addEventListener("click", () => { void start(); });
elements.dialect.addEventListener("change", inputChanged);
elements.sql.addEventListener("input", inputChanged);
elements.sql.addEventListener("scroll", syncScroll);
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
  setStatus("Copied the PySpark code.", "ok");
});
elements.download.addEventListener("click", download);
elements.share.addEventListener("click", async () => {
  const link = sharedQueryLink();
  history.replaceState(null, "", link);
  await navigator.clipboard.writeText(link);
  setStatus("Copied a link to this query.", "ok");
});
elements["output-code"].addEventListener("click", () => chooseOutput(false));
elements["output-notebook"].addEventListener("click", () => chooseOutput(true));
elements.theme.addEventListener("click", toggleTheme);
matchMedia("(prefers-color-scheme: light)").addEventListener("change", showThemeButton);

showThemeButton();
showSource();
void start(true);

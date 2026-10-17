// The page owns the UI; a cancellable worker owns Python and conversion.
import { ConversionWorker, STARTUP_TIMEOUT_MS, CONVERSION_TIMEOUT_MS } from "./worker-client.js";
import { highlightLines, lineCount } from "./highlight.js";
import { filesFromDrop, filesFromInput, readSource } from "./files.js";

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
    "example-field",
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
    "download-zip",
    "errors",
    "warnings",
    "input-limit",
    "input-lines",
    "output-lines",
    "theme",
    "tab-file",
    "tab-folder",
    "view-file",
    "view-folder",
    "folder-count",
    "folder-bar",
    "folder-legend",
    "folder-errors",
    "files",
    "drop",
    "choose-folder",
    "choose-files",
    "pick-folder",
    "pick-files",
    "file-source-label",
    "file-source-name",
    "file-source-lines",
    "file-source-empty",
    "file-source",
    "file-output-label",
    "file-output-name",
    "file-output-lines",
    "file-output-empty",
    "file-output",
    "file-code",
    "file-warnings",
    "file-errors",
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
let mode = "file";
// The last status of each view, shown again when the visitor returns to it.
const savedStatus = { file: null, folder: null };

// The folder view: the files chosen, the plan for converting them (from
// Python, with the command line's rules), one result per planned file, and
// the zip of the converted files. ``batch`` is true while a folder converts,
// across the requests it makes and any reload of Python between them.
let chosen = null;
let plan = null;
let results = [];
let selected = -1;
let archive = null;
let batch = false;

function working() {
  return busy || batch;
}

// The dot beside the status: busy, ready, ok, warn, or bad.
function setStatus(text, state = "ready") {
  elements.status.textContent = text;
  elements.statusbar.dataset.state = state;
  savedStatus[mode] = { text, state };
}

function updateControls() {
  const folder = mode === "folder";
  elements.convert.disabled = !ready || working() || (folder && chosen === null);
  elements.cancel.hidden = !working();
  elements.retry.hidden = runtime !== null;
  elements.example.disabled = !populated || working();
  // A folder's results follow the dialect and output kind, so those stay
  // fixed while its files convert.
  elements.dialect.disabled = !populated || (folder && working());
  elements["output-code"].disabled = folder && working();
  elements["output-notebook"].disabled = folder && working();
  elements.share.disabled = !populated;
  elements["choose-folder"].disabled = !ready || working();
  elements["choose-files"].disabled = !ready || working();
  elements["download-zip"].disabled = working() || archive === null;
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
      `Browser demo limits: ${maxSourceBytes.toLocaleString("en-US")} UTF-8 bytes of source ` +
      `per file, ${about.max_batch_files} files at a time, and 10 seconds per conversion. ` +
      "Use the CLI for larger inputs.";
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
  if (mode === "file") elements.copy.disabled = true;
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
  showErrors(result, elements.errors);
  setStatus("Conversion failed.", "bad");
}

function cancelConversion(message = "Conversion cancelled. Ready to convert again.") {
  if (!working()) return;
  revision += 1;
  if (batch) {
    batch = false;
    resetFolder();
  } else {
    clearOutput();
  }
  // Termination is immediate even if Python cannot process another message.
  runtime?.close();
  void start(false, message);
}

function failureMessage(error) {
  return error.name === "TimeoutError"
    ? "Conversion exceeded the browser demo's 10-second limit. Try a smaller input or use the CLI."
    : "Conversion failed unexpectedly. Try a smaller input, or report a reproducible example on GitHub.";
}

function tooLargeMessage() {
  return `The browser demo accepts up to ${maxSourceBytes.toLocaleString("en-US")} ` +
    "UTF-8 bytes of source code. Use the CLI for larger files.";
}

async function convert() {
  if (!ready || working()) return false;
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
      conversionFailed({ error: tooLargeMessage(), issues: [] });
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
    showWarnings(result.warnings, elements.warnings);
    elements["output-empty"].hidden = true;
    elements.output.hidden = false;
    if (mode === "file") elements.copy.disabled = false;
    elements.download.disabled = false;
    setStatus(result.warnings.length
      ? `Converted with ${result.warnings.length} warning${result.warnings.length === 1 ? "" : "s"}. Review before running.`
      : "Converted successfully.", result.warnings.length ? "warn" : "ok");
    return true;
  } catch (error) {
    if (runtime !== candidate || revision !== currentRevision) return false;
    conversionFailed({ error: failureMessage(error), issues: [] });
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

function showWarnings(warnings, panel) {
  panel.replaceChildren();
  panel.hidden = warnings.length === 0;
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
  panel.append(heading, list);
}

function showErrors(result, panel) {
  panel.replaceChildren();
  panel.hidden = false;
  const heading = document.createElement("p");
  // The first line of the error, e.g. "2 unsupported constructs:"; the
  // issues are listed below it.
  heading.textContent = result.issues.length ? result.error.split("\n")[0] : result.error;
  panel.append(heading);
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
    panel.append(list);
  }
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

// --- Folders ----------------------------------------------------------------
//
// Python plans the batch, then each file converts in its own request, with
// its own deadline; a file that ends the worker is reported, Python reloads,
// and the next file converts. Cancelling stops the whole batch.

const FILE_STATES = {
  pending: "Waiting",
  busy: "Converting",
  ok: "Converted",
  warn: "Warnings",
  bad: "Not converted",
};

function fileCount(count) {
  return `${count} ${count === 1 ? "file" : "files"}`;
}

function chooseFiles(found) {
  if (!ready || working() || found.length === 0) return;
  chosen = found;
  void convertFolder();
}

async function convertFolder() {
  if (!ready || working() || chosen === null) return false;
  const currentRevision = ++revision;
  let candidate = runtime;
  const stale = () => runtime !== candidate || revision !== currentRevision;
  batch = true;
  clearFolder();
  updateControls();
  setStatus("Converting…", "busy");
  try {
    const planned = await candidate.request("plan", {
      paths: chosen.map((entry) => entry.path),
      dialect: elements.dialect.value,
      notebook,
    }, CONVERSION_TIMEOUT_MS);
    if (stale()) return false;
    if (!planned.ok) {
      showFolderErrors(planned);
      setStatus("Conversion failed.", "bad");
      return false;
    }
    plan = planned;
    results = plan.files.map(() => ({ state: "pending" }));
    buildFileList();
    select(0);
    for (const [position, entry] of plan.files.entries()) {
      setStatus(`Converting ${position + 1} of ${fileCount(plan.files.length)}…`, "busy");
      setResult(position, { state: "busy" });
      const result = await convertFile(candidate, entry);
      if (stale()) return false;
      setResult(position, result);
      if (candidate.closed) {
        // A deadline or a crash ended the worker: reload Python for the rest.
        await start(false, "Conversion failed. Ready to try again.");
        if (revision !== currentRevision || !ready) return false;
        candidate = runtime;
        updateControls();
      }
    }
    const [summary, state] = folderSummary();
    setStatus(await packFolder(candidate, stale) ? summary : `${summary} The zip could not be made.`, state);
    return true;
  } catch (error) {
    if (stale()) return false;
    showFolderErrors({ error: failureMessage(error), issues: [] });
    setStatus("Conversion failed.", "bad");
    if (candidate.closed) {
      batch = false;
      void start(false, "Conversion failed. Ready to try again.");
    }
    return false;
  } finally {
    if (revision === currentRevision) {
      batch = false;
      updateControls();
    }
  }
}

// The same checks, and the same messages, as converting one file.
async function convertFile(candidate, entry) {
  const { file } = chosen[entry.index];
  // Reading "\r\n" as "\n" at most halves a file: larger ones cannot fit.
  if (file.size > 2 * maxSourceBytes) {
    return { state: "bad", error: tooLargeMessage(), issues: [] };
  }
  const source = await readSource(file);
  if (source.error) {
    return { state: "bad", error: `Cannot read this file: ${source.error}.`, issues: [] };
  }
  const sql = source.text;
  if (new TextEncoder().encode(sql).length > maxSourceBytes) {
    return { state: "bad", sql, error: tooLargeMessage(), issues: [] };
  }
  try {
    const result = await candidate.request("convert", {
      sql,
      dialect: entry.dialect,
      notebook,
      source: entry.name.split("/").pop(),
    }, CONVERSION_TIMEOUT_MS);
    if (!result.ok) {
      return { state: "bad", sql, error: result.error, issues: result.issues };
    }
    return {
      state: result.warnings.length ? "warn" : "ok",
      sql,
      code: result.code,
      warnings: result.warnings,
    };
  } catch (error) {
    return { state: "bad", sql, error: failureMessage(error), issues: [] };
  }
}

// Zip the converted files, as the command line would write them. False if
// the zip could not be made.
async function packFolder(candidate, stale) {
  const converted = plan.files.flatMap((entry, position) =>
    results[position].code === undefined ? [] : [{ output: entry.output, code: results[position].code }]);
  if (converted.length === 0) return true;
  try {
    const result = await candidate.request("zip", { folder: plan.folder, files: converted }, CONVERSION_TIMEOUT_MS);
    if (stale() || !result.ok) return false;
    const blob = new Blob([bytesFromBase64(result.zip)], { type: "application/zip" });
    archive = { name: `${plan.folder}.zip`, blob };
    return true;
  } catch {
    if (!stale() && candidate.closed) {
      batch = false;
      void start(false, "Conversion failed. Ready to try again.");
    }
    return false;
  }
}

function bytesFromBase64(text) {
  const binary = atob(text);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

// The command line's summary, in a sentence.
function folderSummary() {
  const count = (state) => results.filter((result) => result.state === state).length;
  const failed = count("bad");
  const warned = count("warn");
  const converted = results.length - failed;
  let text = `Converted ${converted} of ${fileCount(results.length)}`;
  if (failed) text += `; ${failed} could not be converted`;
  text += ".";
  if (warned) text += ` ${fileCount(warned)} with warnings: review before running.`;
  return [text, converted === 0 ? "bad" : failed || warned ? "warn" : "ok"];
}

function clearFolder() {
  plan = null;
  results = [];
  selected = -1;
  archive = null;
  elements.files.replaceChildren();
  elements["folder-errors"].replaceChildren();
  elements["folder-errors"].hidden = true;
  showSummary();
  showSelected();
}

// After a change of dialect or output kind the results are out of date: the
// files stay listed, waiting to be converted again.
function resetFolder() {
  archive = null;
  results = results.map(() => ({ state: "pending" }));
  results.forEach((result, position) => showFileState(position));
  showSummary();
  showSelected();
  updateControls();
}

function showFolderErrors(result) {
  showErrors(result, elements["folder-errors"]);
  if (result.collisions?.length) {
    const list = document.createElement("ul");
    for (const { name, output } of result.collisions) {
      const item = document.createElement("li");
      const from = document.createElement("code");
      const to = document.createElement("code");
      from.textContent = name;
      to.textContent = output;
      item.append(from, " → ", to);
      list.append(item);
    }
    elements["folder-errors"].append(list);
  }
}

// One button per file, under a heading for each folder.
function buildFileList() {
  const items = [];
  let folder = "";
  plan.files.forEach((entry, position) => {
    const slash = entry.name.lastIndexOf("/");
    const parent = entry.name.slice(0, slash + 1);
    if (parent !== folder) {
      folder = parent;
      const heading = document.createElement("li");
      heading.className = "dir";
      heading.textContent = parent;
      items.push(heading);
    }
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.position = String(position);
    const dot = document.createElement("span");
    dot.className = "dot";
    dot.setAttribute("aria-hidden", "true");
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = entry.name.slice(slash + 1);
    const tag = document.createElement("span");
    tag.className = "tag";
    button.append(dot, name, tag);
    button.addEventListener("click", () => select(position));
    const item = document.createElement("li");
    item.append(button);
    items.push(item);
  });
  elements.files.replaceChildren(...items);
  results.forEach((result, position) => showFileState(position));
}

function fileButton(position) {
  return elements.files.querySelector(`button[data-position="${position}"]`);
}

function showFileState(position) {
  const button = fileButton(position);
  if (!button) return;
  const { state } = results[position];
  button.dataset.state = state;
  button.querySelector(".tag").textContent = FILE_STATES[state];
}

function setResult(position, result) {
  results[position] = result;
  showFileState(position);
  showSummary();
  if (position === selected) showSelected();
}

function showSummary() {
  const counts = { ok: 0, warn: 0, bad: 0, pending: 0 };
  for (const { state } of results) counts[state === "busy" ? "pending" : state] += 1;
  elements["folder-count"].textContent = plan
    ? `${counts.ok + counts.warn} of ${fileCount(results.length)} converted`
    : "No files chosen";
  const segments = [];
  const legend = [];
  for (const [state, label] of [
    ["ok", "converted"],
    ["warn", "with warnings"],
    ["bad", "not converted"],
    ["pending", "waiting"],
  ]) {
    if (counts[state] === 0) continue;
    const segment = document.createElement("i");
    segment.dataset.state = state;
    segment.style.flexGrow = String(counts[state]);
    segments.push(segment);
    legend.push(legendItem(state, `${counts[state]} ${label}`));
  }
  if (plan?.skipped) {
    legend.push(legendItem("skipped", `${fileCount(plan.skipped)} skipped (not .sql or .sas)`));
  }
  elements["folder-bar"].replaceChildren(...segments);
  elements["folder-legend"].replaceChildren(...legend);
}

function legendItem(state, text) {
  const item = document.createElement("li");
  const dot = document.createElement("span");
  dot.className = "dot";
  dot.dataset.state = state;
  dot.setAttribute("aria-hidden", "true");
  item.append(dot, text);
  return item;
}

function select(position) {
  fileButton(selected)?.removeAttribute("aria-current");
  selected = position;
  fileButton(selected)?.setAttribute("aria-current", "true");
  showSelected();
}

const WAITING_OUTPUT = {
  pending: "Waiting to convert.",
  busy: "Converting…",
  bad: "Not converted: the reasons are listed below.",
};

// The selected file's source, converted code, warnings and errors.
function showSelected() {
  const entry = plan?.files[selected];
  const result = results[selected];
  const sas = entry?.dialect === "sas";
  elements["file-source-label"].textContent = entry ? (sas ? "SAS program" : "SQL") : "Source";
  elements["file-source-name"].textContent = entry?.name ?? "";
  elements["file-output-label"].textContent = notebook ? "Notebook" : "PySpark";
  elements["file-output-name"].textContent = entry?.output ?? "";

  const source = result?.sql;
  elements["file-source"].hidden = source === undefined;
  elements["file-source-empty"].hidden = source !== undefined;
  elements["file-source"].replaceChildren(source === undefined ? "" : highlightLines(source, sas ? "sas" : "sql"));
  elements["file-source-lines"].textContent = source ? lineCount(source) : "";
  elements["file-source-empty"].textContent = !entry
    ? "Choose a folder or files to convert them all at once."
    : result.state === "bad" ? "This file cannot be shown." : "Waiting to be read.";

  const code = result?.code;
  elements["file-output"].hidden = code === undefined;
  elements["file-output-empty"].hidden = code !== undefined;
  elements["file-code"].replaceChildren(code === undefined ? "" : highlightLines(code, "python"));
  elements["file-output-lines"].textContent = code === undefined ? "" : lineCount(code);
  elements["file-output-empty"].textContent = entry
    ? WAITING_OUTPUT[result.state] ?? ""
    : "The converted code appears here.";

  showWarnings(result?.warnings ?? [], elements["file-warnings"]);
  if (result?.state === "bad") {
    showErrors(result, elements["file-errors"]);
  } else {
    elements["file-errors"].replaceChildren();
    elements["file-errors"].hidden = true;
  }
  if (mode === "folder") elements.copy.disabled = code === undefined;
}

// --- Views, output kind and theme -------------------------------------------

const MODES = ["file", "folder"];
const FIRST_STATUS = {
  file: "Ready to convert.",
  folder: "Choose a folder or files to convert them all at once.",
};

function chooseMode(next) {
  if (mode === next) return;
  const previous = mode;
  mode = next;
  if (working()) {
    savedStatus[previous] = null;
    cancelConversion();
  }
  for (const name of MODES) {
    const tab = elements[`tab-${name}`];
    tab.setAttribute("aria-selected", String(name === next));
    tab.tabIndex = name === next ? 0 : -1;
    elements[`view-${name}`].hidden = name !== next;
  }
  const folder = next === "folder";
  elements["example-field"].hidden = folder;
  elements.download.hidden = folder;
  elements.share.hidden = folder;
  elements["download-zip"].hidden = !folder;
  elements.copy.disabled = folder ? results[selected]?.code === undefined : elements.output.hidden;
  updateControls();
  if (ready) {
    const saved = savedStatus[next];
    setStatus(saved?.text ?? FIRST_STATUS[next], saved?.state ?? "ready");
  }
}

function chooseOutput(asNotebook) {
  if (notebook === asNotebook) return;
  notebook = asNotebook;
  elements["output-code"].setAttribute("aria-checked", String(!asNotebook));
  elements["output-notebook"].setAttribute("aria-checked", String(asNotebook));
  elements["output-label"].textContent = asNotebook ? "Notebook" : "PySpark";
  if (mode === "folder") {
    clearOutput();
    if (ready && !working() && chosen !== null) void convertFolder();
    else resetFolder();
  } else {
    resetFolder();
    if (ready && !busy) void convert();
    else clearOutput();
  }
}

function dialectChanged() {
  if (mode === "folder") {
    showInputLanguage();
    showSource();
    clearOutput();
    if (ready && !working() && chosen !== null) void convertFolder();
    else resetFolder();
  } else {
    resetFolder();
    inputChanged();
  }
}

function effectiveTheme() {
  const chosenTheme = document.documentElement.dataset.theme;
  if (chosenTheme) return chosenTheme;
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

function save(blob, name) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
  setStatus(`Downloaded ${name}.`, "ok");
}

function download() {
  const blob = new Blob([elements.code.textContent], { type: "text/x-python" });
  save(blob, notebook ? "converted_notebook.py" : "converted.py");
}

// --- Events -----------------------------------------------------------------

elements.convert.addEventListener("click", () => (mode === "folder" ? convertFolder() : convert()));
elements.cancel.addEventListener("click", () => cancelConversion());
elements.retry.addEventListener("click", () => { void start(); });
elements.dialect.addEventListener("change", dialectChanged);
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
  const code = mode === "folder" ? elements["file-code"] : elements.code;
  await navigator.clipboard.writeText(code.textContent);
  setStatus("Copied the PySpark code.", "ok");
});
elements.download.addEventListener("click", download);
elements["download-zip"].addEventListener("click", () => {
  if (archive) save(archive.blob, archive.name);
});
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

for (const name of MODES) {
  const tab = elements[`tab-${name}`];
  tab.addEventListener("click", () => chooseMode(name));
  // Arrow keys move between tabs, as in any tab list.
  tab.addEventListener("keydown", (event) => {
    const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
    const target = event.key === "Home" ? MODES[0]
      : event.key === "End" ? MODES.at(-1)
        : step ? MODES[(MODES.indexOf(name) + step + MODES.length) % MODES.length] : null;
    if (target === null) return;
    event.preventDefault();
    chooseMode(target);
    elements[`tab-${target}`].focus();
  });
}

elements["choose-folder"].addEventListener("click", () => elements["pick-folder"].click());
elements["choose-files"].addEventListener("click", () => elements["pick-files"].click());
for (const id of ["pick-folder", "pick-files"]) {
  elements[id].addEventListener("change", () => {
    const found = filesFromInput(elements[id].files);
    // Choosing the same files again must still report a change.
    elements[id].value = "";
    chooseFiles(found);
  });
}

// Dropped files are read here; anywhere else, a drop must not make the
// browser navigate away to the file.
for (const type of ["dragover", "drop"]) {
  document.addEventListener(type, (event) => {
    if (event.dataTransfer?.types.includes("Files")) event.preventDefault();
  });
}
elements["view-folder"].addEventListener("dragover", () => elements.drop.classList.add("dragging"));
elements["view-folder"].addEventListener("dragleave", (event) => {
  if (!elements["view-folder"].contains(event.relatedTarget)) elements.drop.classList.remove("dragging");
});
elements["view-folder"].addEventListener("drop", (event) => {
  elements.drop.classList.remove("dragging");
  if (!ready || working()) return;
  filesFromDrop(event.dataTransfer).then(chooseFiles, () => {
    setStatus("The dropped files could not be read. Try Choose folder instead.", "bad");
  });
});

showThemeButton();
showSource();
void start(true);

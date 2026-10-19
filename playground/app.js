// The page owns the UI; a cancellable worker owns Python and conversion.
import { ConversionWorker, STARTUP_TIMEOUT_MS, CONVERSION_TIMEOUT_MS } from "./worker-client.js";
import { highlightLines, lineCount } from "./highlight.js";
import { filesFromDrop, filesFromInput, readSource } from "./files.js";
import { looksLikeSas, looksLikeSql } from "./language.js";
import {
  DeployError, base64, catalogAndSchema, isWorkspaceFolder, post, workspaceOrigin,
} from "./deploy.js";

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
    "open-file",
    "pick-source",
    "language-hint",
    "language-hint-text",
    "language-switch",
    "tab-deploy",
    "view-deploy",
    "convert-controls",
    "deploy-form",
    "deploy-host",
    "deploy-host-hint",
    "deploy-token",
    "deploy-folder",
    "deploy-folder-hint",
    "deploy-read",
    "deploy-read-hint",
    "deploy-write",
    "deploy-write-hint",
    "deploy-job",
    "deploy-overwrite",
    "deploy",
    "deploy-note",
    "deploy-plan",
    "deploy-next",
    "deploy-link",
    "deploy-job-name",
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
const savedStatus = { file: null, folder: null, deploy: null };
// True while notebooks are being sent to a workspace.
let deploying = false;

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

// Whether Python is ready and whether anything is converting, for whoever
// waits on the page (its tests do): the Convert button also needs input.
function showReadiness() {
  elements.statusbar.dataset.python = ready ? "ready" : "loading";
  elements.statusbar.dataset.working = String(working());
}

function updateControls() {
  const folder = mode === "folder";
  // Nothing converts until the visitor gives it something to convert.
  const input = folder ? chosen !== null : elements.sql.value.trim() !== "";
  elements.convert.disabled = !ready || working() || !input;
  elements.cancel.hidden = !working();
  elements["open-file"].disabled = !populated || working();
  showReadiness();
  elements.retry.hidden = runtime !== null;
  elements.example.disabled = !populated || working();
  // A folder's results follow the dialect and output kind, so those stay
  // fixed while its files convert.
  elements.dialect.disabled = !populated || (folder && working());
  elements["output-code"].disabled = folder && working();
  elements["output-notebook"].disabled = folder && working();
  // An empty query is nothing to share.
  elements.share.disabled = !populated || elements.sql.value.trim() === "";
  elements["choose-folder"].disabled = !ready || working();
  elements["choose-files"].disabled = !ready || working();
  elements["download-zip"].disabled = working() || archive === null;
  renderDeploy();
}

async function start(first = false, recoveryStatus = "Ready to convert again.") {
  runtime?.close();
  runtime = null;
  ready = false;
  busy = false;
  elements.retry.hidden = true;
  elements.convert.disabled = true;
  elements.cancel.hidden = true;
  showReadiness();
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
      setStatus(`Ready. SparkShift ${about.version}, running on Pyodide ${pyodideVersion}. ` +
        "Choose the source language, paste your code or open a file, then press Convert.", "ok");
      // A shared query is filled in, and waits for Convert like any other.
      loadSharedQuery();
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
    return;
  }
  const dialect = parameters.get("dialect") ?? "";
  loadSource(sql, dialect in DIALECT_NAMES ? dialect : "", "Loaded a shared query.");
}

// --- Converting -------------------------------------------------------------
//
// Nothing converts by itself: examples, opened files, and shared queries fill
// the editor, and the visitor presses Convert.

function loadExample(index) {
  const example = examples[index];
  if (example === undefined) {
    return;
  }
  const label = elements.example.options[index + 1].textContent;
  loadSource(example.sql, example.dialect, `Loaded the example “${label}”.`);
}

// New source in the editor, in ``dialect``: the old output is cleared.
function loadSource(text, dialect, message) {
  elements.dialect.value = dialect;
  setSource(text);
  inputChanged();
  if (!busy) setStatus(`${message} Press Convert (Ctrl+Enter) to see the PySpark code.`);
}

async function openFile(file) {
  // Reading "\r\n" as "\n" at most halves a file: larger ones cannot fit.
  if (file.size > 2 * maxSourceBytes) {
    setStatus(`${file.name} is larger than the browser demo's limit of ` +
      `${maxSourceBytes.toLocaleString("en-US")} UTF-8 bytes. Use the CLI for larger files.`, "bad");
    return;
  }
  const source = await readSource(file);
  if (source.error) {
    setStatus(`Cannot read ${file.name}: ${source.error}.`, "bad");
    return;
  }
  // As on the command line: a .sas file is a SAS program, and a .sql file
  // keeps the chosen SQL dialect.
  const sas = /\.sas$/i.test(file.name);
  const dialect = sas ? "sas" : elements.dialect.value === "sas" ? "" : elements.dialect.value;
  loadSource(source.text, dialect, `Opened ${file.name}.`);
}

// A source that looks like the other language gets a one-click switch;
// nothing switches by itself.
function showLanguageHint() {
  const text = elements.sql.value;
  const sas = elements.dialect.value === "sas";
  let hint = null;
  if (!sas && looksLikeSas(text)) {
    hint = ["This looks like a SAS program, not SQL.", "Switch to SAS", "sas"];
  } else if (sas && looksLikeSql(text)) {
    hint = ["This looks like SQL, not a SAS program.", "Switch to Generic SQL", ""];
  }
  elements["language-hint"].hidden = hint === null;
  if (hint === null) return;
  const [message, action, dialect] = hint;
  elements["language-hint-text"].textContent = message;
  elements["language-switch"].textContent = action;
  elements["language-switch"].dataset.dialect = dialect;
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
  showLanguageHint();
  clearOutput();
  updateControls();
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
  if (!ready || working() || !elements.sql.value.trim()) return false;
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
  // An empty editor shows its placeholder, without a line number.
  elements["sql-view"].replaceChildren(text ? highlightLines(text, language, true) : "");
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
  // Another folder: the last deployment's steps no longer apply.
  stepStates = [];
  elements["deploy-next"].hidden = true;
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

const MODES = ["file", "folder", "deploy"];
const FIRST_STATUS = {
  file: "Ready to convert.",
  folder: "Choose a folder or files to convert them all at once.",
  deploy: "Convert a folder, then deploy its notebooks to a Databricks workspace.",
};

function chooseMode(next) {
  // A deployment in progress keeps its view.
  if (mode === next || deploying) return;
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
  const deploy = next === "deploy";
  // Deploying needs no conversion controls: it uses the folder's results.
  elements["convert-controls"].hidden = deploy;
  elements.convert.hidden = deploy;
  elements["example-field"].hidden = folder;
  elements["open-file"].hidden = folder;
  elements.copy.hidden = deploy;
  elements.download.hidden = folder || deploy;
  elements.share.hidden = folder || deploy;
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
    // Shown code is converted again in the new kind; nothing converts that
    // the visitor has not converted yet.
    const shown = !elements.output.hidden;
    clearOutput();
    if (shown && ready && !busy) void convert();
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

// --- Deploying ----------------------------------------------------------------
//
// The folder's notebooks, with the form's widget values, and a Create job
// notebook are imported into a workspace with the visitor's token. Python
// writes them all first, so a notebook that no longer converts stops the
// deployment before anything is sent. The token is read from its field when
// needed, sent only to the workspace typed, and the field is cleared after a
// deployment.

const DEFAULT_HINTS = {};
// Each step's state and message in the last deployment, shown until the
// form or the folder changes.
let stepStates = [];

// The folder's files to deploy, or why there are none: a job needs every
// file, as with sparkshift upload --job.
function deployment() {
  if (!plan) {
    return { reason: "Convert a folder first, in the Folder tab: its notebooks are what is deployed." };
  }
  if (batch || results.some(({ state }) => state === "pending" || state === "busy")) {
    return { reason: "Convert the folder first: its results are not up to date." };
  }
  const failed = plan.files.filter((_, position) => results[position].state === "bad");
  if (failed.length) {
    const names = failed.map(({ name }) => name).join(", ");
    return {
      reason: `${fileCount(failed.length)} of the folder ${failed.length === 1 ? "does" : "do"} ` +
        `not convert (${names}). A job needs every file: fix ${failed.length === 1 ? "it" : "them"}, ` +
        "or choose only the files that convert.",
    };
  }
  return { files: plan.files.map((entry, position) => ({ entry, result: results[position] })) };
}

// The form's values, and a message for each field that is wrong.
function deployForm() {
  const host = elements["deploy-host"].value.trim();
  const folder = elements["deploy-folder"].value.trim().replace(/(.)\/+$/, "$1");
  const read = catalogAndSchema(elements["deploy-read"].value);
  const write = catalogAndSchema(elements["deploy-write"].value);
  const problems = {
    "deploy-host": host && !workspaceOrigin(host)
      ? "Not a Databricks workspace URL, such as https://dbc-1234.cloud.databricks.com." : "",
    "deploy-folder": folder && !isWorkspaceFolder(folder)
      ? "Use an absolute path, such as /Workspace/Users/you@example.com/sparkshift." : "",
    "deploy-read": read ? "" : "Use catalog.schema, or only a schema.",
    "deploy-write": write ? "" : "Use catalog.schema, or only a schema.",
  };
  return {
    origin: host ? workspaceOrigin(host) : null,
    folder: folder && isWorkspaceFolder(folder) ? folder : null,
    locations: [...(read ?? ["", ""]), ...(write ?? ["", ""])],
    name: elements["deploy-job"].value,
    overwrite: elements["deploy-overwrite"].checked,
    problems,
    valid: Object.values(problems).every((problem) => !problem),
  };
}

function renderDeploy() {
  if (deploying) return;
  const { files, reason } = deployment();
  const form = deployForm();
  for (const [id, problem] of Object.entries(form.problems)) {
    const hint = elements[`${id}-hint`];
    DEFAULT_HINTS[id] ??= hint.textContent;
    hint.textContent = problem || DEFAULT_HINTS[id];
    hint.classList.toggle("error", Boolean(problem));
    elements[id].setAttribute("aria-invalid", String(Boolean(problem)));
  }
  const warned = files?.filter(({ result }) => result.state === "warn").length ?? 0;
  elements["deploy-note"].textContent = reason ?? (warned
    ? `${fileCount(warned)} with warnings: review them in the Folder tab before running the job.`
    : "");
  elements["deploy-note"].hidden = !elements["deploy-note"].textContent;
  const steps = files ? [
    ["Check the workspace", form.folder ?? "the folder"],
    ...files.map(({ entry }) => [`Import ${entry.name.replace(/\.(sql|sas)$/i, "")}`, entry.name]),
    ["Import Create job", "creates or updates the job"],
  ] : [];
  elements["deploy-plan"].replaceChildren(...steps.map(([title, detail]) => planStep(title, detail)));
  stepStates.forEach(([state, message], index) => markStep(index, state, message, false));
  elements.deploy.textContent = files ? `Deploy ${files.length} notebook${files.length === 1 ? "" : "s"}` : "Deploy";
  elements.deploy.disabled = !files || !form.valid || !form.origin || !form.folder ||
    !elements["deploy-token"].value || !ready || working();
}

function planStep(title, detail) {
  const item = document.createElement("li");
  const state = document.createElement("span");
  state.className = "step-state";
  state.setAttribute("aria-hidden", "true");
  const text = document.createElement("span");
  const name = document.createElement("span");
  name.className = "step-title";
  name.textContent = title;
  const more = document.createElement("span");
  more.className = "step-detail";
  more.textContent = detail;
  // The state in words, for screen readers; the circle shows it.
  const said = document.createElement("span");
  said.className = "visually-hidden step-said";
  text.append(name, " ", more, said);
  item.append(state, text);
  return item;
}

const STEP_SAID = { busy: " (in progress)", ok: " (done)", bad: " (failed)" };

// ``remember``: keep the state, to show again when the plan is redrawn.
function markStep(index, state, message, remember = true) {
  if (remember) stepStates[index] = [state, message];
  const item = elements["deploy-plan"].children[index];
  if (!item) return;
  item.dataset.state = state;
  item.querySelector(".step-said").textContent = STEP_SAID[state];
  if (message) item.querySelector(".step-detail").textContent = message;
}

async function deploy(event) {
  event.preventDefault();
  const { files } = deployment();
  const form = deployForm();
  const token = elements["deploy-token"].value;
  if (!files || !form.valid || !form.origin || !form.folder || !token || !ready || working()) return;
  deploying = true;
  stepStates = [];
  elements.deploy.disabled = true;
  elements["deploy-next"].hidden = true;
  updateControls();
  for (const name of MODES) elements[`tab-${name}`].disabled = name !== "deploy";
  const { origin, folder, locations } = form;
  const paths = files.map(({ entry }) => `${folder}/${entry.output.replace(/\.py$/, "")}`);
  let step = 0;
  try {
    setStatus("Preparing the notebooks…", "busy");
    const sources = [];
    for (const { entry, result } of files) {
      const converted = await runtime.request("convert", {
        sql: result.sql,
        dialect: entry.dialect,
        notebook: true,
        source: entry.name.split("/").pop(),
        locations,
      }, CONVERSION_TIMEOUT_MS);
      if (!converted.ok) throw new DeployError(`${entry.name} no longer converts: ${converted.error}`);
      sources.push(converted.code);
    }
    const job = await runtime.request("job", {
      name: form.name,
      folder,
      notebooks: paths,
      locations,
    }, CONVERSION_TIMEOUT_MS);
    if (!job.ok) throw new DeployError(job.error);

    const total = paths.length + 2;
    const progress = () => setStatus(`Deploying: step ${step + 1} of ${total}…`, "busy");
    progress();
    markStep(step, "busy");
    await post(origin, token, "/api/2.0/workspace/mkdirs", { path: folder });
    markStep(step, "ok");
    const folders = new Set([folder]);
    const imports = [...paths.map((path, index) => [path, sources[index]]), [`${folder}/Create job`, job.notebook]];
    for (const [path, source] of imports) {
      step += 1;
      progress();
      markStep(step, "busy");
      const parent = path.slice(0, path.lastIndexOf("/"));
      if (!folders.has(parent)) {
        await post(origin, token, "/api/2.0/workspace/mkdirs", { path: parent });
        folders.add(parent);
      }
      try {
        await post(origin, token, "/api/2.0/workspace/import", {
          path,
          format: "SOURCE",
          language: "PYTHON",
          content: base64(source),
          overwrite: form.overwrite,
        });
      } catch (error) {
        if (error.code === "RESOURCE_ALREADY_EXISTS") {
          throw new DeployError(`A notebook already exists at ${path}. Tick “Replace notebooks ` +
            "that already exist” to replace it.");
        }
        throw error;
      }
      markStep(step, "ok");
    }
    elements["deploy-token"].value = "";
    elements["deploy-link"].href = `${origin}/#workspace${encodeURI(`${folder}/Create job`)}`;
    elements["deploy-job-name"].textContent = job.name;
    elements["deploy-next"].hidden = false;
    setStatus(`Deployed ${fileCount(paths.length).replace("file", "notebook")} and Create job to ${origin}.`, "ok");
  } catch (error) {
    const message = error instanceof DeployError
      ? error.message
      : "Preparing the notebooks failed unexpectedly. Try again.";
    markStep(step, "bad", message);
    setStatus(`Deployment stopped: ${message}`, "bad");
    if (!(error instanceof DeployError) && runtime?.closed) {
      void start(false, "Deployment stopped. Ready to try again.");
    }
  } finally {
    deploying = false;
    for (const name of MODES) elements[`tab-${name}`].disabled = false;
    updateControls();
  }
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

elements["open-file"].addEventListener("click", () => elements["pick-source"].click());
elements["pick-source"].addEventListener("change", () => {
  const [file] = elements["pick-source"].files;
  // Opening the same file again must still be noticed.
  elements["pick-source"].value = "";
  if (file) void openFile(file);
});
elements["language-switch"].addEventListener("click", () => {
  elements.dialect.value = elements["language-switch"].dataset.dialect;
  dialectChanged();
});

elements["deploy-form"].addEventListener("submit", deploy);
elements["deploy-form"].addEventListener("input", () => {
  // A changed form is another deployment: the last one's steps are history.
  stepStates = [];
  elements["deploy-next"].hidden = true;
  renderDeploy();
});

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

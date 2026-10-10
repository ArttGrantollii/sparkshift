// The SparkShift playground: loads Python (Pyodide) and SparkShift in the
// browser, and converts SQL without sending it anywhere.

const DIALECT_NAMES = {
  "": "Generic SQL",
  tsql: "T-SQL (SQL Server)",
  postgres: "PostgreSQL",
  mysql: "MySQL",
  snowflake: "Snowflake",
  bigquery: "BigQuery",
  oracle: "Oracle",
};

const elements = Object.fromEntries(
  [
    "dialect",
    "example",
    "convert",
    "share",
    "status",
    "sql",
    "output",
    "code",
    "copy",
    "errors",
  ].map((id) => [id, document.getElementById(id)]),
);

let convertForPage = null;
let examples = [];

function setStatus(text) {
  elements.status.textContent = text;
}

async function fetchOk(path) {
  const response = await fetch(path);
  if (!response.ok) {
    throw new Error(`${path}: HTTP ${response.status}`);
  }
  return response;
}

async function start() {
  const [pyodide, manifest] = await Promise.all([
    loadPyodide(),
    fetchOk("manifest.json").then((response) => response.json()),
  ]);
  setStatus("Loading SparkShift…");
  for (const wheel of manifest.wheels) {
    const buffer = await (await fetchOk(wheel)).arrayBuffer();
    pyodide.unpackArchive(buffer, "wheel");
  }
  pyodide.runPython(await (await fetchOk("bridge.py")).text());
  convertForPage = pyodide.globals.get("convert_for_page");
  const about = JSON.parse(pyodide.globals.get("about")());

  for (const dialect of about.dialects) {
    elements.dialect.add(new Option(DIALECT_NAMES[dialect] ?? dialect, dialect));
  }
  examples = manifest.examples;
  examples.forEach((example, index) => {
    const label = `${DIALECT_NAMES[example.dialect]}: ${example.name.replaceAll("_", " ")}`;
    elements.example.add(new Option(label, String(index)));
  });

  for (const control of [elements.dialect, elements.example, elements.convert, elements.share]) {
    control.disabled = false;
  }
  setStatus(`Ready. SparkShift ${about.version}, running on Pyodide ${pyodide.version}.`);
  if (!loadSharedQuery()) {
    loadExample(0);
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
  convert();
  return true;
}

// --- Converting -------------------------------------------------------------

function loadExample(index) {
  const example = examples[index];
  if (example === undefined) {
    return;
  }
  elements.sql.value = example.sql;
  elements.dialect.value = example.dialect;
  convert();
}

function convert() {
  if (convertForPage === null) {
    return;
  }
  const result = JSON.parse(convertForPage(elements.sql.value, elements.dialect.value));
  elements.errors.replaceChildren();
  elements.errors.hidden = result.ok;
  elements.output.hidden = !result.ok;
  elements.code.replaceChildren(result.ok ? highlight(result.code) : "");
  elements.copy.disabled = !result.ok;
  if (!result.ok) {
    showErrors(result);
  }
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

start().catch((error) => {
  setStatus(
    `The playground could not start (${error.message}). It needs JavaScript ` +
      "and a connection to cdn.jsdelivr.net, which serves Python for the browser.",
  );
  console.error(error);
});

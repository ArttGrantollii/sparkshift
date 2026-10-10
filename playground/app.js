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
  ["dialect", "example", "convert", "status", "sql", "code", "copy", "errors"].map(
    (id) => [id, document.getElementById(id)],
  ),
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

  for (const control of [elements.dialect, elements.example, elements.convert]) {
    control.disabled = false;
  }
  setStatus(`Ready. SparkShift ${about.version}, running on Pyodide ${pyodide.version}.`);
  loadExample(0);
}

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
  elements.code.textContent = result.ok ? result.code : "";
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

start().catch((error) => {
  setStatus(`The playground could not start: ${error.message}`);
  console.error(error);
});

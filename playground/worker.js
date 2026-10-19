// Python runs only here. Messages contain source and JSON results, never
// executable Python supplied by the visitor.
import { loadPyodide } from "https://cdn.jsdelivr.net/pyodide/v314.0.7/full/pyodide.mjs";

let convertForPage = null;
let notebookForPage = null;
let planForPage = null;
let zipForPage = null;
let jobForPage = null;

async function fetchOk(path) {
  const response = await fetch(path);
  if (!response.ok) throw new Error("Could not load the browser runtime.");
  return response;
}

// ``locations``: the notebook widgets' values, [catalog, schema,
// output_catalog, output_schema], or none for empty widgets.
self.onmessage = async ({ data }) => {
  const { id, type, sql, dialect, notebook, source, locations = ["", "", "", ""] } = data;
  try {
    if (type === "init") {
      const [pyodide, manifest] = await Promise.all([
        loadPyodide(),
        fetchOk("manifest.json").then((response) => response.json()),
      ]);
      for (const wheel of manifest.wheels) {
        const buffer = await (await fetchOk(wheel)).arrayBuffer();
        pyodide.unpackArchive(buffer, "wheel");
      }
      pyodide.runPython(await (await fetchOk("bridge.py")).text());
      convertForPage = pyodide.globals.get("convert_for_page");
      notebookForPage = pyodide.globals.get("notebook_for_page");
      planForPage = pyodide.globals.get("plan_for_page");
      zipForPage = pyodide.globals.get("zip_for_page");
      jobForPage = pyodide.globals.get("job_for_page");
      const aboutFunction = pyodide.globals.get("about");
      const about = JSON.parse(aboutFunction());
      aboutFunction.destroy();
      self.postMessage({ id, result: { about, manifest, pyodideVersion: pyodide.version } });
    } else if (type === "convert" && convertForPage !== null) {
      const result = JSON.parse(notebook
        ? notebookForPage(sql, dialect, source ?? null, ...locations)
        : convertForPage(sql, dialect));
      self.postMessage({ id, result });
    } else if (type === "plan" && planForPage !== null) {
      const result = JSON.parse(planForPage(JSON.stringify(data.paths), dialect, notebook));
      self.postMessage({ id, result });
    } else if (type === "zip" && zipForPage !== null) {
      const result = JSON.parse(zipForPage(data.folder, JSON.stringify(data.files)));
      self.postMessage({ id, result });
    } else if (type === "job" && jobForPage !== null) {
      const result = JSON.parse(
        jobForPage(data.name, data.folder, JSON.stringify(data.notebooks), ...locations),
      );
      self.postMessage({ id, result });
    } else {
      throw new Error("Unexpected worker request.");
    }
  } catch {
    // Tracebacks and pasted source never cross an unexpected-error boundary.
    self.postMessage({ id, error: true });
  }
};

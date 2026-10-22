// A notebook shown as Databricks shows it: one block per cell, Markdown
// formatted, and the setup cells, the same in every notebook, folded. The
// cells come from Python (bridge.py), which also writes the notebook file:
// Copy and Download keep using that file, unchanged.
//
// Only DOM nodes are made, never HTML from text: what a program contains
// stays text.

import { highlightLines, lineCount } from "./highlight.js";

export function renderCells(cells) {
  const fragment = document.createDocumentFragment();
  cells.forEach((cell, index) => fragment.append(renderCell(cell, index + 1)));
  return fragment;
}

function renderCell(cell, number) {
  if (cell.kind === "markdown") {
    const block = document.createElement("section");
    block.className = "cell markdown";
    block.setAttribute("aria-label", `Cell ${number}: Markdown`);
    block.append(renderMarkdown(cell.text));
    return block;
  }
  const code = document.createElement("pre");
  code.className = "code";
  // Long lines scroll sideways, so the keyboard must reach the code too.
  code.tabIndex = 0;
  code.setAttribute("aria-label", `Cell ${number}: code`);
  code.append(highlightLines(cell.text, "python"));
  if (cell.kind === "setup") {
    const details = document.createElement("details");
    details.className = "cell setup";
    const summary = document.createElement("summary");
    const title = document.createElement("span");
    title.className = "cell-title";
    title.textContent = cell.title;
    const meta = document.createElement("span");
    meta.className = "cell-meta";
    meta.textContent = `Setup · ${lineCount(cell.text)}`;
    summary.append(title, meta);
    details.append(summary, code);
    return details;
  }
  const block = document.createElement("section");
  block.className = "cell";
  block.setAttribute("aria-label", `Cell ${number}: Python`);
  const head = document.createElement("div");
  head.className = "cell-head";
  const label = document.createElement("span");
  label.textContent = `Cell ${number}`;
  const language = document.createElement("span");
  language.textContent = "Python";
  head.append(label, language);
  block.append(head, code);
  return block;
}

// The Markdown SparkShift writes: a "# " title, paragraphs, "- " lists,
// `code` (or `` code `` when it holds a backtick) and **bold**.
export function renderMarkdown(text) {
  const fragment = document.createDocumentFragment();
  for (const block of text.split(/\n[ \t]*\n/)) {
    const lines = block.split("\n").filter((line) => line.trim() !== "");
    if (lines.length === 0) continue;
    if (lines.length === 1 && /^# /.test(lines[0])) {
      const heading = document.createElement("h3");
      heading.append(inline(lines[0].slice(2)));
      fragment.append(heading);
    } else if (lines.every((line) => line.startsWith("- "))) {
      const list = document.createElement("ul");
      for (const line of lines) {
        const item = document.createElement("li");
        item.append(inline(line.slice(2)));
        list.append(item);
      }
      fragment.append(list);
    } else {
      const paragraph = document.createElement("p");
      paragraph.append(inline(lines.join(" ")));
      fragment.append(paragraph);
    }
  }
  return fragment;
}

const INLINE = /``\s?(.*?)\s?``|`([^`]+)`|\*\*([^*]+)\*\*/g;

function inline(text) {
  const fragment = document.createDocumentFragment();
  let end = 0;
  for (const match of text.matchAll(INLINE)) {
    fragment.append(text.slice(end, match.index));
    const bold = match[3] !== undefined;
    const node = document.createElement(bold ? "strong" : "code");
    node.textContent = match[1] ?? match[2] ?? match[3];
    fragment.append(node);
    end = match.index + match[0].length;
  }
  fragment.append(text.slice(end));
  return fragment;
}

// Telling a SAS program from SQL, to catch a source pasted under the wrong
// language. Only these two: SQL dialects often cannot be told apart from the
// text, and a wrong dialect changes results without an error, so the visitor
// chooses the dialect; SAS and SQL look nothing alike.

// Comments and quoted text say nothing about the language.
function withoutCommentsAndStrings(text) {
  return text
    .replace(/\/\*[\s\S]*?\*\//g, " ")
    .replace(/--[^\n]*/g, " ")
    .replace(/'(?:[^']|'')*'|"(?:[^"]|"")*"/g, "''")
    // A SAS comment statement: * text; at the start of a statement.
    .replace(/(^|;)\s*\*[^;]*;/g, "$1");
}

// A DATA step (data name; or data a b(keep=x);) or a PROC step
// (proc sort ...;) starting a line. Data set names are never SQL keywords:
// in "data AS (SELECT ...)", data is the name of a SQL CTE.
const NAME = String.raw`(?!(?:as|select|from|where|join|on|union|group|order|having|limit)\b)[A-Za-z_][\w.]*`;
const OPTIONS = String.raw`(\s*\([^)]*\))?`;
const SAS_STEP = new RegExp(
  String.raw`^\s*(data\s+${NAME}${OPTIONS}(\s+${NAME}${OPTIONS})*\s*;|proc\s+[A-Za-z_]\w*\b[^;]*;)`,
  "im",
);
const SQL_START = /^\s*(select|with)\b/i;

export function looksLikeSas(text) {
  return SAS_STEP.test(withoutCommentsAndStrings(text));
}

export function looksLikeSql(text) {
  const code = withoutCommentsAndStrings(text);
  return SQL_START.test(code) && !SAS_STEP.test(code);
}

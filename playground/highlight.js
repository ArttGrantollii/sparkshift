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
export function highlightLines(text, language, numberLastLine = false) {
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

export function lineCount(text) {
  const lines = text.replace(/\n$/, "").split("\n").length;
  return `${lines} line${lines === 1 ? "" : "s"}`;
}

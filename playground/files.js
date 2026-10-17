// The files a visitor chose or dropped, each as { path, file }: its path
// relative to what was chosen ("claims/legacy/report.sas" for a file in a
// chosen folder) and the File itself. Nothing is uploaded: files are only
// read here, in the browser.

export function filesFromInput(fileList) {
  return [...fileList].map((file) => ({ path: file.webkitRelativePath || file.name, file }));
}

// A dropped folder arrives as entries to walk. Entries must be taken while
// the drop event runs, before the first await; plain files are the fallback.
export async function filesFromDrop(dataTransfer) {
  const entries = [...dataTransfer.items]
    .map((item) => item.webkitGetAsEntry?.())
    .filter(Boolean);
  if (entries.length === 0) return filesFromInput(dataTransfer.files);
  const found = [];
  for (const entry of entries) await walk(entry, found);
  return found;
}

async function walk(entry, found) {
  if (entry.isFile) {
    const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
    found.push({ path: entry.fullPath.replace(/^\//, ""), file });
  } else if (entry.isDirectory) {
    const reader = entry.createReader();
    // readEntries returns a folder's entries in batches, then an empty one.
    for (;;) {
      const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
      if (batch.length === 0) break;
      for (const child of batch) await walk(child, found);
    }
  }
}

// { text } as the command line reads a file: strict UTF-8, a byte order
// mark kept as Python keeps it, and "\r\n" or "\r" read as "\n". Or
// { error }, why it cannot be read, in the command line's words.
export async function readSource(file) {
  let bytes;
  try {
    bytes = await file.arrayBuffer();
  } catch {
    return { error: "it could not be read" };
  }
  try {
    const text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes);
    return { text: text.replace(/\r\n?/g, "\n") };
  } catch {
    return { error: "it is not UTF-8 text" };
  }
}

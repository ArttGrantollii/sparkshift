// Talking to a Databricks workspace from the browser: only the workspace API
// calls Databricks allows from other sites (CORS), with the visitor's token.
// The token is a parameter, never stored: it lives in the page's form field
// and in the requests sent to the workspace the visitor typed.

// Databricks workspace addresses on AWS, Azure, and Google Cloud. The page's
// Content-Security-Policy allows connections to these hosts only.
const WORKSPACE_HOST = /^[a-z0-9-]+(\.[a-z0-9-]+)*\.(cloud\.databricks\.com|azuredatabricks\.net|gcp\.databricks\.com)$/;
const REQUEST_TIMEOUT_MS = 30_000;

export class DeployError extends Error {}

// The workspace's origin, such as https://dbc-1234.cloud.databricks.com, from
// what the visitor typed (a trailing path such as /?o=123 is ignored), or null.
export function workspaceOrigin(text) {
  let url;
  try {
    url = new URL(text.trim());
  } catch {
    return null;
  }
  const host = url.hostname.toLowerCase();
  if (url.protocol !== "https:" || url.port || !WORKSPACE_HOST.test(host)) return null;
  return `https://${host}`;
}

// "catalog.schema", "schema", or "" as [catalog, schema]; null if malformed.
export function catalogAndSchema(text) {
  const parts = text.trim().split(".");
  if (parts.length === 1) return ["", parts[0]];
  if (parts.length === 2 && parts.every(Boolean)) return parts;
  return null;
}

// A workspace folder: an absolute path with no "." or ".." parts.
export function isWorkspaceFolder(text) {
  const parts = text.split("/");
  return text.startsWith("/") && parts.slice(1).every((part) => part && part !== "." && part !== "..");
}

// Text as base64, after UTF-8 encoding, in chunks (no huge argument lists).
export function base64(text) {
  const bytes = new TextEncoder().encode(text);
  let binary = "";
  for (let start = 0; start < bytes.length; start += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(start, start + 0x8000));
  }
  return btoa(binary);
}

// POST a REST API call; its JSON answer. Errors are DeployErrors whose
// message never contains the token.
export async function post(origin, token, path, body) {
  let response;
  try {
    response = await fetch(`${origin}${path}`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify(body),
      // Nothing about the visitor goes along but the token itself.
      credentials: "omit",
      referrerPolicy: "no-referrer",
      cache: "no-store",
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
  } catch (error) {
    throw new DeployError(error.name === "TimeoutError"
      ? `The workspace did not answer within ${REQUEST_TIMEOUT_MS / 1000} seconds.`
      : `Could not reach ${origin}. Check the workspace URL and your connection.`);
  }
  let answer = {};
  try {
    answer = await response.json();
  } catch {
    // Some answers have no JSON body.
  }
  if (response.ok) return answer;
  if (response.status === 401 || response.status === 403) {
    throw new DeployError(
      `The workspace refused the token (HTTP ${response.status}). Use a valid ` +
      "personal access token of this workspace, with access to the folder.",
    );
  }
  const code = answer.error_code ? `${answer.error_code}: ` : "";
  let message = String(answer.message ?? response.statusText);
  if (token) message = message.replaceAll(token, "***");
  throw Object.assign(new DeployError(`Databricks answered HTTP ${response.status}: ${code}${message}`), {
    code: answer.error_code,
  });
}

# ADR 0010: upload notebooks with the Databricks SDK, as an optional extra

- **Status:** Accepted
- **Date:** 2026-10-14

## Context

`sparkshift convert --notebook` writes Databricks notebooks (ADR 0007); the
next step is putting them into a workspace without manual imports. The
Workspace API's import endpoint (`POST /api/2.0/workspace/import`) accepts a
base64-encoded source notebook with a language, and refuses to replace an
existing object unless `overwrite` is set (`RESOURCE_ALREADY_EXISTS`).

Uploading needs credentials, which SparkShift has never handled. The core must
stay a pure-Python library with one runtime dependency (SQLGlot) that also
runs in the browser playground.

## Decision

- **`sparkshift upload PATH --to FOLDER`** converts a `.sql`/`.sas` file, or
  every one in a directory, into notebooks and imports each at its relative
  path under an absolute workspace folder (`a/b.sql` becomes `FOLDER/a/b`).
  The folder is required; SparkShift never guesses where to write.
- **Use the Databricks SDK for Python**, not hand-written REST calls, for its
  unified authentication: environment variables, `~/.databrickscfg` profiles,
  and OAuth logins that avoid long-lived tokens, the same settings as the
  Databricks CLI. Hand-written calls would support tokens only, or reimplement
  that configuration.
- **As an optional extra**, `sparkshift[databricks]`. Only
  `sparkshift/databricks.py` imports the SDK, inside its functions; an
  architecture test keeps it that way. Without the extra, `upload` explains
  how to install it, and `--dry-run` works.
- **Credentials only from the SDK's own sources**, never from a command-line
  option (which would end up in shell history) or a SparkShift setting. Every
  SDK message passes through a redaction step that replaces any token or
  secret from the environment or the client's configuration with `***`.
  `.env.example` documents the variables; `.env` is git-ignored.
- **Never overwrite by default.** An existing notebook is reported as a
  failure for that file, and `--overwrite` replaces it. Every file is
  converted before connecting; a file that cannot be converted or imported is
  reported, and the others are still imported. The exit status is 1 if any
  file failed.
- **Pin the SDK conservatively.** It is labelled Beta and releases almost
  daily, so the extra allows `>=0.144,<1`, and development locks a release
  that was at least two weeks old when added (0.144.0).

## Consequences

- Installing SparkShift is unchanged; the extra adds the SDK and its
  dependencies (requests, google-auth, protobuf). The package check verifies
  that SQLGlot remains the only required dependency and the SDK the only
  optional one, and CI installs the wheel with and without the extra.
- Tests use a fake workspace and mocked SDK clients, with no network access.
  A test sets a fake token and checks that it never appears in any output.
  Importing into a real workspace is verified separately (Phase 5.4).
- Running the notebooks, creating jobs, and Databricks Asset Bundles are out
  of scope here.

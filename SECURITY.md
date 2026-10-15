# Security

## Reporting a vulnerability

Please report security issues privately, not in a public issue: use
**Report a vulnerability** on the repository's
[Security tab](https://github.com/ArttGrantollii/sparkshift/security). You
will get a reply as soon as possible, and a fix will be released before the
issue is made public.

Security fixes are made for the latest release.

## How SparkShift handles your code and credentials

- **Conversion never runs your input.** SparkShift parses SQL and SAS as
  text and returns code as text. It does not connect to a database, start
  Spark, or execute anything it converts.
- **Generated code runs where you run it.** Review it, and the warnings that
  come with it, before running it against real data. Warnings describe
  where the code may differ from the source.
- **The playground stays in your browser.** The
  [online playground](https://arttgrantollii.github.io/sparkshift/) runs
  SparkShift in your browser; your SQL is not sent to any server. A shared
  link keeps the query after the `#`, which browsers do not send to servers.
- **No telemetry.** SparkShift collects and sends no usage data.
- **Databricks credentials stay with the Databricks SDK.** `sparkshift
  upload` reads credentials only from the SDK's own sources: environment
  variables or a `~/.databrickscfg` profile. It has no option to pass a
  token, so a token never ends up in shell history, and it replaces any
  secret it knows of with `***` in its messages. Use a token with a short
  lifetime, never commit one, and never paste one into an issue or chat.
  See [ADR 0010](docs/adr/0010-upload-notebooks-with-the-databricks-sdk.md).

# ADR 0008: protect generated helper column names

## Context

SAS IF blocks sometimes need a saved condition before a branch changes the
columns that condition reads. SQL QUALIFY, HAVING and ORDER BY can need
unselected expressions as temporary columns. Fixed helper names collided
with user columns or aliases: SAS could overwrite and drop a real column,
and SQL could become ambiguous or filter on the wrong expression.

The converter has no access to input schemas. A new prefix or a random name
reduces the chance of a collision but does not establish correctness.

## Decision

For known outputs, choose helper names outside the complete output namespace,
including unselected grouping keys, conservatively comparing names without
case sensitivity, also protecting Unicode variants such as dotted and dotless
I that Spark resolves as an ASCII I. SAS FREQ uses this policy for its percentage helper too.

A SAS DATA step whose SET data set lists its columns with KEEP= has known
columns too: its IF helpers get a static name outside the kept, renamed and
assigned variables, with no schema inspection (amended 2026-10-14; at first
every SAS IF helper was bound at execution, which added about ten lines of
lookup code where the converter already knew every column).

For unknown input columns, represent a temporary name with HelperName and
references with HelperColumn. AllocateHelpers marks where names must be
bound. The emitter assigns the source to a DataFrame variable and writes code
that reads its column names, reserves visible names created by the
operation, and appends underscores until each preference is unused. Every
reference, alias, assignment and drop uses the same bound name. Independent
SQL query scopes have separate identities, even with the same preference.

SQL helper names are bound from FROM before WHERE, so inspecting schema does
not resolve a correlated filter's outer references prematurely. Cleanup uses
Column references rather than Spark's eager string-based drop path. For
EXISTS, output helper cleanup and output DISTINCT are omitted: neither
changes row existence, while Spark resolves their input schemas before
correlated outer references become available. ORDER BY, filtering and LIMIT
remain in place; tests cover EXISTS, NOT EXISTS, DISTINCT and LIMIT 0.

The core remains pure Python and imports no Spark. Schema metadata is read
only when the generated program runs, not during conversion or in the
browser. It does not collect rows, although Spark Connect can request the
schema from its server.

## Alternatives

- Reserve only names visible in the source text: insufficient with SET or *.
- Use an unusual prefix or UUID: probabilistic protection, harder to read.
- Reject a collision at execution: avoids loss but prevents a valid program
  from working when a safe name can be selected.
- Require a supplied schema: useful later, but not a prerequisite for this fix.

## Consequences

Helper-bearing SAS IF blocks whose input columns are unknown, and wildcard
QUALIFY queries, gain a short schema inspection section in their output. Generated source remains deterministic,
while actual helper names depend on the input schema. This can add a metadata
request on Spark Connect, but no row collection or additional dependency.

Tests compare values and schemas on real Spark, with collisions, nested
conditions, several helpers, mixed case and separate CTE scopes. Formatting
checks still compare Python ASTs across line widths. Existing golden IR
expectations are updated deliberately; successful ordinary queries keep their
previous layout.

The public [DataFrame.columns API](https://spark.apache.org/docs/latest/api/python/reference/pyspark.sql/api/pyspark.sql.DataFrame.columns.html)
supports both classic Spark and Spark Connect.

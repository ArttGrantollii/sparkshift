# ADR 0003: Translate through a small intermediate representation

- **Status:** Accepted
- **Date:** 2026-10-07

## Context

SparkShift needs to turn a parsed SQL query into PySpark code. Two concerns are
involved: understanding SQL semantics (which constructs are supported, and what
they mean) and writing readable Python. A SAS frontend is planned (Phase 4)
that should produce the same kind of PySpark code without going through SQL.

ADR 0001 also commits us to keeping SQLGlot-specific code contained, because
SQLGlot's syntax-tree classes change between major versions.

## Decision

Translate in two steps through SparkShift's own intermediate representation
(IR) of DataFrame operations:

```text
SQLGlot tree --translate.py--> IR (ir.py) --emit.py--> PySpark source
```

- `translate.py` is the only module besides `parsing.py` that uses SQLGlot. It
  decides what is supported and reports everything that is not.
- `ir.py` holds small immutable dataclasses. It starts with a single node
  (`TableScan`); each feature adds only the nodes it needs.
- `emit.py` turns IR into code and knows nothing about SQL.
- Architecture tests enforce that the IR and emitter never import SQLGlot.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Emit code directly from the SQLGlot tree | Mixes SQL analysis with Python formatting, spreads SQLGlot types through the codebase, and gives the SAS frontend nothing to reuse. |
| A full relational algebra or query-plan framework | Far more than the project needs now; the IR grows feature by feature instead. |

## Consequences

**Positive**

- SQLGlot changes affect only the frontend.
- The SAS frontend can produce IR and reuse the emitter unchanged.
- The emitter can be tested with hand-built IR, without parsing SQL.
- Counting native, fallback, and unsupported constructs for coverage reports
  falls out of the translation step naturally.

**Negative**

- An extra mapping step and more types to maintain.
- The IR must be designed carefully as it grows, so it describes DataFrame
  operations rather than mirroring SQL syntax.

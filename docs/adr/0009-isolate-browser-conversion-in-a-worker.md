# 9. Isolate browser conversion in a Web Worker

Date: 2026-10-13

## Status

Accepted

## Context

The browser demo ran Python synchronously on the main thread. Input-size limits
and exception handling cannot keep the UI responsive during a CPU-bound parser
or translator call. Users need to cancel work and recover without refreshing.
The core must remain independent of the browser and the site must remain static.

## Decision

- Run Pyodide, the pinned wheels and the JSON bridge in a module Web Worker.
  Pyodide 314.0.7 requires module workers; see its
  [worker documentation](https://pyodide.org/en/stable/usage/webworker.html).
- Keep DOM rendering and input state on the main thread. A small worker client
  owns one outstanding request, its ID and deadline. The page also tracks input
  revisions and worker identity, rejecting results from obsolete work.
- Terminate the worker for cancellation, input edits during conversion, conversion
  deadlines or runtime crashes; load a fresh runtime before another conversion.
  A cancel message cannot interrupt synchronous code blocking a worker's event loop.
- Set conservative demo deadlines of 10 seconds for conversion and 120 seconds
  for loading. They are elapsed-time policies, not benchmark claims or core limits.
  Do not automatically rerun interrupted input. Offer retry after startup failure.
- Keep source text local. No visitor-provided Python or worker URLs are executed.
  The page creates a worker from a fixed local module. CSP permits local scripts
  and the existing pinned Pyodide CDN; module imports are governed by worker-src
  as described in [MDN's Worker reference](https://developer.mozilla.org/en-US/docs/Web/API/Worker/Worker).

## Alternatives

- Stay on the main thread with a timeout: its timer cannot fire while synchronous
  Python blocks the same thread, so it cannot enforce the deadline.
- Send a cancel message to the active worker: it cannot process that message while
  blocked in conversion.
- Add server-side conversion: changes hosting and privacy requirements and adds
  infrastructure the pure-Python converter does not need.

## Consequences

The page remains responsive while Python runs, and terminating a worker stops
CPU-bound work. Restarting reloads Python and packages and may take several seconds;
normal successful conversions reuse the runtime. Initial loading, conversion and
recovery now need explicit asynchronous UI states and request correlation.

Tests execute a real infinite Python loop in the worker, verify UI timers and
cancellation/input-edit recovery, accelerate deadlines using a virtual page clock,
and inject stale messages, runtime crashes and loading failures. Existing browser
checks still verify API-equivalent output, warnings, clipboard/sharing, accessibility
and layout. Worker isolation does not guarantee bounded memory use or prevent the
browser from throttling background tabs. Timers run when the browser schedules them.

# Parallel Scheduling Design

The user explicitly requested implementation and a complete fresh rerun on
2026-10-12. No additional approval checkpoint is required for routine reversible
implementation choices. Preserve the existing six stages, admission policy,
extraction/cleaning prompts, IDs, source bindings and technical-failure isolation.

## Execution

- Run independent subjects concurrently, with eight active subject jobs by default.
- Pipeline stage 02 optionally streams book preparation into classification.
  PDF access is process-isolated, never concurrent PyMuPDF calls in one process.
  Preparation and model queues are bounded independently of input book count.
- All model-facing URLs in a run target one loopback inference gateway. It relays
  requests unchanged to the authorized API and enforces a shared 1024-request
  ceiling, including retries and tokenizer calls. Tokenizer traffic has an
  additional 64-request ceiling. Health/stats are local and require no model call.
- A failed book remains a technical failure, not DROP, but does not prevent the
  other books in its group from receiving classification. Service unavailability
  and source/config drift still fail closed.
- Empty, legitimately filtered cohorts complete classification with zero records;
  malformed inputs still fail validation.
- A deterministic size gate selects exactly 50 freshly admitted dictionaries and
  50 N1/N3 important books. Shortfalls stop with an explicit count, never relabel
  REVIEW or silently substitute old PASS results. Both tracks run sequentially.
- Fresh run folders contain frozen code/config/input hashes and no old stage
  outputs. Classification is sampled; extraction still uses each admitted book's
  complete MD with PDF format assistance as before.

## Scaling And Measurements

- Do not submit one future or read one full PDF for every input book at once.
  Bound preparation processes, ready books, in-flight model jobs and subject jobs.
- Preserve per-subject isolation, native handoff validation and per-stage elapsed
  times. Record gateway active/queued/peak counts, executor CPU/RSS, token counters,
  partial/failed/unresolved counts and completion markers separately from quality.
- GPU percentages come from the authorized Nanhu monitor, not KV-cache gauges;
  a single endpoint's metrics are not assumed to aggregate every deployment pod.

## Verification

Prove preparation/model overlap, global concurrency bounds across multiple
clients, permit release on errors/disconnects, bounded scheduling for thousands
of inputs, isolated per-book failures, empty cohorts, unchanged legacy mode,
and fail-closed source/config drift. Run the repository's offline suites, then a
real API smoke test before dispatching the complete fresh benchmark.

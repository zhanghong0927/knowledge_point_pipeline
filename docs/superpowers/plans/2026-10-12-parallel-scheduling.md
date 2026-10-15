# Parallel Scheduling Implementation Plan

> **For agentic workers:** Use executing-plans for the coordinating work and
> independent subagents for the disjoint gateway and classifier implementations.

**Goal:** Multi-subject, overlapped PDF/model scheduling with one shared 1024
request ceiling, followed by a complete fresh benchmark rerun.

**Architecture:** Add an optional streaming stage-02 interface while preserving
native artifacts. A loopback gateway applies global backpressure. The benchmark
controller bounds subject jobs and keeps the two 50-book tracks separate.

**Tech Stack:** Python 3.10+, multiprocessing, bounded queues, aiohttp gateway,
existing urllib model clients and unittest-based offline verification.

**Spec:** `docs/superpowers/specs/2026-10-12-parallel-scheduling-design.md`.

## Global Constraints

- Keep all six native stages and quality/admission criteria unchanged.
- Total model-facing concurrency is 1024, not 1024 per subject.
- PDF operations use separate processes; queues have finite limits.
- Original source and IDs remain immutable; technical failures stay distinct.
- Rerun under the user's personal NAS development directory, never local home.

## Tasks

1. Gateway: `adapters/inference_gateway.py`, `requirements-scheduling.txt`,
   `tests/test_inference_gateway.py`. `create_app(upstream, limit, max_pending,
   tokenize_limit, request_timeout, max_body_bytes, stats_path)` and CLI write
   readiness/stats; test shared caps, pass-through and permit release.
2. Dictionary streaming: `classify_books.py`, `run_structure_v6_1.py`, focused
   classification tests. CLI `prepare-run --books --out PREPARED --classification
   CLASSIFIED --config --workers --prepare-workers --max-pending`; produce the
   same native preparation/classification artifacts and isolate failed books.
3. Important-book streaming: `layout_model_validation.py`, focused tests. Match
   task 2's CLI/artifact contracts, retaining N1/N3/OTHER and evidence validation.
4. Integration: `pipeline.py`, configuration examples, README and integration
   tests. `structure_streaming.enabled` opts in; defaults preserve legacy plans.
5. Benchmark: fresh controller/configuration and code snapshot under the NAS;
   bounded multi-subject execution, measurements and explicit completion/partial/failure
   reporting. The 2026-10-12 follow-up supersedes the hard quota: continue with 48
   verified dictionaries; do not rerun their completed front stages or relax admission.
6. Verify: failing tests before implementation, targeted tests then full offline
   suites, one fresh code review, local gateway load/overlap tests, real API smoke,
   and detached fresh dispatch. Check native progress beyond process existence.

## Review Focus

- Crashed/slow producers must not strand model consumers or fabricate DONE.
- Tokenizer traffic and retries must not escape the common request limit.
- Large rosters must not create an unbounded future/HTTP/task backlog.
- Source/config drift and failed records must remain isolated at handoff.
- Selecting 50 books must not mutate classification evidence or native manifests.

## Progress

- [x] Inspect current code and failed benchmark; reflect approved design.
- [x] Implement gateway and both streaming classifiers.
- [x] Wire optional stage-02 streaming and bounded multi-subject scheduling.
- [x] Run offline/load/overlap verification and review (Linux suites plus empty-hook regression; historical-fixture skips separate).
- [x] Freeze fresh assets, smoke-test service and dispatch both test tracks.

Dispatch evidence: fresh run `pipeline_benchmark_100books_20261012_03`, PID 66056,
started 2026-10-12 13:25 CST. At the initial verification, eight front jobs were
active and 49 real chat requests were in flight (peak 50); no gateway errors.
The dictionary track starts first and the important-book track is chained after
it. This is launch/liveness evidence, not completion or quality acceptance.
The final Linux suites ran 568 tests: 565 passed, 3 historical-fixture skips.

## Cross-Stage Follow-Up

The user approved immediate extraction after book-level verified classification,
processing the existing 48-book cohort without waiting for 50, one shared 1024
request limit, and independent cached taxonomy-boundary preparation at startup.

- [x] Add red/green tests for short cohorts, bounded cross-stage work and subject-local mounting.
- [x] Implement `bounded_pipeline` and native six-stage book-level controller; old snapshots remain immutable.
- [x] Add boundary-only prewarming and transport-stable, strict asset reuse.
- [x] Verify offline suites, provenance reuse and fresh frozen plans.
- [x] Dispatch the 48-book continuation and verify native extraction startup; important-book track follows automatically.

Follow-up dispatch: `pipeline_benchmark_100books_20261012_04`, launched at
2026-10-12 14:22 CST with PID 72937. The 48 dictionary approvals were revalidated
without model calls or changes to the previous run. Initial observation: eight
books executing stage03, nine subject trees configured for parallel prewarming,
66 active boundary chat requests; extraction-model traffic had not yet started.
Final Linux validation ran 598 tests, 595 passed and 3 historical-fixture skips.
Launch evidence is not full-flow completion, GPU saturation or semantic quality acceptance.

Runtime correction: run04's native extractor waited for the entire shared API to
be idle, conflicting with boundary prewarming. It was stopped with zero book
extraction chat requests, preserving all artifacts. Run05 (PID 76507) started at
14:37 CST with job-scoped HTTP drain gates; real extraction traffic and a global
request peak of 809 were observed. No frozen run05 source was modified.

Large-book tokenize requests also exposed HTTP413 range-planning failures. The
new snapshot treats only that transport envelope rejection as a smaller-range
planning signal; no token counts or book text are invented or removed. Run06
is queued after run05, retries only explicit413 failed books, then combines
verified dictionary outputs through native same-branch dedup. Successful books
and important-book outputs are not rerun. This is an internal benchmark result,
not publication to a formal delivery directory. Final offline suites ran 608
tests, 605 passed and 3 historical-fixture skips; recovery execution is pending.

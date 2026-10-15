# Stage-separated 16-card scheduling implementation plan

> **For agentic workers:** Use subagent-driven-development for the disjoint gateway and native-round components, followed by integration and one complete independent review. Do not commit or publish unrelated pre-existing dirty changes.

**Goal:** Implement stage-separated dictionary and important-book runs with a declared 16-card, 512-request budget and at most 30 admitted books per track.

**Architecture:** A single controlled loopback gateway owns the hard cap and phase transitions. A new bounded controller separates model phases and coordinates cohort-wide native retry rounds. Existing frozen runs and legacy native defaults remain intact.

**Tech Stack:** Python 3.10+, aiohttp 3.14.5, stdlib/unittest, existing native modules.

**Spec:** `docs/superpowers/specs/2026-10-12-stage-separated-16card-design.md`.

## Global constraints

- Never change, terminate or relaunch remote run05/run06; no real model calls or automation creation.
- Preserve the 505-file dirty source baseline archived outside the repository in `outputs/pipeline_stage_separation_20261012_16card/SOURCE_BASELINE.zip`.
- Defaults: 16 declared cards, 512 global requests, retry caps128/32, target30 per track. Actual GPU deployment remains unverified.
- Reuse only validated successful caches; retain source/provenance checks and native failure semantics.
- All model phases are mutually exclusive, with bounded within-phase parallelism and CPU preparation.

## Review focus

- Lowering a cap with requests in flight must stop admissions without losing permits or deadlocking on paused queues.
- A partial HTTP response or upstream504 cannot count as a successful model response; queue waits cannot silently masquerade as model overload.
- Cohort retries cannot replay successful requests, skip rounds, change payload identity or overwrite unrelated extraction results.
- Important-book technical failures must not become admitted N1/N3 or semantic DROP; its rule extraction must remain the native route.
- No target50/limit1024 literals may silently override the new declared profile; old frozen and compatibility modes stay explicit.

## Task 1: controlled gateway

Files: `adapters/inference_gateway.py`, new `adapters/gateway_control.py`, new `tests/test_gateway_phase_control.py`; gateway owner does not edit controller files.

Produces: `POST /_scheduler/control` with fields`limit`, `paused`, `phase`; `transition_phase(api_url, limit, phase, timeout=1800, poll_interval=0.1) -> dict`; stats expose hard/effective limits, pause/phase, HTTP statuses and queue wait separately.

- [x] Write and run RED tests for shrinking shared concurrency, paused admission with pending requests, drain timeout, control validation, HTTP504 accounting, cancellation and recovery.
- [x] Implement the minimal dynamic admission counter with correct single-release lifecycle; keep legacy relay/streaming contracts.
- [x] Run new tests and existing gateway tests GREEN; record commands and output in the task report.

## Task 2: native dictionary cohort rounds

Files: `modules/dictionary/src/run_fullbook_v5.py`, `adapters/dictionary_extraction.py`, new focused tests under`modules/dictionary/tests`; extraction owner does not edit controller or gateway files.

Produces: driver CLI`--only-round 1|2|3`, `--finalize-only`, persistent round/history/manifest identity, and a finalization-only adapter path. Legacy no-flag execution retains serial native rounds.

- [x] RED test separate processes for rounds1/2/3, round skipping rejection, success-cache zero additional requests, identity drift, partial and exhausted504 finalization, and native legacy behavior.
- [x] Implement resumable single-round orchestration and finalization without inventing native statuses or weakening source guards.
- [x] GREEN new and existing driver/transport/handoff tests, record exact commands and output.

## Task 3: two-track phase coordinator and resource profile

Files: new`staged_benchmark.py`, new`stage_scheduling.py`, `benchmark_support.py`, `batch_pipeline.py`, `pipeline.py`, relevant important-book classifier/cleaner retry flags, new`tests/test_stage_separated_benchmark.py` and`tests/test_resource_profile.py`; coordinator owner does not edit task1/2 files.

Consumes: task1 control client, task2 single-round driver/finalization CLI, existing source/admission/cache helpers.

- [x] RED tests for both tracks' phase barriers, CPU preparation bounds, dictionary cohort-wide retry and important technical-only retry selection, profile-derived limits and target30/shortfall behavior.
- [x] Implement declared resource profile, explicit new entrypoint, sequential native model phases and bounded phase pools; support selected native steps where needed without changing legacy defaults.
- [x] Coordinate retry caps512/128/32 using the gateway; finish each stage and restore ordinary cap only at the next phase; use unchanged successful caches.
- [x] Parameterize bookkeeping/configuration, preserve fresh source verification and immutable native outputs; test no real service/card claim and no implicit launch.
- [x] GREEN new tests plus existing controller, provenance, important-book and pipeline regressions; provide task report.

## Task 4: integration, documentation and independent review

- [x] Update README and a dedicated16-card/30-book example; explain staged entry, capacities, retry scope and no deployment claim.
- [x] Run the full offline contract suites with external network disabled; run Linux-only checks in a separate new scratch directory if required, with literal-loopback fake APIs only.
- [x] Compare the final workspace against SOURCE_BASELINE, check whitespace/static plans and absence of unrelated changes.
- [x] Independent review of the complete change; fix material findings with failing regression tests.
- [x] Save source snapshot, verification report and review evidence outside frozen runs. Recheck05/06 read-only. Deliver code and test evidence, without claiming real semantic acceptance or throughput.


## Native extensions discovered during integration

- [x] Task5: dictionary cleaning native cohorts, successful initial/review-context checkpoints, explicit transport failure kind, guarded zero-HTTP finalization and export.
- [x] Task6: routing/review cohorts, per-request success cache, checkpoint/event tamper rejection and unchanged native dispositions.
- [x] Task7: unreviewed full-tree boundary cohorts; validated cache zero HTTP; exhausted fallback happens at offline finalization and keeps original failures.
- [x] Task8: metadata, important MD, dictionary primary and PASS challenge cohorts; checkpoint successful responses before handling another failure; preserve native REVIEW and frozen source identity.
- [x] Independent review findings fixed with failing regressions: complete control bodies, successful-context/layer replay, checkpoint drift, real RUN/code binding, permanent HTTP retry selection, streaming manifest fields and missing final-output/technical dispositions.

User-authorized scope update: the user confirmed the original API was restarted with16 cards and allowed necessary real tests. A separate new-gateway interface smoke used one short real generation, plus models/tokenize checks; it does not start either30-book cohort. Frozen05/06 remain untouched. Offline tests continue to block all external sockets including subprocesses.

Final execution evidence is in the task output directory: Linux1100 tests (1097 pass/3 existing skips), independent final review closed, and background run pipeline_staged_30books_16card_20261012_01/PID113296 accepted at18:13:07. The deployed source is the exact tested final_candidate archive; this completion checkbox update is documentation only and does not hot-edit the running source.

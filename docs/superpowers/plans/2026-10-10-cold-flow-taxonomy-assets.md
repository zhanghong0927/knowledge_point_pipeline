# Cold Flow and Reusable Taxonomy Assets

## Scope

Run a fresh, bounded batch from original 0611 catalog rows through screening,
MD audit, dictionary structure approval, full-book LLM extraction, separate
dictionary cleaning, boundary generation, mounting, path review and dedup.
Only `entry_prose` books enter extraction. Boundary model review is off for
this workflow; final mounting path review remains enabled. Unresolved items
remain isolated. No changes to formal deliveries or the original taxonomy.

The user has authorized implementation and a new-data run without repeated
approval. Reuse the existing `codex/dictionary-routing-cleaning` checkout and
its authorized dirty boundary changes. Freeze code and configuration before
starting production calls; do not manually execute intermediate stages.

## Tasks

1. Add tested, immutable taxonomy asset publication after boundary verification.
   Save the source tree, enriched tree, cards, full generation evidence and
   checksums. Key by source/input identity, subject, policy, review mode,
   generator version and model. No knowledge records or credentials in assets.
2. Add optional `mounting.boundaries.cache_dir`. Exact matching assets are
   verified and imported without new boundary model calls. Different source
   trees or policy versions are cache misses; corrupt matches fail closed.
   Existing `reuse_from` remains compatible but may not be combined with cache.
3. Run focused tests and Linux integration/native suites; review the changed
   interfaces. Select a fresh transport dictionary batch with local original
   MD/PDF assets, fetch its unchanged original 0611 rows and add seeded controls.
   Preserve row provenance; selection is purposeful, not population sampling.
4. Freeze one configuration and invoke all six stages once. Record task times,
   automatic retry/partial states and source/code hashes. Observe only; no
   external manual handoffs or edits to the frozen running code.
5. Verify final dispositions, actual mounted outputs and a bounded source-based
   sample. Verify saved taxonomy identity, checksums and no-call reuse offline.
   Deliver the configuration, logs, reusable tree assets and acceptance report.

## Review Focus

- A cache may never convert missing/failed semantic generation into approval.
- Boundary `off` status must remain unreviewed; empty fallbacks remain empty.
- Changing the tree, prompt, mode or failure policy cannot reuse an old asset.
- Partial extraction must not send unresolved candidates to cleaning.
- The final report must distinguish full-flow execution from semantic quality.

## Progress

- Context: existing six-stage scheduler and previous A/B evidence inspected.
- Design: retain boundary generation, skip its semantic review, retain path review.
- Tasks 1-2: implemented; nine focused tests passed after expected RED failures.
- Read-only review: generation configuration added to identity; extra evidence
  rejected; differing concurrent outputs get distinct immutable content versions.
- Initial Linux baseline: all six suites passed (three native feature tests skipped).
- Preflight selected 34 unchanged raw rows and both new MD/PDF pairs. This initial
  preparation was not executed; a new root will freeze the reviewed final code.
- Task 3: final Linux validation passed, 474 tests passed and three fixture-dependent
  tests skipped. Local cache regression passed all nine tests; diff check passed.
- Task 4: frozen run `_02` stopped at admission with zero approved books. Its
  screening/MD/classification evidence and failed execution record are preserved;
  no manual approval or production-stage patch was performed.
- Task 4: fresh run `_03` started once with 35 original 0611 rows. All three target
  dictionaries passed screening/MD audit; two passed classification admission and
  entered full-book extraction, while one remains in the review queue.
- Task 5: frozen `_03` finished with one scheduler invocation and no manual
  production-stage handoffs. Two admitted books were both partial: 3095 validated
  extraction records advanced, while 83 unresolved items stayed isolated. Cleaning
  retained 2976, mounting retained 1242, and same-branch dedup retained 1222 with
  original IDs/payloads unchanged. Total wall time was 6908.514 seconds.
- Task 5: source/code/input hashes, taxonomy identity and final dispositions passed
  operational acceptance. The saved 1667-node taxonomy reused with zero new model
  calls; 26 empty boundary fallbacks remain explicitly unreviewed. A balanced,
  fixed-seed final sample of 24 records found 22 without obvious issues, one
  cross-reference marker residue and one potentially over-narrow mounting path.
  This is source-based Codex inspection, not independent human or full semantic
  approval. Evidence/report is under the run's `acceptance/` directory.
- Resource follow-up: the user reports the API now has eight cards. Add an
  optional extraction request-concurrency list and an eight-card profile with
  stage caps of 256 and retry rounds 256/64/16. Do not alter the running `_03`
  snapshot, cancel submitted requests or attribute its timings to the new profile.
- Resource follow-up: focused configuration/native tests passed, including an
  actual adapter-to-driver-to-offline-runner subprocess chain for all three rounds.
  Read-only review found no worker-limit bypass; real eight-card throughput remains
  unmeasured. The legacy default is unchanged for existing callers.
- Resource follow-up: the deployed eight-card source passed seven Linux suites,
  with 513 tests passed and three missing-fixture tests skipped. Its archive hash
  matches the uploaded copy. No production model job was launched with this profile.

# Full-book extraction V5

## Scope

This run processes every book in `books.json`, using the entire MD text. The runner
partitions oversized books internally without dropping owned units. PDF text is
not substituted for MD: aligned font/heading evidence is supplementary. Six EPUB
originals in the current 113-book inventory have no PDF evidence; preflight records
this explicitly. This is extraction, not the downstream two-layer name screening
or definition/description separation pipeline.

## Quality gates

- Names and body selections must resolve to literal source character offsets.
- Bilingual fields use original names only. No translation or OCR reconstruction.
- Ambiguous duplicate anchors require evidence; different entries cannot substitute
  for a failed repair. Candidate repair failure does not discard valid neighbors.
- Body cleanup only deletes supported noise or incomplete tails. Deleted spans are
  recorded. Empty bodies and coverage warnings are not semantic certification.
- Structural risks are quarantined, not silently counted as repaired entries.
- Leading/trailing context stays separate from `raw_content`.

## Run

Use Python 3.10+ and the frozen dependency directory recorded in `LAUNCH.json`.
Run `prepare_fullbook_v5.py --inventory INVENTORY --reference-run ORIGINAL_RUN --out RUN` first. It verifies
source hashes and full-MD coverage for PDF caches without reducing the manifest.
The reference inventory restores S057, a table-structured book excluded from an old
112-book test list. Structural difficulty is not an input exclusion for this run.

Run `run_fullbook_v5.py --help` for the orchestration CLI. Its default stages are
1024, 256 and 64 workers, with 32 books scheduled concurrently. Each new stage waits
for visible backend running/waiting gauges to be zero twice. Metrics do not prove
all possible backend traffic is absent.

Only HTTP504 requests are retried in the later transport rounds. Each request slot
has at most one transport attempt per round. Semantic repair is separately bounded
to three slots. Output-limit discovery responses split their owned source range;
malformed/empty schema responses are not treated as successful empty extraction.

## Outputs

- `PREFLIGHT.json`, `books.json`: scope and immutable source inventory.
- `SOURCE_HASHES.json`, `source/`: frozen implementation.
- `full/ORCHESTRATION_STATUS.json`: driver state, current phase and timings.
- `full/ROUNDS.json`: per-round execution, metric gates and summary snapshots.
- `full/SUMMARY.json`: current process book totals and HTTP504 counters.
- Per-book `INPUT.json`, `units.json`, `responses/`, `errors/`, `chunks/` preserve
  identities, exact responses, failures and checkpoint coverage.
- `entries.json` contains all technically grounded records;
  `accepted_entries.json` and `quarantined_entries.json` partition them.
- `semantic_quality_approved=false` is intentional. Technical completion does not
  mean human-reviewed or semantically error-free.

`completed` means all owned units passed the technical extraction protocol.
`partial` retains finished entries plus unresolved ranges/candidates. Exit code 2
is a normal partial completion, not success; other failures stop orchestration.
Do not edit frozen source while resuming checkpoints. Do not resume an older frozen
run concurrently. Do not remove locks from a live process.

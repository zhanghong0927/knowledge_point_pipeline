# V46 bounded content and evidence regression

This revision retains the existing pipeline order. It does not re-extract whole
books or rerun v22/v9 name and subject screening. Inputs are 200 previously
audited retained entries and 200 disjoint, randomly sampled retained entries
from the same 1850-record batch. Results remain isolated from production.

## Changes

- `content_units_v46.py`: source-offset-preserving quote/bracket-aware sentence
  units. High-confidence standalone source notes depend on retention of the
  preceding source unit in the same language. Caller-supplied layout lines are
  excluded without generic person-name deletion.
- `clean_boundary_v46.py`: adds previous heading sequence, surrounding MD text,
  PDF head formatting, and image-adjacent caption evidence. The heading sequence
  is evidence, not an automatically inferred parent hierarchy.
- Definition selection and final alignment require direct identification of
  the current subject. Reliable descriptions may survive an empty definition.
- Existing final alignment also checks independent source-entry status and
  full title scope. Evidence-supported internal sections, index/resource items,
  running headers and incomplete names are not retained. Uncertain evidence is
  separately recorded as review. No fabricated name repairs are permitted.
- No additional model stage is added. All content selections remain literal
  mapped source spans. Technical failures remain failures, not content drops.

## Validation

The remote combined unit suite ran 62 tests successfully before launching the
400-record run. Semantic effectiveness must be evaluated separately, including
new problems and false exclusions among previously clean entries. Changed
fields or reduced retention alone do not prove improvement.

Run directory on wangqiyuan@10.200.48.146:

`/mnt/nas2/home/wangqiyuan/book_structure_classification_runs/20260929_clean_v46_400/`

The frozen `INPUT.json`, `BASELINE.json`, `CODE_HASHES.json`, per-cohort
`COMPARISON.json`, calls and guards preserve before/after evidence. Model
workers are configured as 1024; each cohort contains only 200 records and thus
does not fill that many independent record workers.

## Remaining limits

- This trial enriches existing extraction outputs with evidence and rejects
  unsafe names at validation. It does not claim to repair every upstream title
  split, recover missing text, or rewrite the full-book extractor.
- The rule recognizer covers only high-confidence source-note forms. Uncertain
  punctuation, prose and source damage are not guessed or rewritten.
- PDF and MD may flatten heading hierarchy. Source context can improve the
  model's decision, but cannot guarantee correct independent-entry recognition.
- Blank definitions or descriptions are permitted. Final semantic audit is
  AI-assisted review, not a claim of human approval.

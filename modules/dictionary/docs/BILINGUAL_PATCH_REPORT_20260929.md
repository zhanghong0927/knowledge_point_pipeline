# Chinese bilingual name field repair

## Scope

Literal Chinese translations previously selected as body text can be moved into
the empty `name` field. No translation or semantic rewriting is performed.
The patch requires repeated bilingual layout evidence around the source span,
complete source lines, exact text alignment, and no conflicting translation of
the same English head. Definitions and existing names are not overwritten.

`fullbook_v5_structure.py` imports the new `fullbook_bilingual_fields.py` module.
Ship both files together. Original spans and the field transfer are recorded in
`bilingual_field_repair`; IDs and English names are preserved.

## Frozen 1000-record regression

- Original flagged set: 46 records.
- Repaired: 44 records.
- Other 954 records: unchanged against the old repair function.
- Literal source checks and idempotence: 1000/1000 passed.
- N0301: conflicting source translations for the same English head; unchanged.
- N0772: Latin head `maslak` in the Chinese-name field, not a Chinese translation
  in the body. Original issue categorization was inaccurate; unchanged here.

These are sample results, not a full-corpus correction or global accuracy estimate.
No model calls were made, and production outputs were not overwritten.
The existing concurrency-16 retry process was left untouched.

## Tests

- New module: 10 tests passed, including negative and provenance cases.
- Default full suite: 217 tests run, OK with 3 fixture-dependent skips.
- With historical fixtures enabled: 223 tests run, 222 passed, 1 failed.
- The same failing historical test also fails on the unchanged original source:
  `test_real300_preliminary_healthy_rejections_are_not_systematic` requires more
  than five fixture groups but the selected replay contains zero qualifying
  groups. This is not a new patch regression. No test was weakened or removed.

## Development-machine artifacts

Host: wangqiyuan@10.200.48.146

Directory:
`/mnt/nas2/home/wangqiyuan/book_structure_classification_runs/20260929_bilingual_patch/`

- `source/`: isolated patched source and tests.
- `regress_bilingual_fields.py`: paired replay runner.
- `REGRESSION.json`: regression metrics.
- `CHANGES.json`: 44 before/after records.
- `PATCHED_1000.json`: isolated regression output.
- `TESTS.log` and `TESTS_FULL.log`: test logs.

Limit: the patch targets translation-only bilingual rows, not mixed definitions,
damaged source text, or ambiguous translations. The original full-corpus output
has not been rewritten with this patch.

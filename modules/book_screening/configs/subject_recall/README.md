# 学科宽召回配置

运行引擎时可以重复传入 `--config`，一次扫描 0611 总表生成多个学科书单：

```powershell
python scripts/stream_subject_recall_0611.py `
  --input-csv corpus_es_book_dedup_1451w_deduped_version0611.csv `
  --config configs/subject_recall/civil_engineering.json `
  --config configs/subject_recall/psychology.json `
  --output outputs/subject_broad_recall
```

新增学科时复制任一 JSON，只修改 `subject_name`、`subject_slug` 和 `boundary`：

- `aliases`：题名或元数据中的学科名称及外文名称。
- `direct_subject_labels`：`subject1/2/3` 可直接确认相关的标签。
- `adjacent_subject_labels`：可能包含本学科内容的相邻标签，不能单独触发召回。
- `strong_terms`：题名命中时强召回，其他元数据命中时弱召回。
- `weak_terms`：题名命中一个即可弱召回；摘要、简介、关键词中需至少命中两个，或与相邻学科标签共同命中。
- `cooccurrence_groups`：组内词全部出现时弱召回。
- `exclude_phrases`：仅抑制没有直接标签或题名强词支持的弱召回。

宽召回结果中的 `strong_recall` 与 `weak_recall` 都保留。硬规则排除仅代表年份、格式、语言或 MD 路径不满足后续处理要求，不代表书籍与学科无关。

宽召回完成后，将每个学科的 `满足后续处理条件.csv` 作为
`general_book_screening_pipeline.py metadata` 的输入。无需 L1 分类时不传
`--l1-config`，并用 `--audit-track 辞海类 --audit-all` 生成该学科全部辞海候选，
再执行 `audit` 和 `materialize`。

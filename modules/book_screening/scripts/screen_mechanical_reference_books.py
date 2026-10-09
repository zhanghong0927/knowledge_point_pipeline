from __future__ import annotations

import argparse
import json
from pathlib import Path

from screen_subject_reference_books import (
    classify_title,
    normalize_title_key,
    run_screening as run_subject_screening,
    screen_rows,
)


PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = PROJECT_DIR / "学科分类(1)" / "机械（仪器）.csv"
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "outputs" / "mechanical_reference_rescreen_20260902"
MANUAL_REVIEW_OVERRIDES = {
    "17898334": "属于工程机械数据交换标准中的数据字典，是否作为知识型工具书需复核。",
    "aacid__upload_files_duxiu_main__20240526T031047Z__2WjMUyLQiXBciU4p4q2RhN": (
        "属于汽车科普百科，专业词条密度和解释深度需复核。"
    ),
    "aacid__zlib3_files__20231230T013621Z__27235792__DQ6cNEhepN9Z8CtqYRuDY3": (
        "题名显示可能仅含字母 A 分卷，书籍完整性需复核。"
    ),
}


def run_screening(input_csv: Path, output_dir: Path) -> dict[str, object]:
    return run_subject_screening(
        input_csv,
        output_dir,
        subject_name="机械（仪器）",
        manual_review_overrides=MANUAL_REVIEW_OVERRIDES,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="机械学科工具书筛选兼容入口。")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    print(json.dumps(run_screening(args.input, args.output_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

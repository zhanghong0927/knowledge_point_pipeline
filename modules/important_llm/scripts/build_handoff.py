"""按白名单打包当前可运行模块，生成源码、文档、示例及逐文件校验清单。"""

import argparse
import hashlib
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

from book_extractor.models import Candidate, Record
from book_extractor.pipeline import IMPLEMENTATION_MODULES

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = (
    "build_handoff.py",
    "validate_runs.py",
    "summarize_md_runs.py",
    "screen_book_languages.py",
    "export_cleaning_delivery.py",
)
TOKENIZER_FILES = ("tokenizer.json", "tokenizer_config.json", "chat_template.jinja")
# 人工清洗抽样是独立任务，不属于本交付模块；不能只复制其测试而遗漏工具。
EXCLUDED_TESTS = {"test_review_sampling.py"}


def build(output: Path) -> Path:
    """将白名单复制到全新output，生成同名zip并验证；已有目标一律拒绝覆盖。"""
    output = output.resolve()
    archive = output.with_suffix(".zip")
    if output.exists() or archive.exists():
        raise FileExistsError("Delivery directory or archive already exists")
    files = [
        ROOT / name
        for name in (
            ".gitattributes",
            "README.md",
            "pyproject.toml",
            "requirements.lock.txt",
            "llm_services.example.json",
            "schemas/book-manifest.schema.json",
        )
    ]
    files.extend(
        ROOT / "src/book_extractor" / (name + ".py")
        for name in (*IMPLEMENTATION_MODULES, "__init__", "cli")
    )
    files.extend((ROOT / "docs/handoff").glob("*.md"))
    files.extend(
        p
        for p in (ROOT / "examples").rglob("*")
        if p.suffix in {".py", ".md", ".json", ".jsonl"}
    )
    files.extend(ROOT / "scripts" / name for name in SCRIPTS)
    files.extend(
        path
        for path in (ROOT / "tests").glob("*.py")
        if path.name not in EXCLUDED_TESTS
    )
    files.extend((ROOT / "tests/fixtures").rglob("*.json"))
    files.extend(
        ROOT / "data/tokenizers/qwen3.8-27b" / name for name in TOKENIZER_FILES
    )
    for path in files:
        if path.is_symlink() or not path.is_file():
            raise ValueError(
                f"Missing or unsafe delivery input: {path.relative_to(ROOT)}"
            )
    output.mkdir(parents=True)
    for source in sorted(set(files)):
        target = output / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    (output / ".gitignore").write_text(
        ".venv/\n.llm_services.json\n.env*\nwork/\noutputs/\ndist/\n"
        "__pycache__/\n*.py[cod]\n*.egg-info/\n.ruff_cache/\n",
        encoding="utf-8",
    )
    schemas = output / "schemas"
    schemas.mkdir(exist_ok=True)
    for name, model in (
        ("md-record-v1.schema.json", Record),
        ("md-candidate-v2.schema.json", Candidate),
    ):
        (schemas / name).write_text(
            json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    try:
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        revision = None
    checksums = {
        path.relative_to(output).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(output.rglob("*"))
        if path.is_file()
    }
    (output / "PACKAGE.json").write_text(
        json.dumps(
            {
                "format": 1,
                "source_revision": revision,
                "files": checksums,
                "note": "Source and selected samples; no secrets or run history.",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED, compresslevel=6) as stream:
        for path in sorted(output.rglob("*")):
            if path.is_file():
                stream.write(
                    path, (Path(output.name) / path.relative_to(output)).as_posix()
                )
    with zipfile.ZipFile(archive) as stream:
        if stream.testzip() is not None:
            raise ValueError("Delivery archive CRC validation failed")
        for name, expected in checksums.items():
            actual = hashlib.sha256(stream.read(output.name + "/" + name)).hexdigest()
            if actual != expected:
                raise ValueError(f"Delivery checksum mismatch: {name}")
    return archive


def main() -> None:
    """读取输出目录参数，生成交付目录与ZIP并打印路径、大小和SHA256。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    archive = build(args.output)
    print(
        json.dumps(
            {
                "archive": str(archive),
                "bytes": archive.stat().st_size,
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

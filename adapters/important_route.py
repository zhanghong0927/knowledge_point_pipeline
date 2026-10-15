#!/usr/bin/env python3
"""CLI for the shipped important-book N1/N3 routing function."""

import argparse
import json
from pathlib import Path
import sys


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--prepared", type=Path, required=True)
    p.add_argument("--classification", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--selected-books", type=Path)
    args = p.parse_args()
    scripts = Path(__file__).resolve().parents[1] / "modules/important_books/scripts"
    sys.path.insert(0, str(scripts))
    from layout_model_validation import route_extractors
    if args.out.exists():
        raise FileExistsError(f"Use a new extraction directory: {args.out}")
    selected = None
    if args.selected_books:
        rows = json.loads(args.selected_books.read_text(encoding="utf-8-sig"))
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("selected-books must be a JSON list of book objects")
        selected = [row.get("identifier") for row in rows]
        if any(not isinstance(identifier, str) or not identifier for identifier in selected):
            raise ValueError("Every selected book requires an identifier")
    result = route_extractors(args.prepared.resolve(), args.classification.resolve(), args.out.resolve(),
                              selected_refs=selected)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

"""Run native extraction and explicitly admit finalized content-only partials."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def finalized_report(manifest, out, returncode, allow_partial):
    summary = read(out / "SUMMARY.json")
    driver = read(out / "ORCHESTRATION_STATUS.json")
    books = summary["books"]
    expected = {b["identifier"] for b in read(manifest)}
    identifiers = [b["identifier"] for b in books]
    if len(identifiers) != len(expected) or set(identifiers) != expected or summary.get("finished_books") != len(expected):
        raise ValueError("Extraction output does not cover manifest books")
    statuses = {b.get("status") for b in books}
    if not statuses <= {"completed", "partial"} or driver.get("state") not in {"completed", "partial"}:
        raise ValueError("Extraction contains a technical failure or unfinished driver")
    if any(summary.get(k, 0) or any(b.get(k, 0) for b in books)
           for k in ("http504_pending_chunks", "http504_unresolved_chunks")):
        raise ValueError("Unresolved HTTP504 is a technical failure, not a content partial")
    partial = "partial" in statuses
    if returncode != (2 if partial else 0):
        raise ValueError("Native return code does not match finalized extraction status")
    if partial and not allow_partial:
        raise ValueError("Content partial requires dictionary_extraction.allow_partial=true")
    by_id = {b["identifier"]: b for b in books}
    seen, pending, files = set(), [], {}
    for accepted in sorted(out.glob("*/accepted_entries.json")):
        folder = accepted.parent
        book = read(folder / "SUMMARY.json")
        identifier = book["identifier"]
        if identifier not in expected or identifier in seen or book != by_id[identifier]:
            raise ValueError("Finalized per-book summary differs from global extraction")
        seen.add(identifier)
        entries = read(accepted)
        if len(entries) != book.get("eligible_entries") or any(
            e.get("eligible_for_name_screening") is not True or e.get("source", {}).get("identifier") != identifier for e in entries
        ):
            raise ValueError("Accepted entries do not match eligible extraction records")
        for name in ("accepted_entries.json", "quarantined_entries.json", "units.json", "SUMMARY.json"):
            path = folder / name
            files[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        for leaf in book.get("chunks", []):
            if leaf["status"] == "completed":
                continue
            key = f"{leaf['lo']:08d}_{leaf['hi']:08d}"
            path = folder / "chunks" / (key + ".json")
            chunk = read(path)
            if chunk.get("transport_retryable") or chunk.get("http504_unresolved"):
                raise ValueError("Unresolved HTTP504 in finalized chunk")
            pending.extend({**item, "identifier": identifier, "chunk": key, "chunk_status": leaf["status"]}
                           for item in chunk.get("unresolved", []))
    if seen != expected:
        raise ValueError("Missing finalized accepted_entries for manifest book")
    return {"status": "finished_with_pending" if partial else "completed", "native_returncode": returncode,
            "allow_partial": allow_partial, "completed_books": sum(b["status"] == "completed" for b in books),
            "partial_books": sum(b["status"] == "partial" for b in books),
            "accepted_records": sum(b["eligible_entries"] for b in books), "unresolved_items": len(pending),
            "artifact_sha256": files, "semantic_quality_approved": False}, pending


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-partial", action="store_true")
    args, native_args = parser.parse_known_args()
    if "--out" not in native_args or "--manifest" not in native_args:
        parser.error("Native --out and --manifest are required")
    out = Path(native_args[native_args.index("--out") + 1])
    manifest = Path(native_args[native_args.index("--manifest") + 1])
    code = subprocess.run([sys.executable, "-u", str(ROOT / "modules/dictionary/src/run_fullbook_v5.py"), *native_args]).returncode
    if code not in (0, 2):
        return code
    report, pending = finalized_report(manifest, out, code, args.allow_partial)
    (out / "HANDOFF.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (out / "UNRESOLVED.jsonl").open("w", encoding="utf-8") as stream:
        for row in pending:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

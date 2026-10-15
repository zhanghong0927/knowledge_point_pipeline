#!/usr/bin/env python3
"""Plan or execute explicit stages of the book-to-knowledge pipeline."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent


def validate_boundary_identity_url(value):
    if not isinstance(value, str) or not value or value != value.strip() or '?' in value or '#' in value:
        raise ValueError('mounting.boundaries.cache_identity_url must be an HTTP(S) URL without auth, query or fragment')
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in ('http', 'https') and bool(parsed.hostname)
                 and parsed.port != 0 and not parsed.username and not parsed.password
                 and '@' not in parsed.netloc and not parsed.path.startswith('//'))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('mounting.boundaries.cache_identity_url must be an HTTP(S) URL without auth, query or fragment')


def mounting_review_enabled(config):
    enabled = config.get('mounting', {}).get('review_enabled', False)
    if type(enabled) is not bool:
        raise ValueError('mounting.review_enabled must be a boolean')
    return enabled


def load_config(path):
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    if config["track"] not in ("dictionary", "important"):
        raise ValueError("track must be dictionary or important; run tracks separately")
    values = {"root": str(ROOT), "python": sys.executable,
              "subject": config["subject"]["name"], "slug": config["subject"]["slug"],
              "api_url": config["model"]["api_url"].rstrip("/"),
              "model": config["model"]["name"]}
    url = values["api_url"]
    if url.endswith("/v1/chat/completions"):
        url = url[:-len("/v1/chat/completions")]
    elif url.endswith("/v1"):
        url = url[:-3]
    values["api_root"] = url
    values["chat_url"] = url + "/v1/chat/completions"
    for key, value in config["paths"].items():
        if not value:
            values[key] = ""
            continue
        p = Path(value.format_map(values)).expanduser()
        values[key] = str((path.parent / p).resolve() if not p.is_absolute() else p.resolve())
    from project_taxonomy import subject
    pin=subject(values['slug'],full=False)
    values['taxonomy']=pin['taxonomy']
    values['taxonomy_dir']=str(Path(pin['taxonomy']).parent)
    return config, values


def plan(config, v):
    from project_taxonomy import subject
    v={**v,'taxonomy':subject(v['slug'],full=False)['taxonomy']}
    module = ROOT / "modules"
    run = Path(v["run"])
    screening = module / "book_screening"
    dictionary = module / "dictionary"
    important = module / "important_books"
    cleaning = module / "cleaning"
    py = v["python"]
    stages = []

    def stage(number, title):
        result = {"stage": number, "name": title, "tasks": []}
        stages.append(result)
        return result["tasks"]

    def task(tasks, name, argv, required=(), products=(), cwd=ROOT, env=None, blocked=None):
        tasks.append({"name": name, "command": [str(x) for x in argv],
                      "requires": [str(x) for x in required],
                      "produces": [str(x) for x in products], "cwd": str(cwd),
                      "env": env or {}, "blocked": blocked})

    def hook(tasks, name, products, required=()):
        commands = config.get("hooks", {}).get(name)
        if not commands:
            task(tasks, name, [], required, products,
                 blocked=f"Configure hooks.{name} as a list of argv lists; see README")
        else:
            for i, command in enumerate(commands):
                if not isinstance(command, list) or not command:
                    raise ValueError(f"hooks.{name} must contain nonempty argv lists")
                task(tasks, f"{name}_{i+1}", [str(s).format_map(v) for s in command],
                     required if i == 0 else (), products if i == len(commands)-1 else ())

    screen_run = run / "screening"
    env = {"CORPUS": v["catalog"], "SUBJECT_CONFIG": v["screening_scope"],
           "RUN_DIR": str(screen_run), "API_URL": v["chat_url"], "MODEL": v["model"],
           "META_WORKERS": str(config.get("screening_workers", 20)),
           "MD_WORKERS": str(config.get("screening_workers", 20)),
           "DICT_WORKERS": str(config.get("classification_workers", 4))}
    env.update(config.get("screening_environment", {}))
    env = {k: str(value).format_map(v) for k, value in env.items()}
    tasks = stage("01", "Book catalog screening / two tracks")
    native_recalled = str(screen_run / "01_recall" / v["slug"] / (v["subject"] + "_满足后续处理条件.csv"))
    recalled = env.get("RECALLED_CSV") or native_recalled
    dictionary_candidates = screen_run / "02_metadata/辞海类/初筛/进入MD审核0611字段.csv"
    important_candidates = screen_run / "02_metadata/其他重要书籍/初筛/严格保留0611字段.csv"
    metadata_result = screen_run / "03_important_metadata/书目大模型精筛结果.csv"
    screening_contracts = {
        1: ([v["catalog"]], [native_recalled]),
        2: ([recalled], [dictionary_candidates, important_candidates]),
        3: ([important_candidates], [metadata_result]),
        4: ([dictionary_candidates, important_candidates, metadata_result],
            [screen_run / "04_tracks/dictionary.csv", screen_run / "04_tracks/important.csv"]),
    }
    for number in (1, 2, 3, 4):
        task(tasks, f"screening_{number}", ["bash", screening / "examples/run_stages.sh", number],
             [v["screening_scope"], *screening_contracts[number][0]], screening_contracts[number][1],
             screening, env)

    tasks = stage("02", "MD quality / audited book manifest / structure classification")
    if config.get("run_md_quality_audit", True):
        md_contracts = {
            5: ([screen_run / "04_tracks/important.csv"], [screen_run / "05_important_md/最终审核结果.csv"]),
            6: ([screen_run / "04_tracks/dictionary.csv"], [screen_run / "06_dictionary_final/最终审核结果.csv"]),
            7: ([screen_run / "05_important_md/最终审核结果.csv", screen_run / "06_dictionary_final/最终审核结果.csv"],
                [screen_run / "07_final/最终审核结果.csv"]),
        }
        for number in (5, 6, 7):
            task(tasks, f"md_audit_{number}", ["bash", screening / "examples/run_stages.sh", number],
                 md_contracts[number][0], products=md_contracts[number][1],
                 cwd=screening, env=env)
    books_path = v.get("books", "")
    manifest = config.get("book_manifest", {})
    if manifest.get("enabled", False):
        audited = v.get("screening_result") or str(screen_run / "07_final/最终审核结果.csv")
        manifest_dir = run / "02_structure/book_manifest"
        command = [py, ROOT / "adapters/screened_books_to_manifest.py", "--input", audited,
                   "--out", manifest_dir, "--track", config["track"], "--subject-slug", v["slug"]]
        required = [audited]
        for field, option in (("md_root", "--md-root"), ("pdf_root", "--pdf-root")):
            if v.get(field):
                command += [option, v[field]]
                required.append(v[field])
        if config["track"] == "dictionary" and v.get("dictionary_scope"):
            command += ["--scope-config", v["dictionary_scope"]]
            required.append(v["dictionary_scope"])
        for mapping in manifest.get("path_maps", []):
            command += ["--path-map", mapping.format_map(v)]
        if env.get("MD_PREFIX") and env.get("MD_ROOT"):
            command += ["--path-map", env["MD_PREFIX"] + "=" + env["MD_ROOT"]]
        for field in ("decision_column", "track_column", "id_column", "title_column", "md_column", "pdf_column"):
            if manifest.get(field):
                command += ["--" + field.replace("_", "-"), manifest[field]]
        for field in ("assume_track", "allow_partial", "allow_empty"):
            if manifest.get(field, False):
                command += ["--" + field.replace("_", "-")]
        books_path = str(manifest_dir / "books.json")
        task(tasks, "convert_audited_books", command, required, [books_path, manifest_dir / "report.json"])
    prepared = run / "02_structure/prepared"
    classified = run / "02_structure/classification"
    classifier = (dictionary / "classification/scripts/classify_books.py" if config["track"] == "dictionary"
                  else important / "scripts/layout_model_validation.py")
    streaming = config.get("structure_streaming", {})
    if not isinstance(streaming, dict) or type(streaming.get("enabled", False)) is not bool:
        raise ValueError("structure_streaming.enabled must be boolean")
    if type(streaming.get('strict_pdf', False)) is not bool:
        raise ValueError('structure_streaming.strict_pdf must be boolean')
    if streaming.get("enabled", False):
        prepare_workers = streaming.get("prepare_workers", 2)
        max_pending = streaming.get("max_pending", 32)
        if any(type(n) is not int or n <= 0 for n in (prepare_workers, max_pending)):
            raise ValueError("structure_streaming limits must be positive integers")
        task(tasks, "stream_structure", [py, classifier, "prepare-run", "--books", books_path,
             "--out", prepared, "--classification", classified,
             "--config", v["classification_model_config"],
             "--workers", config.get("classification_workers", 4),
             "--prepare-workers", prepare_workers, "--max-pending", max_pending],
             [books_path, v["classification_model_config"]],
             [prepared / "PREPARED.json", prepared / "manifest.json",
              classified / "results", classified / "manifest.json"])
    else:
        prepare_command = [py, classifier, "prepare", "--books", books_path, "--out", prepared]
        if config['track'] == 'dictionary' and streaming.get('strict_pdf', False):
            prepare_command.append('--strict-pdf')
        if config['track'] == 'important' and manifest.get('allow_empty', False):
            prepare_command.append('--allow-empty')
        task(tasks, "prepare_structure", prepare_command,
             [books_path], [prepared / "PREPARED.json", prepared / "manifest.json"])
        task(tasks, "classify", [py, classifier, "run", "--base", prepared, "--config", v["classification_model_config"],
                                "--out", classified, "--workers", config.get("classification_workers", 4)],
             [prepared / "PREPARED.json", v["classification_model_config"]], [classified / "results", classified / "manifest.json"])
    task(tasks, "verify_classification", [py, classifier, "verify", "--out", classified], [classified / "results"],
         [classified / ("DONE.json" if config["track"] == "dictionary" else "verification.json")])

    approved_books = v.get("approved_books", "")
    if config["track"] == "dictionary" and config.get("dictionary_approval", {}).get("enabled", True):
        from adapters.approve_dictionary_books import validate_families
        families = validate_families(config.get("dictionary_approval", {}).get("allowed_families", ["entry_prose"]))
        approved_dir = run / "02_structure/dictionary_approval"
        approved_books = str(approved_dir / "approved_books.json")
        task(tasks, "approve_dictionary_books", [py, ROOT / "adapters/approve_dictionary_books.py",
             "--books", books_path, "--classification", classified, "--out", approved_dir,
             *(["--allow-empty"] if manifest.get("allow_empty", False) else []),
             "--allowed-families", *families],
             [books_path, classified / "results", classified / "DONE.json"],
             [approved_books, approved_dir / "report.json"])
        # Native dictionary verification produces this marker; important-book verification uses a different schema.
        next(t for t in tasks if t["name"] == "verify_classification")["produces"] = [str(classified / "DONE.json")]

    tasks = stage("03", "Knowledge extraction")
    extraction = run / "03_extraction"
    selection = config.get("important_selection", {})
    if not isinstance(selection, dict) or type(selection.get("enabled", False)) is not bool:
        raise ValueError("important_selection.enabled must be boolean")
    if selection.get("enabled", False) and (config["track"] != "important"
            or config.get("important_extraction", "rule") != "rule" or not v.get("approved_books")):
        raise ValueError("important_selection requires important rule extraction and paths.approved_books")
    if config["track"] == "dictionary":
        source = extraction / "source_prepared"
        task(tasks, "prepare_fullbook", [py, dictionary / "portable_pipeline.py", "prepare", "--books",
             approved_books, "--out", source], [approved_books], [source / "books.json"])
        extraction_options = config.get("dictionary_extraction", {})
        allow_partial = extraction_options.get("allow_partial", False)
        if not isinstance(allow_partial, bool):
            raise ValueError("dictionary_extraction.allow_partial must be boolean")
        round_workers = extraction_options.get("round_workers", [1024, 256, 64])
        idle_scope = extraction_options.get('idle_scope', 'service')
        if idle_scope not in ('service', 'scheduler-job'):
            raise ValueError('dictionary_extraction.idle_scope must be service or scheduler-job')
        if (not isinstance(round_workers, list) or len(round_workers) != 3
                or any(type(value) is not int or value <= 0 for value in round_workers)):
            raise ValueError("dictionary_extraction.round_workers must be three positive integers")
        task(tasks, "extract_fullbook", [py, ROOT / "adapters/dictionary_extraction.py",
             *(["--allow-partial"] if allow_partial else []), "--manifest", source / "books.json",
             "--out", extraction / "raw", "--api-url", v["api_root"], "--model", v["model"],
             "--context", config.get("context_limit", 32768), "--server-context", config.get("context_limit", 32768),
             "--output-tokens", config.get("extraction_max_tokens", 8192),
             "--round-workers", *round_workers,
             "--book-workers", config.get("book_workers", 4),
             *(['--idle-scope', idle_scope] if idle_scope != 'service' else [])], [source / "books.json"],
             [extraction / "raw", extraction / "raw/HANDOFF.json", extraction / "raw/UNRESOLVED.jsonl"])
        task(tasks, "verify_extraction_sources", [py, dictionary / "portable_pipeline.py", "clean-prepare",
             "--books", source / "books.json", "--extraction", extraction / "raw", "--out", extraction / "verified"],
             [source / "books.json", extraction / "raw"], [extraction / "verified/INPUT.json",
              extraction / "verified/MANIFEST.json", extraction / "verified/PREPARED.json"])
        default_input = extraction / "verified/INPUT.json"
        mode = "dictionary"
    elif config.get("important_extraction", "rule") == "llm":
        llm = config.get("important_llm", {})
        llm_module = module / "important_llm"
        llm_python = str(llm.get("python", "python3.12")).format_map(v)
        services = v.get("important_llm_services", "")
        env_llm = {"PYTHONPATH": str(llm_module / "src"), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONOPTIMIZE": "0",
                   "BOOK_EXTRACTOR_TOKENIZER_PATH": str(llm_module / "data/tokenizers/qwen3.8-27b")}
        work = extraction / "llm_work"
        llm_input = work / "inputs/manifest.json"
        delivery = extraction / "llm_delivery"
        task(tasks, "prepare_important_llm", [py, ROOT / "adapters/important_llm_io.py", "prepare",
             "--books", books_path, "--prepared", prepared, "--classification", classified,
             "--out", work / "inputs", "--subject-slug", v["slug"]],
             [books_path, prepared / "manifest.json", classified / "results"],
             [llm_input, work / "inputs/report.json"])
        native_cmd = [llm_python, "-m", "book_extractor.cli", "--manifest", llm_input,
                      "--services", services, "--output", work / "runs",
                      "--workers", llm.get("workers", 4), "--book-workers", llm.get("book_workers", 1),
                      "--max-connections", llm.get("max_connections", 4),
                      "--chunk-tokens", llm.get("chunk_tokens", 4000),
                      "--max-output-tokens", llm.get("max_output_tokens", 8192),
                      "--timeout", llm.get("timeout", 180), "--transport-failure-limit", llm.get("transport_failure_limit", 2)]
        if llm.get("service"):
            native_cmd += ["--service", llm["service"]]
        required = [llm_input, services, llm_module / "src/book_extractor/cli.py"]
        if "/" in llm_python:
            required.append(llm_python)
        task(tasks, "important_llm_extract", native_cmd, required, [work / "runs/batch.json"], llm_module, env_llm)
        task(tasks, "validate_important_llm", [llm_python, llm_module / "scripts/validate_runs.py", work / "runs"],
             [work / "runs/batch.json"], [work / "runs/validation.json"], cwd=llm_module, env=env_llm)
        task(tasks, "export_important_llm", [llm_python, llm_module / "scripts/export_cleaning_delivery.py", work, delivery],
             [llm_input, work / "runs/batch.json", work / "runs/validation.json"],
             [delivery / "books.json", delivery / "checksums.json"], llm_module, env_llm)
        task(tasks, "consolidate_important_llm", [py, ROOT / "adapters/important_llm_io.py", "consolidate",
             "--delivery", delivery, "--work", work, "--out", extraction / "llm_clean_input",
             "--subject", v["subject"], "--subject-slug", v["slug"]],
             [delivery / "books.json", delivery / "checksums.json"], [extraction / "llm_clean_input/records.jsonl"])
        default_input = extraction / "llm_clean_input/records.jsonl"
        mode = "standard"
    else:
        if config.get("important_extraction", "rule") != "rule":
            raise ValueError("important_extraction must be rule or llm")
        command = [py, ROOT / "adapters/important_route.py", "--prepared", prepared,
                   "--classification", classified, "--out", extraction]
        required = [prepared / "PREPARED.json", classified / "results"]
        if selection.get("enabled", False):
            command += ["--selected-books", v["approved_books"]]
            required.append(v["approved_books"])
        task(tasks, "extract_N1_N3", command, required, [extraction / "knowledge_points.csv"])
        default_input = extraction / "knowledge_points.csv"
        mode = "important"

    tasks = stage("04", "Track-specific knowledge cleaning")
    raw = v.get("knowledge_input") or str(default_input)
    mode = config.get("knowledge_input_format", mode)
    if config["track"] == "dictionary":
        if mode != "dictionary":
            raise ValueError("Dictionary cleaning requires verified native dictionary INPUT.json, not normalized standard records")
        cleaning_books = v.get("cleaning_books") or str(extraction / "source_prepared/books.json")
        native_clean = run / "04_cleaning/dictionary"
        task(tasks, "clean_dictionary", [py, ROOT / "adapters/dictionary_cleaning.py",
             "--input", raw, "--books", cleaning_books, "--out", native_clean,
             "--export", run / "04_cleaning/export", "--subject", v["subject"], "--slug", v["slug"],
             "--api-url", v["api_root"], "--model", v["model"],
             "--workers", config.get("cleaning_workers", 32), "--context-limit", config.get("context_limit", 32768)],
             [raw, cleaning_books, Path(raw).parent / 'MANIFEST.json', Path(raw).parent / 'PREPARED.json'],
             [run / "04_cleaning/export/records.jsonl",
              run / "04_cleaning/export/trace.jsonl", run / "04_cleaning/export/report.json",
              native_clean / "MANIFEST.json", native_clean / "STATE.json",
              native_clean / "DISPOSITIONS.jsonl", native_clean / "FINAL_RECORDS.jsonl",
              native_clean / "SUMMARY.json", native_clean / "REVIEW.jsonl", native_clean / "TECHNICAL_FAILURES.jsonl"])
    else:
        standardized = run / "04_cleaning/input"
        clean_run = run / "04_cleaning/current"
        task(tasks, "normalize_fields", [py, ROOT / "adapters/normalize_records.py", "--input", raw,
             "--format", mode, "--subject", v["subject"], "--slug", v["slug"], "--out", standardized],
             [raw], [standardized / "records.jsonl", standardized / "trace.jsonl", standardized / "report.json"])
        task(tasks, "rules", [py, cleaning / "scripts/rule_clean.py", "--input", standardized / "records.jsonl",
             "--output-dir", clean_run / "01_rule_clean"], [standardized / "records.jsonl"],
             [clean_run / "01_rule_clean/rule_pass.jsonl"])
        model_clean_command = [py, cleaning / "scripts/model_clean.py", "--input", clean_run / "01_rule_clean/rule_pass.jsonl",
             "--output-dir", clean_run / "02_model_clean", "--subject", v["subject"],
             "--subject-description", config["subject"].get("description", ""), "--taxonomy", v["taxonomy"],
             "--api-url", v["chat_url"], "--model", v["model"], "--workers", config.get("cleaning_workers", 32),
             "--batch-size", config.get("cleaning_batch_size", 8), "--max-tokens", 8192,
             "--context-chars", config.get("cleaning_context_chars", 3000), "--response-format", "schema"]
        if config.get('defer_transport_retry'):
            model_clean_command.append('--defer-transport-retry')
        task(tasks, "model_clean", model_clean_command,
             [clean_run / "01_rule_clean/rule_pass.jsonl", v["taxonomy"]],
             [clean_run / "02_model_clean/clean_standard.jsonl", clean_run / "02_model_clean/model_clean_report.json",
              clean_run / "02_model_clean/model_judgments.jsonl"])
        task(tasks, "restore_trace", [py, ROOT / "adapters/normalize_records.py", "--restore",
             "--input", clean_run / "02_model_clean/clean_standard.jsonl", "--trace", standardized / "trace.jsonl",
             "--normalization-report", standardized / "report.json",
             "--cleaning-report", clean_run / "02_model_clean/model_clean_report.json",
             "--subject", v["subject"], "--slug", v["slug"], "--out", run / "04_cleaning/export"],
             [clean_run / "02_model_clean/clean_standard.jsonl", standardized / "trace.jsonl", standardized / "report.json",
              clean_run / "02_model_clean/model_clean_report.json", clean_run / "02_model_clean/model_judgments.jsonl"],
             [run / "04_cleaning/export/records.jsonl", run / "04_cleaning/export/trace.jsonl",
              run / "04_cleaning/export/report.json"])

    tasks = stage("05", "Taxonomy mounting and optional reviewed standard export")
    mount = config.get('mounting', {})
    review_enabled = mounting_review_enabled(config)
    mount_review_mode = 'on' if review_enabled else 'off'
    boundaries = mount.get('boundaries', {})
    from project_taxonomy import subject,DEFAULT_LOCK
    project_pin=subject(v['slug'],full=False)
    generate_boundaries = True  # Published assets are mandatory, including old configs.
    boundary_review_mode = project_pin['review_mode']
    boundary_cache = None
    boundary_identity_url = None
    mounting_inputs = mount.get('inputs', ['{run}/04_cleaning/export/records.jsonl'])
    if not isinstance(mounting_inputs, list) or not mounting_inputs or any(not isinstance(s, str) or not s for s in mounting_inputs):
        raise ValueError("mounting.inputs must be a nonempty list of cleaned record paths")
    mounting_inputs = [str(Path(s.format_map(v))) for s in mounting_inputs]
    if any(not Path(p).is_absolute() for p in mounting_inputs) or len({str(Path(p).resolve()) for p in mounting_inputs}) != len(mounting_inputs):
        raise ValueError("mounting.inputs must be distinct absolute paths or use {run}/{root}")
    if config.get('hooks', {}).get('mounting'):
        if generate_boundaries:
            raise ValueError('Semantic boundaries require the built-in mounting bridge, not a custom hook')
        if len(mounting_inputs) != 1:
            raise ValueError("Multiple mounting inputs require the built-in mounting bridge, not a custom hook")
        hook(tasks, "mounting", [run / "05_mounting/mounted_standard.jsonl"],
             [*mounting_inputs, v["taxonomy"]])
    else:
        bridge = ROOT/'adapters/mounting_bridge.py'
        mount_dir = run/'05_mounting'
        command = [py,bridge,'prepare','--require-cleaned','--out',mount_dir,
                   '--project-assets-lock',DEFAULT_LOCK,
                   '--subject-slug',v['slug'],'--threshold',mount.get('threshold',.85),
                   '--min-depth',mount.get('min_depth',2),'--max-depth',mount.get('max_depth',5),
                   '--max-related',mount.get('max_related',2),'--limit',mount.get('limit',0),
                   '--mount-review-mode',mount_review_mode]
        for cleaned in mounting_inputs:
            command += ['--input', cleaned]
        if v.get('taxonomy'):
            command += ['--taxonomy',v['taxonomy']]
        if v.get('taxonomy_dir'):
            command += ['--taxonomy-dir',v['taxonomy_dir']]
        if generate_boundaries:
            command += ['--require-boundaries', '--boundary-failure-policy', project_pin['failure_policy'],
                        '--boundary-review-mode', boundary_review_mode]
            if boundary_cache:
                command += ['--boundary-cache-dir', boundary_cache]
            if boundary_identity_url:
                command += ['--boundary-cache-identity-url', boundary_identity_url]
        task(tasks,'prepare_mounting',command,mounting_inputs,[mount_dir/'PREPARED.json'])
        api = ['--api-url',mount.get('api_url') or v['chat_url'],'--model',mount.get('model') or v['model'],
               '--workers',mount.get('workers',16),'--timeout',mount.get('timeout',600),
               '--max-tokens',mount.get('max_tokens',4096)]
        if mount.get('api_key_env'):
            api += ['--api-key-env',mount['api_key_env']]
        routing_inputs = [mount_dir/'PREPARED.json']
        if generate_boundaries:
            task(tasks, 'generate_mounting_boundaries', [py, bridge, 'activate-project-assets', '--out', mount_dir],
                 [mount_dir/'PREPARED.json',DEFAULT_LOCK], [mount_dir/'BOUNDARIES_GENERATED.json'])
            task(tasks, 'verify_mounting_boundaries', [py, bridge, 'verify-boundaries', '--out', mount_dir],
                 [mount_dir/'PREPARED.json', mount_dir/'BOUNDARIES_GENERATED.json'], [mount_dir/'BOUNDARIES_VERIFIED.json'])
            routing_inputs.append(mount_dir/'BOUNDARIES_VERIFIED.json')
        native_outputs = [mount_dir/'groups'/v['slug']/'routed.jsonl']
        task(tasks,'route_mounting',[py,bridge,'route','--out',mount_dir,*api],
             routing_inputs,[mount_dir/'ROUTED.json',*native_outputs])
        export_inputs = [mount_dir/'PREPARED.json',mount_dir/'ROUTED.json',*native_outputs]
        if review_enabled:
            task(tasks,'review_mounting',[py,bridge,'review','--out',mount_dir,*api],
                 [mount_dir/'ROUTED.json'],[mount_dir/'REVIEWED.json',mount_dir/'review_responses.jsonl'])
            export_inputs = [mount_dir/'REVIEWED.json',mount_dir/'review_responses.jsonl']
        task(tasks,'export_mounting',[py,bridge,'export','--out',mount_dir,'--mount-review-mode',mount_review_mode],
             export_inputs,
             [mount_dir/'mounted_standard.jsonl',mount_dir/'SUMMARY.json'])

    tasks = stage("06", "Final merge and same-path deduplication")
    inputs = config.get("dedup_inputs") or [str(run / "05_mounting/mounted_standard.jsonl")]
    inputs = [str(Path(s.format_map(v))) for s in inputs]
    for p in inputs:
        if not Path(p).is_absolute():
            raise ValueError("dedup_inputs must be absolute or use {run}/{root}")
    command = [py, module / "dedup/dedup.py", "run", "--mode", "length", "--subject", v["slug"],
               "--out", run / "06_dedup"]
    for p in inputs:
        command += ["--input", p]
    task(tasks, "dedup", command, inputs, [run / "06_dedup/retained.jsonl", run / "06_dedup/SUMMARY.json"])
    task(tasks, "verify_dedup", [py, module / "dedup/dedup.py", "verify", "--out", run / "06_dedup"],
         [run / "06_dedup/retained.jsonl"], [run / "06_dedup/VERIFICATION.json"])
    return stages


def select_tasks(stages, names):
    """Return selected native tasks in plan order, rejecting misspelled selectors."""
    if not names:
        return stages
    wanted = set(names)
    if len(wanted) != len(names) or any(not isinstance(name, str) or not name for name in names):
        raise ValueError("Task selectors must be distinct nonempty names")
    available = {task["name"] for stage in stages for task in stage["tasks"]}
    unknown = wanted - available
    if unknown:
        raise ValueError("Unknown native task names: " + ", ".join(sorted(unknown)))
    return [{**stage, "tasks": [task for task in stage["tasks"] if task["name"] in wanted]}
            for stage in stages if any(task["name"] in wanted for task in stage["tasks"])]


def run_stages(config_path, config, values, stages, resume, retry_failed=False):
    import fcntl
    run = Path(values["run"])
    from project_taxonomy import pin_run
    if config.get('track') in ('dictionary','important'):
        pin_run(run,values['slug'])
    run.mkdir(parents=True, exist_ok=True)
    with (run / "pipeline.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        digest = hashlib.sha256(config_path.read_bytes()).hexdigest()
        frozen = run / "pipeline_config.sha256"
        if frozen.exists() and frozen.read_text().strip() != digest:
            raise ValueError("Config changed; use a new run directory")
        frozen.write_text(digest + "\n")
        state_file = run / "pipeline_state.json"
        state = json.loads(state_file.read_text()) if state_file.exists() else {}
        for stage in stages:
            for task in stage["tasks"]:
                key = stage["stage"] + "." + task["name"]
                signature = hashlib.sha256(json.dumps(task, sort_keys=True).encode()).hexdigest()
                saved = state.get(key, {})
                if saved.get("status") == "completed":
                    if not resume:
                        raise ValueError(f"{key} already ran; use --resume to skip completed tasks")
                    if saved.get("signature") != signature or not all(Path(p).exists() for p in task["produces"]):
                        raise ValueError(f"{key}: command or output changed")
                    if key in ('04.clean_dictionary', '04.restore_trace'):
                        sys.path.insert(0, str(ROOT / 'adapters'))
                        from cleaning_handoff import verify_export
                        verify_export(Path(task['produces'][0]))
                    print(f"SKIP {key}", flush=True)
                    continue
                if saved.get("status") == "running" or (saved.get("status") == "failed" and not retry_failed):
                    raise ValueError(f"{key} incomplete; follow native module recovery instructions before retrying")
                if saved.get('status')=='failed' and saved.get('signature')!=signature:
                    raise ValueError(f'{key}: failed-task command changed; use a new run directory')
                if task["blocked"]:
                    raise ValueError(task["blocked"])
                missing = [p for p in task["requires"] if not p or not Path(p).exists()]
                if missing:
                    raise FileNotFoundError(f"{key} missing handoff/input: {missing}")
                if any("MODEL_HOST" in arg or "EXACT_MODEL_ID" in arg for arg in task["command"] + list(task["env"].values())):
                    raise ValueError("Fill real model settings before execution")
                log_dir = run / "logs"
                log_dir.mkdir(exist_ok=True)
                state[key] = {"status": "running", "signature": signature, "command": task["command"],
                              **({'previous_failed_attempt':saved} if saved.get('status')=='failed' else {})}
                state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2))
                print(f"RUN {key}; log: {log_dir / (key + '.log')}", flush=True)
                env = os.environ.copy()
                env.update(task["env"])
                env["PYTHONUNBUFFERED"] = "1"
                # Shell wrappers use python3; select the current interpreter's environment.
                env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
                try:
                    with (log_dir / (key + ".log")).open("a") as handle:
                        result = subprocess.run(task["command"], cwd=task["cwd"], env=env,
                                                stdout=handle, stderr=subprocess.STDOUT)
                except OSError as exc:
                    state[key].update(status="failed", returncode=None, error=str(exc))
                    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2))
                    raise
                ok = result.returncode == 0 and all(Path(p).exists() for p in task["produces"])
                state[key].update(status="completed" if ok else "failed", returncode=result.returncode)
                state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2))
                if not ok:
                    raise RuntimeError(f"{key} failed or expected output missing; inspect log and native reports")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("plan", "check", "run"))
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--stages", default="01,02,03,04,05,06")
    p.add_argument("--tasks", help="Comma-separated native task names within selected stages")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--retry-failed", action="store_true", help="Explicitly retry failed tasks using their native recovery behavior; never clears outputs")
    args = p.parse_args()
    config, values = load_config(args.config.resolve())
    stages = plan(config, values)
    wanted = args.stages.split(",")
    if not set(wanted) <= {s["stage"] for s in stages}:
        p.error("stages must be comma-separated 01 through 06")
    stages = [s for s in stages if s["stage"] in wanted]
    if args.tasks:
        stages = select_tasks(stages, args.tasks.split(","))
    if args.action == "plan":
        print(json.dumps(stages, ensure_ascii=False, indent=2))
    elif args.action == "check":
        available = set()
        issues = []
        for stage in stages:
            for task in stage["tasks"]:
                for path in task["requires"]:
                    if path not in available and (not path or not Path(path).exists()):
                        issues.append({"task": task["name"], "missing": path})
                if task["blocked"]:
                    issues.append({"task": task["name"], "blocked": task["blocked"]})
                available.update(task["produces"])
        print(json.dumps({"issues": issues, "note": "Offline file/handoff check only; no API probes"}, ensure_ascii=False, indent=2))
        return 1 if issues else 0
    else:
        run_stages(args.config.resolve(), config, values, stages, args.resume, args.retry_failed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""First mounting -> optional evidence-gated review -> export; prepare is offline."""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import wraps
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shared_request_pool import completed as shared_completed
from round_integrity import digest_file, full_verification
from urllib import request
from urllib.parse import urlsplit

from mounting_assets import ROOT, load_module, normalize
from cleaning_handoff import verify_export

NATIVE = ROOT / "modules/mounting/mapping_runtime"
REGISTRY = ROOT / "configs/taxonomy_registry.json"
TEXT_FIELDS = ("name", "knowledge_point", "definition", "en_definition", "description", "en_description")


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def rows(path):
    with path.open(encoding="utf-8-sig") as f:
        return [json.loads(line) for line in f if line.strip()]


def sha(path):
    return digest_file(path)


def full_on_finalize(fn):
    verified = full_verification(fn)

    @wraps(fn)
    def wrapped(args, *positional, **keywords):
        if getattr(args, 'finalize_only', False):
            return verified(args, *positional, **keywords)
        return fn(args, *positional, **keywords)
    return wrapped


def dump(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def write_rows(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as f:
        for value in values:
            f.write(json.dumps(value, ensure_ascii=False) + "\n")
    temp.replace(path)


def aliases(registry):
    result = {}
    for slug, config in registry.items():
        for name in [slug, config["name"], *config.get("aliases", [])]:
            if name in result and result[name] != slug:
                raise ValueError(f"Ambiguous subject alias: {name}")
            result[name] = slug
    return result


def boundary_cache_identity_url(value):
    if value is None:
        return None
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError('Boundary cache identity URL must be an HTTP(S) URL')
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in ('http', 'https') and bool(parsed.hostname)
                 and parsed.port != 0 and not parsed.username and not parsed.password
                 and not parsed.query and not parsed.fragment and not parsed.path.startswith('//'))
    except ValueError:
        valid = False
    if not valid or '@' in parsed.netloc or '?' in value or '#' in value:
        raise ValueError('Boundary cache identity URL must be HTTP(S) without auth, query or fragment')
    return value


def mounting_review_enabled(config, mode=None):
    enabled = config.get('mount_review_enabled', False)
    if type(enabled) is not bool:
        raise ValueError('mount_review_enabled must be a boolean')
    if mode is None:
        return enabled
    if mode not in ('off', 'on'):
        raise ValueError('mount review mode must be off or on')
    return mode == 'on'


def prepare(args):
    if args.out.exists():
        raise FileExistsError(f"Use a new mounting directory: {args.out}")
    mount_review_enabled = mounting_review_enabled({}, getattr(args, 'mount_review_mode', None))
    boundary_only = bool(getattr(args, 'boundary_only', False))
    if boundary_only and (args.input or getattr(args, 'require_cleaned', False)
                          or not getattr(args, 'require_boundaries', False) or not args.subject_slug):
        raise ValueError('Boundary-only prepare requires boundaries and subject slug, without input or cleaned proof')
    identity_url = boundary_cache_identity_url(getattr(args, 'boundary_cache_identity_url', None))
    registry = read(args.registry)
    by_alias = aliases(registry)
    target_slug = by_alias.get(args.subject_slug) if args.subject_slug else None
    if args.subject_slug and not target_slug:
        raise ValueError("Add the subject and its taxonomy file to the registry first")
    if args.taxonomy and not args.subject_slug:
        raise ValueError("An explicit --taxonomy needs --subject-slug")
    inputs = [] if boundary_only else args.input if isinstance(args.input, list) else [args.input]
    if (not boundary_only and not inputs) or any(not isinstance(p, Path) for p in inputs):
        raise ValueError('At least one cleaned input file is required')
    inputs = [p.resolve() for p in inputs]
    if len(set(inputs)) != len(inputs):
        raise ValueError('Duplicate mounting input path')
    input_info = [{'path': str(p), 'sha256': sha(p)} for p in inputs]
    if getattr(args, 'require_cleaned', False):
        for path, info in zip(inputs, input_info):
            proof = verify_export(path)
            info['cleaning_report_sha256'] = sha(path.parent / 'report.json')
            info['cleaning_track'] = proof['cleaning_export']['track']
            proof_slug = by_alias.get(proof['cleaning_export'].get('subject'))
            if not proof_slug or (target_slug and proof_slug != target_slug):
                raise ValueError('Mounting cleaning proof subject does not match target')
            info['cleaning_subject'] = proof_slug
    source = []
    for path, info in zip(inputs, input_info):
        values = rows(path)
        if info.get('cleaning_subject') and any(by_alias.get(row.get('tag')) != info['cleaning_subject'] for row in values):
            raise ValueError('Mounting cleaning proof subject and record tags disagree')
        source.extend(values)
        info['records'] = len(values)
    groups, unknown, identities = {}, [], set()
    # Validate the complete handoff before applying an optional sample limit.
    for row in source:
        if not isinstance(row, dict) or not isinstance(row.get("id"), (str, int)) or isinstance(row["id"], bool) or str(row["id"]) == "":
            raise ValueError("Every standard record needs a nonempty string/integer id")
        tag = row.get("tag") or args.subject_slug
        slug = by_alias.get(tag)
        if target_slug and tag and slug != target_slug:
            raise ValueError("Mixed subjects in a single-subject pipeline input")
        identity = (str(tag), str(row["id"]))
        canonical = (slug or str(tag), str(row["id"]))
        if canonical in identities:
            raise ValueError(f"Duplicate record identity: {canonical}")
        identities.add(canonical)
        if not any(row.get(f) for f in ("name", "knowledge_point")):
            raise ValueError(f"Empty name fields: {identity}")
        if any(row.get(f) is not None and not isinstance(row[f], str) for f in TEXT_FIELDS):
            raise ValueError(f"Invalid standard text field: {identity}")
    selected = source[:args.limit] if args.limit else source
    project_lock = getattr(args, 'project_assets_lock', None)
    if project_lock is None and not getattr(args,'asset_maintenance',False):
        from project_taxonomy import DEFAULT_LOCK
        project_lock=DEFAULT_LOCK
    project_pin = None
    if project_lock:
        import project_taxonomy
        slugs = {by_alias.get(row.get('tag') or args.subject_slug) for row in selected}
        if boundary_only or not slugs:
            slugs = {target_slug}
        if None in slugs:
            raise ValueError('Project taxonomy subject is not configured')
        project_pin = project_taxonomy.binding(slugs, project_lock)
        if args.taxonomy and target_slug:
            if sha(args.taxonomy) != project_pin['subjects'][target_slug]['source_sha256']:
                raise ValueError('Task taxonomy differs from project taxonomy; update the project release explicitly')
    for row in selected:
        slug = by_alias.get(row.get('tag') or args.subject_slug)
        explicit_tree = args.taxonomy if args.taxonomy and slug == target_slug else None
        if project_pin:
            explicit_tree = Path(project_pin['subjects'][slug]['taxonomy'])
        if not slug or (not registry[slug].get("file") and not explicit_tree):
            unknown.append({"record": row, "status": "unconfigured", "reason": "subject_taxonomy_not_configured"})
            continue
        tree_path = explicit_tree or args.taxonomy_dir / registry[slug]["file"]
        if not tree_path.is_file():
            unknown.append({"record": row, "status": "unconfigured", "reason": "taxonomy_file_missing", "path": str(tree_path)})
            continue
        groups.setdefault(slug, {"rows": [], "tree_path": tree_path})["rows"].append(row)
    if boundary_only:
        configured_tree = registry[target_slug].get('file')
        if not args.taxonomy and not configured_tree:
            raise ValueError('Boundary-only taxonomy is not configured')
        tree_path = args.taxonomy or args.taxonomy_dir / configured_tree
        if project_pin:
            tree_path = Path(project_pin['subjects'][target_slug]['taxonomy'])
        if not tree_path.is_file():
            raise ValueError('Boundary-only taxonomy file is missing')
        groups[target_slug] = {"rows": [], "tree_path": tree_path}
    if any(sha(Path(info['path'])) != info['sha256'] for info in input_info):
        raise ValueError('Mounting input changed during preparation')
    args.out.mkdir(parents=True)
    write_rows(args.out / "input.snapshot.jsonl", selected)
    write_rows(args.out / "unconfigured.jsonl", unknown)
    manifest = {"inputs": input_info, "input_records": len(source),
                "selected_records": len(selected), "limit": args.limit, "unconfigured": len(unknown),
                "threshold": args.threshold, "min_depth": args.min_depth, "max_depth": args.max_depth,
                "max_related": args.max_related, "groups": {},
                "mount_review_enabled": mount_review_enabled}
    manifest['require_semantic_boundaries'] = bool(getattr(args, 'require_boundaries', False))
    manifest['boundary_failure_policy'] = getattr(args, 'boundary_failure_policy', 'strict')
    manifest['boundary_review_mode'] = getattr(args, 'boundary_review_mode', 'strict')
    cache_dir = getattr(args, 'boundary_cache_dir', None)
    manifest['boundary_cache_dir'] = str(cache_dir.resolve()) if cache_dir else None
    manifest['boundary_only'] = boundary_only
    manifest['boundary_cache_identity_url'] = identity_url
    if project_pin:
        policies={(v['review_mode'],v['failure_policy']) for v in project_pin['subjects'].values()}
        if len(policies)!=1:
            raise ValueError('Project release has incompatible boundary policies')
        review_mode,policy=policies.pop()
        manifest.update(project_assets=project_pin,require_semantic_boundaries=True,
                        boundary_review_mode=review_mode,boundary_failure_policy=policy,
                        boundary_cache_dir=None,boundary_cache_identity_url=None)
    if manifest['boundary_review_mode'] not in ('strict', 'off'):
        raise ValueError('Invalid boundary review mode')
    if len(inputs) == 1:
        manifest.update(input=str(inputs[0]), input_sha256=input_info[0]['sha256'])
    for slug, group in groups.items():
        target = args.out / "groups" / slug
        target.mkdir(parents=True)
        tree, index, cards, info = normalize(group["tree_path"], registry[slug]["name"])
        profile = target / "profile"
        shutil.copytree(NATIVE / "profiles/template", profile)
        dump(profile / "data/knowledge_tree.json", tree)
        write_rows(profile / "data/semantic_cards.jsonl", cards)
        config = read(profile / "profile.json")
        config.update(subject_name=registry[slug]["name"], subject_scope="以所提供知识树的节点和原有边界为准，允许直接、实质的交叉关系。主要根据名称和定义判断，描述只作补充；优先具体节点，证据不足时允许父节点。")
        dump(profile / "profile.json", config)
        dump(target / "node_index.json", index)
        if manifest['require_semantic_boundaries']:
            dump(target / 'boundary_input.json', {'nodes': [
                {'node_code': node['code'], 'name_zh': node['name_zh'], 'name_en': node['name_en'],
                 'path': node['path'], 'parent_code': node['parent_code'], **node['existing_context']}
                for node in index.values()]})
        items = []
        mapping = {}
        for original in group["rows"]:
            rid = "KP_" + hashlib.sha256(json.dumps([slug, str(original["id"])], ensure_ascii=False).encode()).hexdigest()
            mapping[rid] = original
            items.append({**{f: original.get(f) or "" for f in TEXT_FIELDS}, "record_id": rid,
                          "id": original["id"], "main_tags": "", "related_tags": [],
                          "source": original.get("source", ""), "tag": registry[slug]["name"]})
        write_rows(target / "input.jsonl", items)
        dump(target / "original_records.json", mapping)
        assets = [target / "input.jsonl", target / "original_records.json", target / "node_index.json",
                  *[p for p in profile.rglob('*') if p.is_file()]]
        if manifest['require_semantic_boundaries']:
            assets.append(target / 'boundary_input.json')
        manifest["groups"][slug] = {"records": len(items), "subject": registry[slug]["name"],
                                     "taxonomy": str(group["tree_path"].resolve()), **info,
                                     "assets": {p.relative_to(args.out).as_posix(): sha(p) for p in assets}}
    manifest["snapshot_sha256"] = sha(args.out / "input.snapshot.jsonl")
    manifest["unconfigured_sha256"] = sha(args.out / "unconfigured.jsonl")
    dump(args.out / "PREPARED.json", manifest)
    return {"selected": len(selected), "groups": len(groups), "unconfigured": len(unknown), "api_calls": 0}


def frozen(out, allow_pending_boundaries=False):
    m = read(out / "PREPARED.json")
    if m.get('project_assets'):
        from project_taxonomy import validate_binding
        validate_binding(m['project_assets'])
    for info in m.get('inputs', []):
        if sha(Path(info['path'])) != info['sha256']:
            raise ValueError('Mounting source input changed')
        if info.get('cleaning_report_sha256'):
            path = Path(info['path'])
            verify_export(path)
            if sha(path.parent / 'report.json') != info['cleaning_report_sha256']:
                raise ValueError('Mounting cleaning report changed')
    if sha(out / "input.snapshot.jsonl") != m["snapshot_sha256"] or sha(out / "unconfigured.jsonl") != m["unconfigured_sha256"]:
        raise ValueError("Mounting snapshot changed")
    for group in m["groups"].values():
        if sha(Path(group['taxonomy'])) != group['source_sha256']:
            raise ValueError('Mounting taxonomy changed')
        for name, digest in group["assets"].items():
            if sha(out / name) != digest:
                raise ValueError(f"Prepared mounting asset changed: {name}")
    if m.get('require_semantic_boundaries'):
        if not m.get('boundaries_verified'):
            if not allow_pending_boundaries:
                raise ValueError('Semantic boundaries have not passed verification')
        elif sha(out / 'BOUNDARIES_VERIFIED.json') != m['boundaries_verified_sha256']:
            raise ValueError('Verified semantic boundary handoff changed')
        elif sha(out / 'BOUNDARIES_GENERATED.json') != m['boundary_generation_report_sha256']:
            raise ValueError('Semantic boundary generation report changed')
    return m


def production_manifest(out):
    manifest = frozen(out)
    if manifest.get('boundary_only'):
        raise ValueError('A boundary-only PREPARED run cannot route, review or export knowledge records')
    return manifest


@full_on_finalize
def generate_boundaries(args):
    from mounting_boundaries import generate
    return generate(args)


def verify_boundaries(args):
    from mounting_boundaries import verify
    return verify(args)


def endpoint(value):
    value = value.rstrip('/')
    for suffix in ('/v1/chat/completions', '/v1'):
        if value.endswith(suffix):
            return value[:-len(suffix)]
    return value


def api_settings(args):
    if not args.api_url or not args.model or any(v in args.api_url + args.model for v in ('MODEL_HOST', 'EXACT_MODEL_ID')):
        raise ValueError("Set the deployed API URL and exact model ID")
    key = os.environ.get(args.api_key_env, '') if args.api_key_env else ''
    if args.api_key_env and not key:
        raise ValueError(f"Missing API key environment variable: {args.api_key_env}")
    return endpoint(args.api_url), key


def orchestration_status(out, task, **values):
    path = out/task/'ORCHESTRATION_STATUS.json'
    dump(path, {'task': task, **values})
    return path


@full_on_finalize
def route(args):
    m = production_manifest(args.out)
    phase = getattr(args, 'only_round', None)
    finalize = getattr(args, 'finalize_only', False)
    staged = phase is not None or finalize
    status_path = args.out/'route'/'ORCHESTRATION_STATUS.json'
    if not staged and status_path.exists() and read(status_path).get('state') == 'finalized':
        saved = read(status_path)
        for slug in m['groups']:
            target = args.out/'groups'/slug
            validate_routes(target)
            if saved.get('routed_sha256', {}).get(slug) != sha(target/'routed.jsonl'):
                raise ValueError('Finalized route output changed')
        if not (args.out/'ROUTED.json').exists():
            raise ValueError('Finalized route status lacks ROUTED.json')
        return read(args.out/'ROUTED.json')
    if phase and phase > 1 and not status_path.exists():
        raise ValueError('Missing route round 1 orchestration status')
    if finalize and (not status_path.exists() or not (
            read(status_path).get('state') == 'ready_to_finalize' or
            read(status_path).get('resume_state') == 'ready_to_finalize')):
        raise ValueError('Route is not ready to finalize')
    base, key = api_settings(args) if m['groups'] and not finalize else ('', '')
    for slug, group in m['groups'].items():
        target = args.out/'groups'/slug
        result = target/'routed.jsonl'
        if result.exists() and not staged:
            raise FileExistsError(f"Route output exists; use a new test directory: {result}")
        cmd = [sys.executable, str(NATIVE/'scripts/hierarchical-knowledge-labeling-beam-v3.py'),
               '--input', str(target/'input.jsonl'), '--output', str(result), '--profile-dir', str(target/'profile'),
               '--api-url', base+'/v1/chat/completions', '--model', args.model, '--workers', str(args.workers),
               '--retry-failed-workers', str(args.workers), '--threshold', str(m['threshold']),
               '--stop-threshold', str(m['threshold']), '--routing-threshold', '0.6', '--root-threshold', '0.6',
               '--min-parent-depth', str(m['min_depth']), '--min-mount-depth', str(m['min_depth']),
               '--max-depth', str(m['max_depth']), '--timeout', str(args.timeout), '--max-tokens', str(args.max_tokens),
               '--max-seed-examples', '0', '--retries', '1', '--retry-failed-rounds', '1']
        if m.get('boundaries_verified'):
            cmd += ['--semantic-card-chars', '0']
        env = os.environ.copy()
        if key:
            env['KNOWLEDGE_LABELING_API_KEY'] = key
            cmd.append('--send-auth')
        if phase:
            cmd += ['--only-round', str(phase), '--retries', '0', '--retry-failed-rounds', '0', '--skip-preflight']
        elif finalize:
            cmd += ['--finalize-only', '--skip-preflight']
        dump(target/'route_command.json', cmd)
        try:
            subprocess.run(cmd, check=True, env=env)
        except Exception as exc:
            if staged:
                orchestration_status(args.out, 'route', state='technical_failure',
                                     round=phase, failed_group=slug, error=str(exc),
                                     resume_state='ready_to_finalize' if finalize else None,
                                     completed_round=read(status_path).get('completed_round') if finalize else None)
            raise
        if not phase:
            validate_routes(target)
    if phase:
        states=[read(args.out/'groups'/slug/'routed.rounds.json') for slug in m['groups']]
        pending_transport = sum(s['pending_transport'] for s in states)
        pending_format = sum(s.get('pending_format',0) for s in states)
        pending_retryable = sum(s.get('pending_retryable',s['pending_transport']) for s in states)
        retry = pending_retryable > 0 and phase < 3
        return orchestration_status(args.out, 'route', state='awaiting_retry' if retry else 'ready_to_finalize',
                                    completed_round=phase, next_round=phase+1 if retry else None,
                                    pending_transport=pending_transport,
                                    pending_format=pending_format,pending_retryable=pending_retryable,
                                    groups=len(m['groups']), records=sum(g['records'] for g in m['groups'].values()))
    report = {'groups':len(m['groups']), 'records':sum(g['records'] for g in m['groups'].values())}
    dump(args.out/'ROUTED.json', report)
    if finalize:
        previous = read(status_path)
        unresolved = sum(read(args.out/'groups'/slug/'routed.rounds.json')['pending_transport']
                         for slug in m['groups'])
        format_unresolved = sum(read(args.out/'groups'/slug/'routed.rounds.json').get('pending_format',0)
                                for slug in m['groups'])
        orchestration_status(args.out, 'route', state='finalized', completed_round=previous['completed_round'],
                             groups=report['groups'], records=report['records'],
                             transport_unresolved=unresolved,
                             format_unresolved=format_unresolved,
                             retryable_unresolved=unresolved+format_unresolved,
                             routed_sha256={slug: sha(args.out/'groups'/slug/'routed.jsonl') for slug in m['groups']})
    return report


def validate_routes(target):
    original = read(target/'original_records.json')
    output = rows(target/'routed.jsonl')
    by_id = {r.get('record_id'): r for r in output}
    if len(by_id) != len(output) or set(by_id) != set(original):
        raise ValueError('Native route output ID coverage mismatch')
    return original, by_id


def candidate(codes, score, index, config):
    if not isinstance(codes, list) or not codes or not all(isinstance(c,str) for c in codes):
        return None
    node = index.get(codes[-1])
    if not node or codes != node['chain_codes'][1:]:
        return None
    if not config['min_depth'] <= node['depth'] <= config['max_depth']:
        return None
    if isinstance(score, bool) or not isinstance(score,(int,float)) or not math.isfinite(score) or score < config['threshold'] or score>1:
        return None
    return {'codes':codes, 'path':node['path'], 'path_score':score, 'depth':node['depth'],
            'path_names':node['chain_names'], 'children_names':[index[c]['name_zh'] for c in node['children_codes']]}


def review_plan(out, config, *, build_review_items=True):
    items, decisions = [], []
    for slug in config['groups']:
        target = out/'groups'/slug
        originals, routed = validate_routes(target)
        index = read(target/'node_index.json')
        semantic_cards = ({card['node_code']: card
                           for card in rows(target/'profile/data/semantic_cards.jsonl')}
                          if config.get('boundaries_verified') else {})
        for rid, original in originals.items():
            label = routed[rid].get('knowledge_labeling') or {}
            decision = {'subject_slug':slug, 'record_id':rid, 'original':original,
                        'labeling':label, 'review_request_ids':[], 'candidates':[]}
            if label.get('status') != 'ok':
                decision.update(status='technical_failure', reason='native_mount_failure')
            elif label.get('decision') not in ('accepted_leaf','accepted_parent'):
                decision.update(status='review' if label.get('decision')=='needs_review' else 'not_mounted',
                                reason=label.get('decision') or 'no_accepted_path')
            else:
                primary = candidate(label.get('best_path_codes'), label.get('path_score'), index, config)
                if not primary:
                    decision.update(status='review',reason='path_depth_score_or_chain_not_accepted')
                else:
                    candidates = [primary]
                    for p in label.get('top_paths') or []:
                        if len(candidates) >= config['max_related'] + 1:
                            break
                        other = candidate(p.get('path_codes'),p.get('path_score'),index,config)
                        if not other or p.get('truncated') or any(other['path']==c['path'] for c in candidates):
                            continue
                        # Avoid adding ancestors/descendants as redundant related tags.
                        a,b=primary['codes'],other['codes']
                        if a[:len(b)]==b or b[:len(a)]==a:
                            continue
                        candidates.append(other)
                    for c in candidates:
                        contexts = []
                        if semantic_cards:
                            contexts = [
                                {'node_code': code, 'path': index[code]['path'],
                                 'semantic_card': semantic_cards[code]['semantic_card'],
                                 'boundary_status': ('empty_boundary_fallback' if semantic_cards[code].get('provenance') == 'empty_boundary_fallback'
                                                     else 'model_generated_unreviewed' if config.get('boundary_review_mode') == 'off'
                                                     else 'model_reviewed')}
                                for code in index[c['codes'][-1]]['chain_codes']]
                            decision.setdefault('empty_boundary_codes_in_candidates', [])
                            for context in contexts:
                                if (context['boundary_status'] == 'empty_boundary_fallback'
                                        and context['node_code'] not in decision['empty_boundary_codes_in_candidates']):
                                    decision['empty_boundary_codes_in_candidates'].append(context['node_code'])
                        if not build_review_items:
                            continue
                        request_id=hashlib.sha256(json.dumps([rid,c['codes']]).encode()).hexdigest()
                        item={f:original.get(f) or '' for f in TEXT_FIELDS}
                        source=original.get('source') or ''
                        item.update(request_id=request_id,main_tags=c['path'],
                                    source=source if isinstance(source,str) else json.dumps(source,ensure_ascii=False),
                                    taxonomy_context={'exact_path_exists':True,'path_names':c['path_names'],'children_names':c['children_names']})
                        if semantic_cards:
                            item['taxonomy_context']['node_semantic_cards'] = contexts
                        items.append(item);decision['review_request_ids'].append(request_id)
                    decision.update(status='pending_review',candidates=candidates)
            decisions.append(decision)
    return items, decisions


def reviewer_module():
    sys.path.insert(0,str(ROOT/'modules/mounting/review_lib'))
    import mount_pilot_v3
    return mount_pilot_v3


def review_wire_item(reviewer, base, model, item, position):
    """Keep long audit identity local; only a short, validated ID goes over the wire."""
    wire_id=f'R{position:07d}'
    wire_item={**item,'request_id':wire_id}
    response=reviewer.review_one(base,model,wire_item)
    if response.get('request_id')!=wire_id:
        raise ValueError('Unexpected review transport identity')
    for phase in ('first','second'):
        part=response.get(phase)
        if part and part.get('result'):
            checked=reviewer.validate_result(part['result'],wire_item)
            part['result']={**checked,'request_id':item['request_id']}
    first=(response.get('first') or {}).get('result')
    second=(response.get('second') or {}).get('result')
    response['final']=reviewer.combine(first,second)
    response.update(request_id=item['request_id'],wire_request_id=wire_id)
    return response


def staged_review_wire_item(reviewer, base, model, item, position, previous=None):
    """One attempt per necessary wire call; preserve a successful first judgment."""
    from urllib import error as urlerror
    wire_id = f'R{position:07d}'
    wire_item = {**item, 'request_id': wire_id}

    def call(counter=False):
        payload = dict(model=model, temperature=0, max_tokens=1800,
                       chat_template_kwargs={'enable_thinking': False}, messages=[
                           dict(role='system', content=reviewer.COUNTER_PROMPT if counter else reviewer.PROMPT),
                           dict(role='user', content=json.dumps(reviewer.api_view(wire_item), ensure_ascii=False))])
        try:
            raw = reviewer.request_json(base+'/v1/chat/completions', payload)
        except (urlerror.URLError, TimeoutError, ConnectionError, OSError) as exc:
            retryable = not isinstance(exc, urlerror.HTTPError) or exc.code == 429 or exc.code >= 500
            return {'result': None, 'attempts': [{'error': str(exc), 'type': type(exc).__name__}],
                    'failure_kind': 'transport' if retryable else 'permanent_transport'}
        except ValueError as exc:
            return {'result': None, 'attempts': [{'error': str(exc), 'response_received': True}],
                    'failure_kind': 'format_or_semantic'}
        try:
            choice = raw['choices'][0]
            if choice.get('finish_reason') != 'stop':
                raise ValueError('non-stop completion')
            content = choice['message']['content'].strip()
            if content.startswith('```'):
                content = content.split('\n', 1)[1].rsplit('```', 1)[0]
            checked = reviewer.validate_result(json.loads(content), wire_item)
            return {'result': {**checked, 'request_id': item['request_id']}, 'attempts': [{'response_received': True}]}
        except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
            return {'result': None, 'attempts': [{'error': str(exc), 'response_received': True}],
                    'failure_kind': 'format_or_semantic'}

    first = (previous or {}).get('first')
    if not first or first.get('result') is None:
        first = call()
    second = (previous or {}).get('second')
    if first.get('result') and first['result']['judgment'] == 'unreasonable':
        if not second or second.get('result') is None:
            second = call(counter=True)
    else:
        second = None
    final = reviewer.combine(first.get('result'), (second or {}).get('result'))
    pending_part = first if not first.get('result') else second if second and not second.get('result') else None
    return {'request_id': item['request_id'], 'wire_request_id': wire_id,
            'first': first, 'second': second, 'final': final,
            'transport_retryable': bool(pending_part and pending_part.get('failure_kind') == 'transport')}


@full_on_finalize
def staged_review(args, config, items, decisions):
    task_dir = args.out/'review'
    task_dir.mkdir(parents=True, exist_ok=True)
    state_path = task_dir/'review_rounds.json'
    events_path = task_dir/'review_rounds.jsonl'
    response_path = args.out/'review_responses.jsonl'
    phase = getattr(args, 'only_round', None)
    finalize = getattr(args, 'finalize_only', False)
    reviewer = reviewer_module()
    item_ids = [item['request_id'] for item in items]
    if len(item_ids) != len(set(item_ids)):
        raise ValueError('Duplicate review request identities')
    identity = {'prepared_sha256': sha(args.out/'PREPARED.json'),
                'routed_sha256': {slug: sha(args.out/'groups'/slug/'routed.jsonl') for slug in config['groups']},
                'items_sha256': hashlib.sha256(json.dumps(items, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                'prompt_sha256': hashlib.sha256((reviewer.PROMPT+reviewer.COUNTER_PROMPT).encode()).hexdigest(),
                'reviewer_sha256': sha(Path(reviewer.__file__)),
                'model': args.model, 'max_tokens': args.max_tokens}
    if state_path.exists():
        state = read(state_path)
        if state.get('identity') != identity:
            raise ValueError('Review source or semantic identity changed')
    elif phase == 1:
        if events_path.exists() or response_path.exists():
            raise ValueError('Review output/checkpoint exists without matching state')
        state = {'identity': identity, 'completed_rounds': [],
                 'completed_prefix_bytes': 0,
                 'completed_prefix_sha256': hashlib.sha256(b'').hexdigest()}
        dump(state_path, state)
        write_rows(args.out/'review_input.jsonl', items)
        write_rows(args.out/'route_decisions.jsonl', decisions)
    else:
        raise ValueError('Missing review round 1 checkpoint')
    if rows(args.out/'review_input.jsonl') != items or rows(args.out/'route_decisions.jsonl') != decisions:
        raise ValueError('Review source changed since round 1')
    completed = state['completed_rounds']
    if completed != list(range(1, len(completed)+1)) or len(completed) > 3:
        raise ValueError('Invalid review round history')
    raw_events = events_path.read_bytes() if events_path.exists() else b''
    prefix_bytes = state.get('completed_prefix_bytes', 0)
    if (len(raw_events) < prefix_bytes or hashlib.sha256(raw_events[:prefix_bytes]).hexdigest()
            != state.get('completed_prefix_sha256')):
        raise ValueError('Review checkpoint completed prefix checksum changed')
    events = {}
    if events_path.exists():
        for event in rows(events_path):
            core = {key: event[key] for key in ('round', 'position', 'response')}
            if event.get('sha256') != hashlib.sha256(
                    json.dumps(core, ensure_ascii=False, sort_keys=True).encode()).hexdigest():
                raise ValueError('Review checkpoint result checksum changed')
            round_number, position, response = event['round'], event['position'], event['response']
            if round_number not in (1, 2, 3) or not isinstance(position, int) or not 1 <= position <= len(items):
                raise ValueError('Invalid review checkpoint position')
            key = (round_number, position)
            if key in events or response.get('request_id') != item_ids[position-1]:
                raise ValueError('Review checkpoint identity changed')
            events[key] = response

    def latest(through):
        selected = {}
        for round_number in range(1, through+1):
            for position in range(1, len(items)+1):
                if (round_number, position) in events:
                    selected[position] = events[(round_number, position)]
        return selected

    if finalize:
        if not completed or len(latest(completed[-1])) != len(items):
            raise ValueError('Review rounds incomplete; cannot finalize')
        selected = latest(completed[-1])
        if completed[-1] < 3 and any(response.get('transport_retryable') for response in selected.values()):
            raise ValueError('Review still has transport failures for a later round')
        write_rows(response_path, [selected[position] for position in range(1, len(items)+1)])
        report = {'records': len(decisions), 'path_reviews': len(items),
                  'reviewed_this_run': 0, 'reused': len(items), 'api_url': args.api_url,
                  'model': args.model, 'transport_policy': 'short_id_exact_evidence_staged_v1',
                  'transport_unresolved': sum(bool(response['transport_retryable']) for response in selected.values()),
                  'format_or_semantic_unresolved': sum(response['final']['judgment'] == 'technical_failure' and
                                                       not response['transport_retryable'] for response in selected.values())}
        dump(args.out/'REVIEWED.json', report)
        orchestration_status(args.out, 'review', state='finalized', completed_round=completed[-1],
                             records=len(decisions), path_reviews=len(items),
                             transport_unresolved=report['transport_unresolved'],
                             response_sha256=sha(response_path))
        return report

    if phase in completed:
        if len(latest(phase)) != len(items):
            raise ValueError('Completed review round lacks coverage')
        return read(task_dir/'ORCHESTRATION_STATUS.json')
    if completed != list(range(1, phase)):
        raise ValueError(f'Review round {phase} requires completed prior rounds')
    previous = latest(phase-1)
    positions = list(range(1, len(items)+1)) if phase == 1 else [
        position for position, response in previous.items() if response.get('transport_retryable')]
    pending = [position for position in positions if (phase, position) not in events]
    if pending:
        base, key = api_settings(args)
        def transport(url, payload=None):
            headers = {'Content-Type': 'application/json'}
            if key:
                headers['Authorization'] = 'Bearer '+key
            if payload is not None:
                payload = {**payload, 'max_tokens': args.max_tokens}
            req = request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None,
                                  headers=headers)
            with request.urlopen(req, timeout=args.timeout) as response:
                return json.load(response)
        reviewer.request_json = transport
        note = '\nrequest_id必须逐字复制。evidence.text只摘录原字段中短且连续的片段；语义不确定时仍判uncertain。'
        prompt, counter = reviewer.PROMPT, reviewer.COUNTER_PROMPT
        reviewer.PROMPT = prompt+note
        reviewer.COUNTER_PROMPT = counter+note
        try:
            with events_path.open('a', encoding='utf-8') as handle:
                for position, future in shared_completed(
                        lambda p: staged_review_wire_item(reviewer, base, args.model,
                                                         items[p-1], p, previous.get(p)),
                        pending, args.workers):
                    response = future.result()
                    event = {'round': phase, 'position': position, 'response': response}
                    event['sha256'] = hashlib.sha256(json.dumps(
                        event, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                    handle.write(json.dumps(event, ensure_ascii=False)+'\n')
                    handle.flush()
                    os.fsync(handle.fileno())
                    events[(phase, position)] = response
        finally:
            reviewer.PROMPT, reviewer.COUNTER_PROMPT = prompt, counter
    if any((phase, position) not in events for position in positions):
        raise ValueError('Review round checkpoint incomplete')
    state['completed_rounds'].append(phase)
    checkpoint_bytes = events_path.read_bytes() if events_path.exists() else b''
    state['completed_prefix_bytes'] = len(checkpoint_bytes)
    state['completed_prefix_sha256'] = hashlib.sha256(checkpoint_bytes).hexdigest()
    dump(state_path, state)
    selected = latest(phase)
    pending_transport = sum(bool(response['transport_retryable']) for response in selected.values())
    retry = pending_transport > 0 and phase < 3
    status = {'state': 'awaiting_retry' if retry else 'ready_to_finalize',
              'completed_round': phase, 'next_round': phase+1 if retry else None,
              'records': len(decisions), 'path_reviews': len(items),
              'pending_transport': pending_transport}
    orchestration_status(args.out, 'review', **status)
    return status


@full_on_finalize
def review(args):
    config=production_manifest(args.out)
    if not mounting_review_enabled(config, getattr(args, 'mount_review_mode', None)):
        return {'mounting_review_enabled': False, 'model_review_performed': False,
                'records': config['selected_records'], 'skipped': True}
    items, decisions=review_plan(args.out,config)
    status_path=args.out/'review'/'ORCHESTRATION_STATUS.json'
    phase=getattr(args,'only_round',None)
    finalize=getattr(args,'finalize_only',False)
    if not phase and not finalize and status_path.exists() and read(status_path).get('state')=='finalized':
        if rows(args.out/'review_input.jsonl')!=items:
            raise ValueError('Finalized review input changed')
        if rows(args.out/'route_decisions.jsonl')!=decisions:
            raise ValueError('Finalized review decisions changed')
        response_ids=[r.get('request_id') for r in rows(args.out/'review_responses.jsonl')]
        if response_ids!=[item['request_id'] for item in items]:
            raise ValueError('Finalized review coverage mismatch')
        state=read(args.out/'review'/'review_rounds.json')
        if state['identity']['prepared_sha256'] != sha(args.out/'PREPARED.json') or any(
            state['identity']['routed_sha256'][slug] != sha(args.out/'groups'/slug/'routed.jsonl')
            for slug in config['groups']):
            raise ValueError('Finalized review source changed')
        if read(status_path).get('response_sha256') != sha(args.out/'review_responses.jsonl'):
            raise ValueError('Finalized review responses changed')
        return read(args.out/'REVIEWED.json')
    if phase or finalize:
        try:
            return staged_review(args,config,items,decisions)
        except Exception as exc:
            orchestration_status(args.out,'review',state='technical_failure',round=phase,error=str(exc))
            raise
    response_path=args.out/'review_responses.jsonl'
    existing={}
    if response_path.exists():
        if not getattr(args,'retry_failed_reviews',False):
            raise FileExistsError('Review output exists; use a new test directory or --retry-failed-reviews')
        if rows(args.out/'review_input.jsonl')!=items:
            raise ValueError('Review input changed since previous attempt')
        prior=rows(response_path);existing={r['request_id']:r for r in prior}
        if len(prior)!=len(existing) or set(existing)!={i['request_id'] for i in items}:
            raise ValueError('Previous review coverage mismatch')
        backup=args.out/('review_before_retry_'+sha(response_path)[:16]+'.jsonl')
        if not backup.exists():shutil.copy2(response_path,backup)
    write_rows(args.out/'review_input.jsonl',items)
    write_rows(args.out/'route_decisions.jsonl',decisions)
    pending=[(position,item) for position,item in enumerate(items,1)
             if item['request_id'] not in existing or existing[item['request_id']].get('final',{}).get('judgment')=='technical_failure']
    if pending:
        base,key=api_settings(args)
        reviewer=reviewer_module()
        def transport(url,payload=None):
            headers={'Content-Type':'application/json'}
            if key: headers['Authorization']='Bearer '+key
            if payload is not None:
                payload={**payload,'max_tokens':args.max_tokens}
            req=request.Request(url,data=json.dumps(payload,ensure_ascii=False).encode() if payload is not None else None,headers=headers)
            with request.urlopen(req,timeout=args.timeout) as response:return json.load(response)
        reviewer.request_json=transport
        prompt,counter=reviewer.PROMPT,reviewer.COUNTER_PROMPT
        note='\nrequest_id必须逐字复制。evidence.text只摘录原字段中短且连续的片段，优先使用name中的完整名称；禁止省略号、拼接或改写。此要求不预设挂载合理性，语义不确定时仍判uncertain。'
        reviewer.PROMPT=prompt+note;reviewer.COUNTER_PROMPT=counter+note
        try:
            with (args.out/'review_retry_log.jsonl').open('a',encoding='utf-8') as handle, ThreadPoolExecutor(max_workers=args.workers) as pool:
                for result in pool.map(lambda pair:review_wire_item(reviewer,base,args.model,pair[1],pair[0]),pending):
                    existing[result['request_id']]=result
                    handle.write(json.dumps(result,ensure_ascii=False)+'\n');handle.flush()
        finally:
            reviewer.PROMPT=prompt;reviewer.COUNTER_PROMPT=counter
    write_rows(response_path,[existing[item['request_id']] for item in items])
    dump(args.out/'REVIEWED.json',{'records':len(decisions),'path_reviews':len(items),
                                 'reviewed_this_run':len(pending),'reused':len(items)-len(pending),
                                 'api_url':args.api_url,'model':args.model,'transport_policy':'short_id_exact_evidence_v2'})


def export(args):
    config=production_manifest(args.out)
    enabled = mounting_review_enabled(config, getattr(args, 'mount_review_mode', None))
    if not enabled:
        receipt = args.out / 'ROUTED.json'
        if not receipt.is_file():
            raise ValueError('Direct export requires ROUTED.json')
        routed = read(receipt)
        if (routed.get('groups') != len(config['groups']) or
                routed.get('records') != sum(group['records'] for group in config['groups'].values())):
            raise ValueError('ROUTED.json coverage mismatch')
        status = args.out / 'route/ORCHESTRATION_STATUS.json'
        if status.is_file() and read(status).get('state') == 'finalized':
            saved = read(status)
            for slug in config['groups']:
                if saved.get('routed_sha256', {}).get(slug) != sha(args.out/'groups'/slug/'routed.jsonl'):
                    raise ValueError('Finalized route output changed')
    items, decisions=review_plan(args.out,config,build_review_items=enabled)
    checked={}
    if enabled:
        responses=rows(args.out/'review_responses.jsonl')
        response_by={r.get('request_id'):r for r in responses}
        if len(response_by)!=len(responses) or set(response_by)!={i['request_id'] for i in items}:
            raise ValueError('Review ID coverage mismatch')
        reviewer=reviewer_module()
        for item in items:
            response=response_by[item['request_id']]
            first=(response.get('first') or {}).get('result')
            second=(response.get('second') or {}).get('result')
            first=reviewer.validate_result(first,item) if first else None
            second=reviewer.validate_result(second,item) if second else None
            checked[item['request_id']]=reviewer.combine(first,second)
    return export_records(args.out, config, decisions, checked, enabled)


def export_records(out, config, decisions, checked, review_enabled):
    buckets={name:[] for name in ('mounted','review','not_mounted','technical_failure')}
    by_subject = {slug: [] for slug in config['groups']}
    audit=[]
    for d in decisions:
        status=d['status'];original=d['original'];evidence=[]
        if status=='pending_review':
            if review_enabled:
                evidence=[checked[r] for r in d['review_request_ids']]
                primary=evidence[0]['judgment']
                status={'reasonable':'mounted','unreasonable':'not_mounted','uncertain':'review','technical_failure':'technical_failure'}[primary]
            else:
                status='mounted'
        if status=='mounted':
            related=([c['path'] for c,r in zip(d['candidates'][1:],evidence[1:]) if r['judgment']=='reasonable']
                     if review_enabled else [c['path'] for c in d['candidates'][1:]])
            record={**original,'main_tags':d['candidates'][0]['path'],'related_tags':related}
            if not record.get('tag'):record['tag']=config['groups'][d['subject_slug']]['subject']
            buckets['mounted'].append(record)
            by_subject[d['subject_slug']].append(record)
        else:
            buckets[status].append({'record':original,'status':status,'reason':d.get('reason'),
                                    'labeling':d['labeling'],'path_reviews':evidence})
        audit.append({**d,'status':status,'path_reviews':evidence})
    unknown=rows(out/'unconfigured.jsonl')
    assert sum(map(len,buckets.values()))+len(unknown)==config['selected_records']
    for name,values in buckets.items():
        write_rows(out/('mounted_standard.jsonl' if name=='mounted' else name+'.jsonl'),values)
    write_rows(out/'mount_audit.jsonl',audit)
    for slug in config['groups']:
        write_rows(out/'groups'/slug/'mounted_standard.jsonl',by_subject[slug])
    report={'selected_records':config['selected_records'],'input_records':config['input_records'],
            'counts':{**{k:len(v) for k,v in buckets.items()},'unconfigured':len(unknown)},
            'mounted_by_depth':dict(Counter(str(d['candidates'][0]['depth']) for d in audit if d['status']=='mounted')),
            'count_conserved':True,'content_policy':'only main_tags/related_tags updated; absent tag filled from registry',
            'review_policy':('native evidence-gated review; unreasonable receives independent counter-review'
                             if review_enabled else 'disabled; locally validated native route dispositions'),
            'mounting_review_enabled':review_enabled,'model_review_performed':review_enabled,
            'semantic_quality_approved':False,
            'threshold':config['threshold'],'level_range':[config['min_depth'],config['max_depth']]}
    policy=load_module(NATIVE/'scripts/hierarchical-knowledge-labeling-beam-v3.py','mounting_technical_failure_accounting')
    route_failures=[{'knowledge_labeling':d['labeling']} for d in audit
                    if d['status']=='technical_failure' and d['labeling'].get('status')=='failed']
    transport=sum(policy.staged_transport_failure(r) for r in route_failures)
    malformed=sum(policy.staged_format_failure(r) for r in route_failures)
    report['technical_failure_breakdown']={'route_transport':transport,'route_response_format':malformed,
        'route_other':len(route_failures)-transport-malformed,
        'review':len(buckets['technical_failure'])-len(route_failures)}
    if config.get('project_assets'):
        report['project_assets']=config['project_assets']
    if config.get('require_semantic_boundaries'):
        report['semantic_boundaries'] = {
            'failure_policy': config.get('boundary_failure_policy', 'strict'),
            'review_mode': config.get('boundary_review_mode', 'strict'),
            'model_reviewed': config.get('boundary_model_reviewed', config.get('boundary_review_mode', 'strict') == 'strict'),
            'empty_boundary_nodes': config.get('boundary_fallback_nodes', 0),
            'quality_degraded': config.get('boundary_quality_degraded', False),
            'mounted_with_empty_boundary_in_candidates': sum(
                d['status'] == 'mounted' and bool(d.get('empty_boundary_codes_in_candidates')) for d in audit),
            'empty_boundary_is_semantic_approval': False,
        }
    dump(out/'SUMMARY.json',report)
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('prepare','activate-project-assets','generate-boundaries','verify-boundaries','route','review','export'))
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--input',type=Path,action='append',help='Repeat to join cleaned dictionary/important records at mounting')
    p.add_argument('--require-cleaned', action='store_true', help='Require hash-bound pass-only stage04 exports')
    p.add_argument('--require-boundaries', action='store_true', help='Block routing until full-tree generated boundaries pass verification')
    p.add_argument('--boundary-only', action='store_true', help='Prepare one taxonomy subject without knowledge records')
    p.add_argument('--boundary-cache-identity-url', help='Stable HTTP(S) transport identity for boundary cache')
    p.add_argument('--boundary-context-bytes', type=int, default=50000)
    p.add_argument('--boundary-failure-policy', choices=('strict', 'empty'), default='strict')
    p.add_argument('--boundary-review-mode', choices=('strict', 'off'), default='strict',
                   help='Boundary model review policy, separate from mounting model review')
    p.add_argument('--mount-review-mode', choices=('off', 'on'), default=None,
                   help='Mounting model review: prepare defaults off; export derives PREPARED unless overridden')
    p.add_argument('--boundary-rewrite-rounds', type=int, default=2)
    p.add_argument('--boundary-reuse-root', type=Path)
    p.add_argument('--boundary-cache-dir', type=Path, help='Save and automatically reuse checksum-verified taxonomy assets')
    p.add_argument('--taxonomy',type=Path)
    from project_taxonomy import DEFAULT_LOCK
    p.add_argument('--project-assets-lock',type=Path,default=DEFAULT_LOCK,
                   help='Published project taxonomy release; all production tasks share this lock')
    p.add_argument('--asset-maintenance',action='store_true',
                   help='Explicit boundary-only maintenance; never selected by a production pipeline')
    p.add_argument('--taxonomy-dir',type=Path,default=ROOT.parent/'Books_textbooks_cleaning_pipeline/taxonomy')
    p.add_argument('--registry',type=Path,default=REGISTRY)
    p.add_argument('--subject-slug')
    p.add_argument('--threshold',type=float,default=.85)
    p.add_argument('--min-depth',type=int,default=2)
    p.add_argument('--max-depth',type=int,default=5)
    p.add_argument('--max-related',type=int,default=2)
    p.add_argument('--limit',type=int,default=0)
    p.add_argument('--api-url',default='')
    p.add_argument('--model',default='')
    p.add_argument('--api-key-env',default='')
    p.add_argument('--workers',type=int,default=16)
    p.add_argument('--timeout',type=int,default=600)
    p.add_argument('--max-tokens',type=int,default=4096)
    p.add_argument('--retry-failed-reviews',action='store_true')
    p.add_argument('--only-round',type=int,choices=(1,2,3),help='Run one durable cohort transport round')
    p.add_argument('--finalize-only',action='store_true',help='Materialize staged outputs without API requests')
    a=p.parse_args()
    if a.asset_maintenance:
        if a.action not in ('prepare','generate-boundaries','verify-boundaries') or (a.action=='prepare' and not a.boundary_only):
            p.error('--asset-maintenance is limited to boundary-only asset publication work')
        a.project_assets_lock=None
    a.out=a.out.resolve()
    if not 0<a.threshold<=1 or not 1<=a.min_depth<=a.max_depth or a.max_related<0 or a.limit<0 or min(a.workers,a.timeout,a.max_tokens,a.boundary_context_bytes)<1 or not 0<=a.boundary_rewrite_rounds<=5:
        p.error('Invalid depth, score, worker or budget setting')
    if a.boundary_only and a.action != 'prepare':
        p.error('--boundary-only is valid only with prepare')
    if a.only_round and a.finalize_only:
        p.error('--only-round and --finalize-only are mutually exclusive')
    if (a.only_round or a.finalize_only) and a.action not in ('activate-project-assets','generate-boundaries','route','review'):
        p.error('Staged rounds apply only to generate-boundaries, route and review')
    if a.boundary_only and (a.input or a.require_cleaned or not a.require_boundaries or not a.subject_slug):
        p.error('--boundary-only needs --require-boundaries and --subject-slug, without --input or --require-cleaned')
    try:
        boundary_cache_identity_url(a.boundary_cache_identity_url)
    except ValueError as exc:
        p.error(str(exc))
    if a.action=='prepare':
        if not a.input and not a.boundary_only:p.error('--input required for prepare')
        result=prepare(a)
    else:
        prepared=read(a.out/'PREPARED.json')
        if not a.asset_maintenance and not prepared.get('project_assets'):
            p.error('Existing run has no project taxonomy pin; preserve it and use its frozen code or a new run')
        if ('--project-assets-lock' in sys.argv and prepared.get('project_assets') and
                a.project_assets_lock.resolve()!=Path(prepared['project_assets']['lock_path'])):
            p.error('Project taxonomy version cannot be switched inside an existing run')
        import fcntl
        with (a.out/'mounting.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            from project_taxonomy import activate
            result={'activate-project-assets':activate,'generate-boundaries':generate_boundaries,'verify-boundaries':verify_boundaries,
                    'route':route,'review':review,'export':export}[a.action](a)
    if isinstance(result, Path):
        result = read(result)
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    main()

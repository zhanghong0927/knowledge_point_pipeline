#!/usr/bin/env python3
"""First mounting -> evidence-gated review -> standard export; prepare is offline."""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib import request

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
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def prepare(args):
    if args.out.exists():
        raise FileExistsError(f"Use a new mounting directory: {args.out}")
    registry = read(args.registry)
    by_alias = aliases(registry)
    target_slug = by_alias.get(args.subject_slug) if args.subject_slug else None
    if args.subject_slug and not target_slug:
        raise ValueError("Add the subject and its taxonomy file to the registry first")
    if args.taxonomy and not args.subject_slug:
        raise ValueError("An explicit --taxonomy needs --subject-slug")
    inputs = args.input if isinstance(args.input, list) else [args.input]
    if not inputs or any(not isinstance(p, Path) for p in inputs):
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
    for row in selected:
        slug = by_alias.get(row.get('tag') or args.subject_slug)
        explicit_tree = args.taxonomy if args.taxonomy and slug == target_slug else None
        if not slug or (not registry[slug].get("file") and not explicit_tree):
            unknown.append({"record": row, "status": "unconfigured", "reason": "subject_taxonomy_not_configured"})
            continue
        tree_path = explicit_tree or args.taxonomy_dir / registry[slug]["file"]
        if not tree_path.is_file():
            unknown.append({"record": row, "status": "unconfigured", "reason": "taxonomy_file_missing", "path": str(tree_path)})
            continue
        groups.setdefault(slug, {"rows": [], "tree_path": tree_path})["rows"].append(row)
    if any(sha(Path(info['path'])) != info['sha256'] for info in input_info):
        raise ValueError('Mounting input changed during preparation')
    args.out.mkdir(parents=True)
    write_rows(args.out / "input.snapshot.jsonl", selected)
    write_rows(args.out / "unconfigured.jsonl", unknown)
    manifest = {"inputs": input_info, "input_records": len(source),
                "selected_records": len(selected), "limit": args.limit, "unconfigured": len(unknown),
                "threshold": args.threshold, "min_depth": args.min_depth, "max_depth": args.max_depth,
                "max_related": args.max_related, "groups": {}}
    manifest['require_semantic_boundaries'] = bool(getattr(args, 'require_boundaries', False))
    manifest['boundary_failure_policy'] = getattr(args, 'boundary_failure_policy', 'strict')
    manifest['boundary_review_mode'] = getattr(args, 'boundary_review_mode', 'strict')
    cache_dir = getattr(args, 'boundary_cache_dir', None)
    manifest['boundary_cache_dir'] = str(cache_dir.resolve()) if cache_dir else None
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
                                     "assets": {str(p.relative_to(args.out)): sha(p) for p in assets}}
    manifest["snapshot_sha256"] = sha(args.out / "input.snapshot.jsonl")
    manifest["unconfigured_sha256"] = sha(args.out / "unconfigured.jsonl")
    dump(args.out / "PREPARED.json", manifest)
    return {"selected": len(selected), "groups": len(groups), "unconfigured": len(unknown), "api_calls": 0}


def frozen(out, allow_pending_boundaries=False):
    m = read(out / "PREPARED.json")
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


def route(args):
    m = frozen(args.out)
    base, key = api_settings(args) if m['groups'] else ('', '')
    for slug, group in m['groups'].items():
        target = args.out/'groups'/slug
        result = target/'routed.jsonl'
        if result.exists():
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
        dump(target/'route_command.json', cmd)
        subprocess.run(cmd, check=True, env=env)
        validate_routes(target)
    dump(args.out/'ROUTED.json', {'groups':len(m['groups']), 'records':sum(g['records'] for g in m['groups'].values())})


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


def review_plan(out, config):
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
                        request_id=hashlib.sha256(json.dumps([rid,c['codes']]).encode()).hexdigest()
                        item={f:original.get(f) or '' for f in TEXT_FIELDS}
                        source=original.get('source') or ''
                        item.update(request_id=request_id,main_tags=c['path'],
                                    source=source if isinstance(source,str) else json.dumps(source,ensure_ascii=False),
                                    taxonomy_context={'exact_path_exists':True,'path_names':c['path_names'],'children_names':c['children_names']})
                        if semantic_cards:
                            item['taxonomy_context']['node_semantic_cards'] = [
                                {'node_code': code, 'path': index[code]['path'],
                                 'semantic_card': semantic_cards[code]['semantic_card'],
                                 'boundary_status': ('empty_boundary_fallback' if semantic_cards[code].get('provenance') == 'empty_boundary_fallback'
                                                     else 'model_generated_unreviewed' if config.get('boundary_review_mode') == 'off'
                                                     else 'model_reviewed')}
                                for code in index[c['codes'][-1]]['chain_codes']]
                            decision.setdefault('empty_boundary_codes_in_candidates', [])
                            for context in item['taxonomy_context']['node_semantic_cards']:
                                if (context['boundary_status'] == 'empty_boundary_fallback'
                                        and context['node_code'] not in decision['empty_boundary_codes_in_candidates']):
                                    decision['empty_boundary_codes_in_candidates'].append(context['node_code'])
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


def review(args):
    config=frozen(args.out)
    items, decisions=review_plan(args.out,config)
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
    config=frozen(args.out)
    items, decisions=review_plan(args.out,config)
    responses=rows(args.out/'review_responses.jsonl')
    response_by={r.get('request_id'):r for r in responses}
    if len(response_by)!=len(responses) or set(response_by)!={i['request_id'] for i in items}:
        raise ValueError('Review ID coverage mismatch')
    reviewer=reviewer_module()
    checked={}
    for item in items:
        response=response_by[item['request_id']]
        first=(response.get('first') or {}).get('result')
        second=(response.get('second') or {}).get('result')
        first=reviewer.validate_result(first,item) if first else None
        second=reviewer.validate_result(second,item) if second else None
        checked[item['request_id']]=reviewer.combine(first,second)
    buckets={name:[] for name in ('mounted','review','not_mounted','technical_failure')}
    audit=[]
    for d in decisions:
        status=d['status'];original=d['original'];evidence=[]
        if status=='pending_review':
            evidence=[checked[r] for r in d['review_request_ids']]
            primary=evidence[0]['judgment']
            status={'reasonable':'mounted','unreasonable':'not_mounted','uncertain':'review','technical_failure':'technical_failure'}[primary]
        if status=='mounted':
            related=[c['path'] for c,r in zip(d['candidates'][1:],evidence[1:]) if r['judgment']=='reasonable']
            record={**original,'main_tags':d['candidates'][0]['path'],'related_tags':related}
            if not record.get('tag'):record['tag']=config['groups'][d['subject_slug']]['subject']
            buckets['mounted'].append(record)
        else:
            buckets[status].append({'record':original,'status':status,'reason':d.get('reason'),
                                    'labeling':d['labeling'],'path_reviews':evidence})
        audit.append({**d,'status':status,'path_reviews':evidence})
    unknown=rows(args.out/'unconfigured.jsonl')
    assert sum(map(len,buckets.values()))+len(unknown)==config['selected_records']
    for name,values in buckets.items():
        write_rows(args.out/('mounted_standard.jsonl' if name=='mounted' else name+'.jsonl'),values)
    write_rows(args.out/'mount_audit.jsonl',audit)
    for slug in config['groups']:
        accepted_ids={str(d['original']['id']) for d in audit if d['subject_slug']==slug and d['status']=='mounted'}
        # Same IDs may exist in different subjects in standalone multi-subject mode.
        original=read(args.out/'groups'/slug/'original_records.json')
        original_by_id={str(v['id']):v for v in original.values()}
        group_records=[]
        for d in audit:
            if d['subject_slug']==slug and str(d['original']['id']) in accepted_ids and d['status']=='mounted':
                source=original_by_id[str(d['original']['id'])]
                group_records.append({**source,'tag':source.get('tag') or config['groups'][slug]['subject'],
                                      'main_tags':d['candidates'][0]['path'],
                                      'related_tags':[c['path'] for c,r in zip(d['candidates'][1:],d['path_reviews'][1:]) if r['judgment']=='reasonable']})
        write_rows(args.out/'groups'/slug/'mounted_standard.jsonl',group_records)
    report={'selected_records':config['selected_records'],'input_records':config['input_records'],
            'counts':{**{k:len(v) for k,v in buckets.items()},'unconfigured':len(unknown)},
            'mounted_by_depth':dict(Counter(str(d['candidates'][0]['depth']) for d in audit if d['status']=='mounted')),
            'count_conserved':True,'content_policy':'only main_tags/related_tags updated; absent tag filled from registry',
            'review_policy':'native evidence-gated review; unreasonable receives independent counter-review',
            'threshold':config['threshold'],'level_range':[config['min_depth'],config['max_depth']]}
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
    dump(args.out/'SUMMARY.json',report)
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('prepare','generate-boundaries','verify-boundaries','route','review','export'))
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--input',type=Path,action='append',help='Repeat to join cleaned dictionary/important records at mounting')
    p.add_argument('--require-cleaned', action='store_true', help='Require hash-bound pass-only stage04 exports')
    p.add_argument('--require-boundaries', action='store_true', help='Block routing until full-tree generated boundaries pass verification')
    p.add_argument('--boundary-context-bytes', type=int, default=50000)
    p.add_argument('--boundary-failure-policy', choices=('strict', 'empty'), default='strict')
    p.add_argument('--boundary-review-mode', choices=('strict', 'off'), default='strict',
                   help='Off skips boundary model review only; identity checks and final path review remain')
    p.add_argument('--boundary-rewrite-rounds', type=int, default=2)
    p.add_argument('--boundary-reuse-root', type=Path)
    p.add_argument('--boundary-cache-dir', type=Path, help='Save and automatically reuse checksum-verified taxonomy assets')
    p.add_argument('--taxonomy',type=Path)
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
    a=p.parse_args()
    a.out=a.out.resolve()
    if not 0<a.threshold<=1 or not 1<=a.min_depth<=a.max_depth or a.max_related<0 or a.limit<0 or min(a.workers,a.timeout,a.max_tokens,a.boundary_context_bytes)<1 or not 0<=a.boundary_rewrite_rounds<=5:
        p.error('Invalid depth, score, worker or budget setting')
    if a.action=='prepare':
        if not a.input:p.error('--input required for prepare')
        result=prepare(a)
    else:
        import fcntl
        with (a.out/'mounting.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            result={'generate-boundaries':generate_boundaries,'verify-boundaries':verify_boundaries,
                    'route':route,'review':review,'export':export}[a.action](a)
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    main()

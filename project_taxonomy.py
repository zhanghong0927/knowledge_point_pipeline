"""Read a published project taxonomy release; production never generates assets."""
import json
from pathlib import Path, PurePosixPath
import shutil

from round_integrity import digest_file

ROOT=Path(__file__).resolve().parent
DEFAULT_LOCK=ROOT/'project_assets/LOCK.json'


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _inside(base,name):
    relative=PurePosixPath(name)
    if not name or '\\' in name or relative.is_absolute() or '..' in relative.parts or ':' in name:
        raise ValueError('Invalid project asset path')
    path=base/Path(*relative.parts)
    if not path.resolve().is_relative_to(base.resolve()):
        raise ValueError('Project asset escaped release directory')
    for candidate in (path,*path.parents):
        if candidate==base.parent:break
        if candidate.is_symlink():raise ValueError('Project asset symlink is forbidden')
    return path


def subject(slug,lock_path=None,full=True):
    lock_path=Path(lock_path or DEFAULT_LOCK).resolve()
    if not lock_path.is_file():
        raise ValueError('Project taxonomy LOCK.json is missing; publish project assets before running')
    lock=_read(lock_path)
    if lock.get('schema')!=1 or not isinstance(lock.get('project_version'),str) or not lock['project_version']:
        raise ValueError('Invalid project taxonomy release')
    entry=lock.get('subjects',{}).get(slug)
    if not entry:
        raise ValueError('Project taxonomy subject is not published: '+slug)
    asset=_inside(lock_path.parent,entry['asset'])
    manifest_path=asset/'MANIFEST.json'
    if not manifest_path.is_file() or digest_file(manifest_path)!=entry['manifest_sha256']:
        raise ValueError('Project asset manifest checksum changed: '+slug)
    manifest=_read(manifest_path);identity=manifest.get('identity',{})
    import hashlib
    expected_key=hashlib.sha256(json.dumps(identity,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    if identity.get('subject_slug')!=slug or manifest.get('asset_key')!=expected_key:
        raise ValueError('Project asset identity changed: '+slug)
    assets=manifest.get('assets',{})
    selected='boundaries/runtime_cards.jsonl' if identity.get('failure_policy')=='empty' or identity.get('review_mode')=='off' else 'boundaries/cross_validated_cards.jsonl'
    required={'taxonomy_original.json','boundary_input.json','taxonomy_enriched.json',selected,'boundaries/summary.json'}
    if not required<=set(assets):raise ValueError('Incomplete project taxonomy asset: '+slug)
    if (assets['taxonomy_original.json']!=identity.get('source_sha256') or
            assets['boundary_input.json']!=identity.get('boundary_input_sha256')):
        raise ValueError('Project taxonomy source identity changed: '+slug)
    if full:
        paths=list(asset.rglob('*'))
        if any(p.is_symlink() for p in paths):raise ValueError('Project asset symlink is forbidden')
        actual={p.relative_to(asset).as_posix() for p in paths if p.is_file() and p!=manifest_path}
        if actual!=set(assets):raise ValueError('Project asset file coverage changed: '+slug)
        for name,expected in assets.items():
            path=_inside(asset,name)
            if not path.is_file() or digest_file(path)!=expected:
                raise ValueError('Project asset checksum changed: '+slug+'/'+name)
    return {'version':lock['project_version'],'lock_path':str(lock_path),'lock_sha256':digest_file(lock_path),
            'subject':slug,'asset_path':str(asset),'manifest_sha256':entry['manifest_sha256'],
            'taxonomy':str(asset/'taxonomy_original.json'),'source_sha256':identity['source_sha256'],
            'boundary_input_sha256':identity['boundary_input_sha256'],'cards_sha256':assets[selected],
            'enriched_sha256':assets['taxonomy_enriched.json'],'review_mode':identity['review_mode'],
            'failure_policy':identity['failure_policy']}


def binding(slugs,lock_path=None,full=True):
    values={slug:subject(slug,lock_path,full=full) for slug in sorted(set(slugs))}
    if not values:raise ValueError('Project asset binding requires a subject')
    first=next(iter(values.values()))
    if any((v['version'],v['lock_sha256'])!=(first['version'],first['lock_sha256']) for v in values.values()):
        raise ValueError('Project taxonomy release changed during preparation')
    return {key:first[key] for key in ('version','lock_path','lock_sha256')}|{'subjects':values}


def validate_binding(saved):
    current=binding(saved['subjects'],saved['lock_path'])
    if saved!=current:raise ValueError('Project taxonomy lock or pinned version changed; use a new run directory')
    return current


def activate(args):
    """Import already published evidence. No API, generation, or mutable cache."""
    import sys
    sys.path.insert(0,str(ROOT/'adapters'))
    import mounting_bridge as bridge
    import mounting_tree_cache as cache
    manifest=bridge.frozen(args.out,allow_pending_boundaries=True)
    saved=manifest.get('project_assets')
    if not saved:raise ValueError('Mounting run has no pinned project taxonomy; use its original frozen code or a new run')
    validate_binding(saved)
    report_path=args.out/'BOUNDARIES_GENERATED.json'
    if report_path.exists():
        report=bridge.read(report_path)
        if report.get('project_assets')!=saved:raise ValueError('Project asset activation identity changed')
        for group in report['groups'].values():
            for name,expected in group['assets'].items():
                if bridge.sha(args.out/name)!=expected:raise ValueError('Activated project asset changed: '+name)
    else:
        # Validate the complete source set before writing any per-run copy.
        for slug,pin in saved['subjects'].items():
            target=args.out/'groups'/slug
            if (bridge.sha(target/'boundary_input.json')!=pin['boundary_input_sha256'] or
                    manifest['groups'][slug]['source_sha256']!=pin['source_sha256']):
                raise ValueError('Prepared taxonomy differs from project taxonomy: '+slug)
        report={'groups':{},'expert_approved':False,'model_calls':0,'project_assets':saved}
        for slug,pin in saved['subjects'].items():
            asset=Path(pin['asset_path']);source=asset/'boundaries';target=args.out/'groups'/slug/'boundaries'
            if target.exists():raise ValueError('Boundary destination already exists without activation receipt; preserve it and use a new run')
            shutil.copytree(source,target)
            report['groups'][slug]={'summary':bridge.read(target/'summary.json'),'cache_reused':True,
                'cache_source':str(asset),'project_asset':pin,
                'assets':{p.relative_to(args.out).as_posix():bridge.sha(p) for p in cache.approved_evidence(target)}}
        bridge.dump(report_path,report)
    phase=getattr(args,'only_round',None)
    status={'state':'ready_to_finalize' if phase else 'finalized','completed_rounds':[1],
            'next_round':None,'transport_unresolved':0,'model_calls':0,'project_assets':saved,
            'semantic_quality_degraded':any(bridge.read(Path(v['asset_path'])/'boundaries/summary.json').get('semantic_quality_degraded',False) for v in saved['subjects'].values())}
    bridge.dump(args.out/'boundary/ORCHESTRATION_STATUS.json',status)
    return report


def pin_run(run,slug):
    """Freeze release identity before any new run schedules a model request."""
    saved=binding([slug])
    receipt=Path(run)/'PROJECT_ASSETS.json'
    if receipt.exists():
        if _read(receipt)!=saved:raise ValueError('Project taxonomy version changed for existing run')
    else:
        if (Path(run)/'pipeline_state.json').exists():
            raise ValueError('Existing legacy run has no project asset pin; preserve it and use a new run')
        receipt.parent.mkdir(parents=True,exist_ok=True)
        receipt.write_text(json.dumps(saved,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return saved

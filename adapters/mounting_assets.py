"""Local taxonomy normalization for the native mounting runtime."""

import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def electronic_tree(rows, subject):
    # The supplied file encodes levels explicitly; '/' inside a level name is not a separator.
    root = {"name": subject, "children": []}
    lookup = {}
    for row in rows:
        chain = []
        parent = root
        for key in ("l1", "l2", "l3"):
            name = row.get(key)
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"Missing {key} in electronic taxonomy")
            chain.append(name)
            identity = tuple(chain)
            if identity not in lookup:
                node = {"name": name, "children": []}
                parent["children"].append(node)
                lookup[identity] = node
            parent = lookup[identity]
        for child in row.get("l4_nodes", []):
            name = child.get("l4_name")
            if not isinstance(name, str) or not name.strip():
                raise ValueError("Missing electronic l4_name")
            identity = (*chain, name)
            if identity not in lookup:
                node = {"name": name, "children": []}
                parent["children"].append(node)
                lookup[identity] = node
    return root


def normalize(path, subject):
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    synthetic = isinstance(data, list) and bool(data) and all(isinstance(n, dict) and "l1" in n for n in data)
    if synthetic:
        data = electronic_tree(data, subject)
    generator = load_module(ROOT / "modules/mounting/pipeline/generate_semantic_boundaries.py", "mount_asset_normalizer")
    nodes = generator.normalize(data)
    built, index, paths = {}, {}, set()
    for n in nodes:
        parent = index.get(n["parent_code"])
        codes = ([] if parent is None else parent["chain_codes"]) + [n["code"]]
        names = ([] if parent is None else parent["chain_names"]) + [n["name_zh"]]
        full = "/".join(names)
        if full in paths:
            raise ValueError(f"Duplicate canonical taxonomy path: {full}")
        paths.add(full)
        node = {"code": n["code"], "name_zh": n["name_zh"], "name_en": n["name_en"],
                "path": full, "children": []}
        built[n["code"]] = node
        if parent:
            built[n["parent_code"]]["children"].append(node)
        index[n["code"]] = {**n, "path": full, "source_path": n["path"],
                              "chain_codes": codes, "chain_names": names}
    roots = [n for n in nodes if n["parent_code"] is None]
    if len(roots) != 1:
        raise ValueError("Taxonomy needs exactly one routing root")
    cards = []
    for node in index.values():
        context = {"path": node["path"], "existing_definition_and_boundary": node["existing_context"],
                   "children": [index[c]["name_zh"] for c in node["children_codes"]]}
        cards.append({"node_code": node["code"], "semantic_card": json.dumps(context, ensure_ascii=False), "seed_examples": []})
    report = {"nodes": len(nodes), "max_depth": max(n["depth"] for n in nodes),
              "synthetic_subject_routing_root": synthetic,
              "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
              "semantic_cards": "existing taxonomy fields only; no model-generated definitions",
              "path_policy": "join exact ancestor names including routing root; original paths retained in index"}
    return built[roots[0]["code"]], index, cards, report

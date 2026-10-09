"""Extract conservative leaf-path aliases from delivered taxonomy formats."""


def normalize_path(value):
    if isinstance(value, list):
        parts = value
    else:
        parts = str(value or '').replace(' > ', '/').split('/')
    return '/'.join(str(part).strip() for part in parts if str(part).strip())


def leaf_paths(data):
    leaves = set()

    def add(path, root=''):
        path = normalize_path(path)
        root = normalize_path(root)
        if path:
            leaves.add(path)
            if root and path.startswith(root + '/'):
                leaves.add(path[len(root) + 1:])
            elif root:
                leaves.add(root + '/' + path)

    if isinstance(data, dict) and isinstance(data.get('nodes'), list):
        for node in data['nodes']:
            if node.get('is_leaf'):
                add(node.get('path_names') or node.get('full_path'), root='管理学')
        return leaves

    if isinstance(data, dict) and isinstance(data.get('hierarchy'), list):
        for l1 in data['hierarchy']:
            p1 = [l1['name']]
            for l2 in l1.get('l2_nodes', []):
                p2 = p1 + [l2['name']]
                for l3 in l2.get('l3_nodes', []):
                    p3 = p2 + [l3['name']]
                    l4s = [v for v in l3.get('l4_nodes', [])
                           if isinstance(v, str) and v.strip() and v.strip() != '—'
                           and not v.strip().startswith('（')]
                    if l4s:
                        for l4 in l4s:
                            add(p3 + [l4])
                    else:
                        add(p3)
        return leaves

    if isinstance(data, dict):
        root_name = data.get('name') or data.get('name_zh') or ''

        def walk(node, names):
            children = node.get('children') or []
            name = node.get('name') or node.get('name_zh') or ''
            current = names + ([name] if name else [])
            if children:
                for child in children:
                    walk(child, current)
            elif names:
                add(node.get('path') or current, root_name)

        walk(data, [])
    return leaves

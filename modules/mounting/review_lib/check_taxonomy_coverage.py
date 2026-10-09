"""Tree-based ancestry: never infer hierarchy by splitting path strings."""
from collections import defaultdict
class Taxonomy:
    def __init__(self,data,core_paths=None):
        self.nodes={}; self.sets={m:defaultdict(set) for m in ('main','combined')}
        self.core_paths=core_paths
        if isinstance(data,dict) and 'root' in data: data=data['root']
        if isinstance(data,dict) and 'nodes' in data:
            flat=data['nodes']
            if not isinstance(flat,list): raise ValueError('nodes must be array')
            by_code={n['node_code']:dict(n,children=[]) for n in flat}
            if len(by_code)!=len(flat): raise ValueError('duplicate node_code')
            roots=[]
            for code,n in by_code.items():
                names=n.get('path_names')
                n['path']=n.get('path', '/'.join(names) if isinstance(names,list) and all(isinstance(x,str) for x in names) else n.get('full_path'))
                parent=n.get('parent_code')
                if parent is None: roots.append(n)
                elif parent not in by_code: raise ValueError('unknown parent_code')
                else: by_code[parent]['children'].append(n)
                seen={code}; ancestor=parent
                while ancestor is not None:
                    if ancestor in seen: raise ValueError('taxonomy cycle')
                    seen.add(ancestor)
                    if ancestor not in by_code: raise ValueError('unknown parent_code')
                    ancestor=by_code[ancestor].get('parent_code')
            data=roots
        if isinstance(data,dict) and 'hierarchy' in data:
            def convert(n,level=1):
                if isinstance(n,str): return {'name':n,'depth':level,'children':[]}
                return {'name':n['name'],'depth':level,'children':[convert(x,level+1) for x in n.get('l'+str(level+1)+'_nodes',[])]}
            data={'path':'','depth':0,'children':[convert(n) for n in data['hierarchy']]}
        def walk(n,parent,level):
            if not isinstance(n,dict): raise ValueError('taxonomy node must be object')
            children=n.get('children',[])
            if not isinstance(children,list): raise ValueError('children must be array')
            raw_level=n.get('depth',n.get('level',level))
            depth=int(str(raw_level).removeprefix('L'))
            path=n.get('path')
            if path is None:
                name=n.get('name',n.get('name_zh'))
                if not isinstance(name,str): raise ValueError('node without path/name')
                path='' if depth==0 else '/'.join(filter(None,[parent,name]))
            if not isinstance(path,str): raise ValueError('path not string')
            if path in self.nodes: raise ValueError('duplicate taxonomy path: '+path)
            self.nodes[path]=dict(depth=depth,parent=parent,leaf=not children)
            for child in children: walk(child,path,depth+1)
        if isinstance(data,list):
            for n in data: walk(n,None,1)
        elif isinstance(data,dict) and ('children' in data or 'path' in data): walk(data,None,0)
        else: raise ValueError('unsupported taxonomy structure')
        if core_paths is not None:
            if not isinstance(core_paths,list) or not all(isinstance(p,str) for p in core_paths): raise ValueError('core_paths must be an array of strings')
            if len(core_paths)!=len(set(core_paths)): raise ValueError('core_paths contains duplicates')
            if any(p not in self.nodes or self.nodes[p]['depth']!=3 for p in core_paths): raise ValueError('core list contains non-L3/unknown paths')
    def branch(self,path,depth):
        while path in self.nodes:
            if self.nodes[path]['depth']==depth: return path
            path=self.nodes[path]['parent']
        return None
    def add(self,r,row):
        out=[]; main=r.get('main_tags'); related=r.get('related_tags',[])
        valid_main=isinstance(main,str) and main in self.nodes and self.nodes[main]['depth']>0
        if isinstance(main,str) and not valid_main: out.append(('error','unknown_main_path'))
        others=[]
        if isinstance(related,list):
            for p in related:
                if isinstance(p,str) and p in self.nodes and self.nodes[p]['depth']>0: others.append(p)
                else: out.append(('error','unknown_related_path'))
        ident=r.get('id'); key=('id',ident) if isinstance(ident,str) and ident.strip() else ('row',row)
        for mode,paths in [('main',[main] if valid_main else []),('combined',([main] if valid_main else [])+others)]:
            for p in set(paths):
                self.sets[mode][p].add(key)
                parent=self.nodes[p]['parent']
                while parent in self.nodes:
                    if self.nodes[parent]['depth']==3: self.sets[mode][parent].add(key)
                    parent=self.nodes[parent]['parent']
        return out
    def count(self,path,mode): return len(self.sets[mode].get(path,()))
    def report(self):
        out={}
        for mode in self.sets:
            out[mode]={}
            groups={'leaf':[p for p,n in self.nodes.items() if n['leaf'] and n['depth']>0], 'l3':[p for p,n in self.nodes.items() if n['depth']==3], 'core_l3':self.core_paths}
            for group,paths in groups.items():
                for minimum,label,threshold in [(1,'gt0',1.0 if group=='core_l3' else .9),(10,'ge10',.9 if group=='core_l3' else .8)]:
                    denominator=len(paths) if paths is not None else 0
                    numerator=sum(self.count(p,mode)>=minimum for p in paths) if paths else 0
                    rate=numerator/denominator if denominator else None
                    passed=None if rate is None else (rate==1 if group=='core_l3' and minimum==1 else rate>threshold)
                    out[mode][group+'_'+label]=dict(numerator=numerator,denominator=denominator,rate=rate,passed=passed)
        return out

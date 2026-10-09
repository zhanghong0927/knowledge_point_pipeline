"""Exact nonempty names only. No synonym or destructive normalization."""
from collections import defaultdict
class Duplicates:
    def __init__(self,scope='node',taxonomy=None):
        self.scope=scope; self.taxonomy=taxonomy; self.index=defaultdict(list); self.unresolved=0
    def add(self,r,row):
        p=r.get('main_tags')
        if not isinstance(p,str) or not p.strip(): return
        branch=p
        if self.scope!='node':
            branch=self.taxonomy.branch(p,int(self.scope)) if self.taxonomy else None
            if branch is None: self.unresolved+=1; return
        for f in ('name','knowledge_point'):
            n=r.get(f)
            if isinstance(n,str) and n.strip(): self.index[(branch,f,n)].append(row)
    def groups(self):
        for (branch,field,name),rows in self.index.items():
            if len(rows)>1: yield dict(branch=branch,field=field,name=name,rows=rows)

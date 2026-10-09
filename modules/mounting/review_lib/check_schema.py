"""Subject-independent delivery schema checks; never changes records."""
class Schema:
    required = ('id','name','knowledge_point','main_tags','related_tags','source')
    optional = ('definition','en_definition','description','en_description')
    def __init__(self):
        self.ids=set()
    def check(self,r):
        if not isinstance(r,dict): return [('error','record_not_object')]
        out=[]
        for k in self.required+self.optional:
            if k not in r:
                if k in self.required: out.append(('error','missing_'+k))
                continue
            v=r[k]
            valid=isinstance(v,list) and all(isinstance(x,str) for x in v) if k=='related_tags' else isinstance(v,str)
            if not valid: out.append(('error','type_'+k)); continue
            if k in self.required and k!='related_tags' and not v.strip():
                out.append(('observation' if k in ('name','knowledge_point') else 'error','empty_'+k))
        ident=r.get('id')
        if isinstance(ident,str):
            if ident in self.ids: out.append(('error','duplicate_id'))
            self.ids.add(ident)
        return out

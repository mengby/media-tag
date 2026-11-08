"""Finite optional CSV keywords from already-confirmed associations and published equivalence."""
import json
import re
from media_tag.catalog import NotFound,RevisionConflict
from media_tag.contracts.models import EffectiveValue
from media_tag.review.policy import MEDICAL
from media_tag.terminology.contracts import normalize
from media_tag.terminology.lexicon.service import fingerprint


class AliasExport:
    def __init__(self,search):self.search=search

    def binding(self,c):
        head=self.search.resolver.indices.head(c);published=self.search.terminology._head(c)
        taxid=head['taxonomy_release_id'] or published['taxonomy_release_id'];lexid=head['lexicon_release_id'] or published['lexicon_release_id']
        if not lexid or not taxid:raise ValueError('published alias vocabulary unavailable')
        _,lexicon,_=self.search.resolver.indices.pair(c,taxid,lexid)
        return {'taxonomy_release_id':taxid,'lexicon_release_id':lexid,'lexicon_sha256':fingerprint(lexicon.model_dump(mode='json'))}

    def append(self,c,asset,item,issues,binding):
        taxonomy,lexicon,_=self.search.resolver.indices.pair(c,binding['taxonomy_release_id'],binding['lexicon_release_id'])
        options={o.entry_id:o for o in taxonomy.options if o.status=='active'};concepts={con.entry_id:con for con in lexicon.concepts if con.status=='active'}
        values={};medical={}
        for row in c.execute('SELECT field,payload FROM effective_values WHERE asset_id=?',(asset['asset_id'],)):
            value=EffectiveValue.model_validate_json(row['payload']);values[row['field']]=value
            if row['field'] not in MEDICAL or value.value.state!='observed':continue
            decision=c.execute("SELECT payload_json FROM review_decisions WHERE asset_id=? AND field=? AND asset_revision=? AND decision!='reject'",
                (asset['asset_id'],value.field,value.revision)).fetchone()
            if decision and json.loads(decision[0]).get('effective')==value.model_dump(mode='json'):medical[value.field]=set(value.value.values)
        contexts={}
        for link in c.execute('SELECT * FROM asset_concept_links WHERE asset_id=? AND active=1',(asset['asset_id'],)):
            con=concepts.get(link['concept_id'])
            if self.search.links.current(c,link,con,asset['blob_sha256']):contexts.setdefault(con.entry_id,[]).append((link['polarity'],link['direction']))
        for con in concepts.values():
            if con.concept_type!='other' and con.option_ids and all(o in options and options[o].value in medical.get(options[o].field,set()) for o in con.option_ids):
                contexts.setdefault(con.entry_id,[]).append(('affirmed','unspecified'))
        known={field:{oid for oid,o in options.items() if o.value in medical.get(o.field,set()) and o.field==field} for field in ('科室','器官/系统')}
        body='\n'.join(v for value in values.values() if value.value.state=='observed' for v in value.value.values)
        added=[]
        for alias in lexicon.aliases:
            if alias.relation!='equivalent' or not alias.default_expand or alias.target_id not in contexts:continue
            context=alias.context
            if context.departments and not set(context.departments)&known['科室']:continue
            if context.organs and not set(context.organs)&known['器官/系统']:continue
            if any(normalize(t) not in normalize(body) for t in context.required_terms) or any(normalize(t) in normalize(body) for t in context.excluded_terms):continue
            valid=False
            for polarity,direction in contexts[alias.target_id]:
                if polarity=='negated' and context.polarity!='negated':continue
                if context.polarity!='unspecified' and context.polarity!=polarity:continue
                if direction not in ('unspecified','static') and context.direction!=direction:continue
                if context.direction!='unspecified' and context.direction!=direction:continue
                valid=True;break
            if not valid:continue
            if any(c in alias.expression for c in (';','\n','\r','|')):
                issues.append({'asset_id':asset['asset_id'],'code':'alias_not_flat_keyword','alias_id':alias.entry_id});continue
            field='中文检索词' if re.search(r'[\u3400-\u9fff]',alias.expression) else '英文检索词'
            if field in values and values[field].value.state!='observed':continue
            existing=item.get(field,'').split(';') if item.get(field) else []
            if alias.expression in existing:continue
            item[field]=';'.join([*existing,alias.expression]);added.append(alias.entry_id)
            if len(added)>=8:break
        if added:issues.append({'asset_id':asset['asset_id'],'code':'confirmed_alias_keywords_added','alias_ids':added,'lexicon_release_id':binding['lexicon_release_id'],'database_values_changed':False})

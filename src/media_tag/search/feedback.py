"""Private human feedback; approval/promotion creates a draft and never a live alias."""
import json
from uuid import uuid4
from media_tag.catalog import AccessDenied,NotFound,RevisionConflict
from media_tag.catalog.database import utc_now
from media_tag.catalog.service import _human,encode
from media_tag.terminology.contracts import Alias,Lexicon,normalize
from media_tag.terminology.lexicon.service import fingerprint


class FeedbackService:
    def __init__(self,search):self.search=search;self.terms=search.terminology

    def _row(self,c,fid,principal):
        _human(principal)
        row=c.execute('SELECT * FROM terminology_feedback WHERE feedback_id=?',(fid,)).fetchone()
        if not row:raise NotFound('feedback unavailable')
        if row['owner_id']!=principal.actor_id and principal.actor_id not in self.terms.config.managers:raise AccessDenied('feedback permission denied')
        return row

    def _payload(self,c,spec):
        binding=spec.lexicon_release_id or self.terms._head(c)['lexicon_release_id']
        if binding:
            row,lexicon=self.terms._release(c,binding)
            if row['kind']!='lexicon':raise ValueError('published lexicon required')
        reported_binding=spec.reported_lexicon_release_id or binding
        reported_lexicon=None
        if spec.reported_target_id or spec.reported_entry_id:
            if not reported_binding:raise ValueError('reported mapping needs its original published version')
            reported_row,reported_lexicon=self.terms._release(c,reported_binding)
            if reported_row['kind']!='lexicon':raise ValueError('reported mapping requires a lexicon')
        if spec.reported_target_id:
            if not any(con.entry_id==spec.reported_target_id and con.status=='active' for con in reported_lexicon.concepts):raise ValueError('reported target is not in the pinned lexicon')
        if spec.reported_entry_id:
            matches=[a for a in reported_lexicon.aliases if a.entry_id==spec.reported_entry_id and a.target_id==spec.reported_target_id]
            if not matches and not any(con.entry_id==spec.reported_entry_id==spec.reported_target_id for con in reported_lexicon.concepts):raise ValueError('reported mapping is not in the pinned lexicon')
        if spec.target_id:
            if not binding:raise ValueError('known target requires a published lexicon')
            row,lexicon=self.terms._release(c,binding)
            if row['kind']!='lexicon' or not any(con.entry_id==spec.target_id and con.status=='active' for con in lexicon.concepts):raise ValueError('target is not active')
        payload=spec.model_dump(mode='json',exclude={'request_id','expected_revision','withdraw'})|{'bound_lexicon_release_id':binding,
            'reported_lexicon_release_id':reported_binding if reported_lexicon else spec.reported_lexicon_release_id}
        digest=fingerprint({'expression':normalize(spec.expression),'target':spec.target_id,'relation':spec.relation,
                            'context':spec.context.model_dump(mode='json'),'binding':binding,'kind':spec.kind,
                            'reported_target':spec.reported_target_id,'reported_entry':spec.reported_entry_id,'reported_binding':reported_binding if reported_lexicon else None})
        return payload,digest

    def create(self,spec,*,principal):
        _human(principal);data={'action':'feedback_create',**spec.model_dump(mode='json')}
        with self.search.database.transaction(write=True) as c:
            replay=self.terms._replay(c,principal,spec.request_id,data)
            if replay:return replay
            payload,digest=self._payload(c,spec)
            old=c.execute('SELECT * FROM terminology_feedback WHERE owner_id=? AND fingerprint=?',(principal.actor_id,digest)).fetchone()
            fid=old['feedback_id'] if old else 'feedback-'+uuid4().hex
            if old:
                revision=old['revision']+1
                c.execute('UPDATE terminology_feedback SET revision=?,occurrences=occurrences+1 WHERE feedback_id=?',(revision,fid))
                state=old['state'];occurrences=old['occurrences']+1
            else:
                revision=1;state='pending';occurrences=1
                c.execute('INSERT INTO terminology_feedback VALUES(?,?,?,?,?,?,1,NULL,?)',
                    (fid,principal.actor_id,1,state,digest,encode(payload),utc_now()))
            c.execute('INSERT INTO feedback_events(feedback_id,actor_id,action,payload_json,created_at) VALUES(?,?,?,?,?)',
                      (fid,principal.actor_id,'submit',encode(payload),utc_now()))
            return self.terms._done(c,principal,spec.request_id,data,{'feedback_id':fid,'revision':revision,'state':state,'occurrences':occurrences,
                'published':False},'feedback_create')

    def get(self,fid,*,principal):
        with self.search.database.transaction() as c:
            row=self._row(c,fid,principal)
            return dict(row)|{'payload':json.loads(row['payload_json']),'events':[dict(e)|{'payload':json.loads(e['payload_json'])} for e in c.execute(
                'SELECT actor_id,action,payload_json,created_at FROM feedback_events WHERE feedback_id=? ORDER BY event_id',(fid,))]}

    def listing(self,*,principal,management=False):
        _human(principal)
        if management:self.terms._manager(principal)
        with self.search.database.transaction() as c:
            return {'items':[dict(r)|{'payload':json.loads(r['payload_json'])} for r in c.execute('''SELECT * FROM terminology_feedback
                WHERE ? OR owner_id=? ORDER BY created_at DESC LIMIT 200''',(int(management),principal.actor_id))],'limit':200}

    def revise(self,fid,spec,*,principal):
        data={'action':'feedback_revise','feedback_id':fid,**spec.model_dump(mode='json')}
        with self.search.database.transaction(write=True) as c:
            row=self._row(c,fid,principal)
            if row['owner_id']!=principal.actor_id:raise AccessDenied('only submitter can revise feedback')
            replay=self.terms._replay(c,principal,spec.request_id,data)
            if replay:return replay
            if row['revision']!=spec.expected_revision:raise RevisionConflict('feedback changed')
            if row['state'] not in ('pending','needs_info'):raise RevisionConflict('processed feedback requires a new submission')
            payload,digest=self._payload(c,spec)
            duplicate=c.execute('SELECT feedback_id FROM terminology_feedback WHERE owner_id=? AND fingerprint=? AND feedback_id!=?',(principal.actor_id,digest,fid)).fetchone()
            if duplicate:raise RevisionConflict('matching feedback already exists; retain both sources')
            state='withdrawn' if spec.withdraw else 'pending';revision=row['revision']+1
            c.execute('UPDATE terminology_feedback SET revision=?,state=?,fingerprint=?,payload_json=? WHERE feedback_id=?',(revision,state,digest,encode(payload),fid))
            c.execute('INSERT INTO feedback_events(feedback_id,actor_id,action,payload_json,created_at) VALUES(?,?,?,?,?)',
                      (fid,principal.actor_id,'withdraw' if spec.withdraw else 'revise',encode(payload),utc_now()))
            return self.terms._done(c,principal,spec.request_id,data,{'feedback_id':fid,'revision':revision,'state':state,'published':False},'feedback_revise')

    def decide(self,fid,spec,*,principal):
        self.terms._manager(principal);data={'action':'feedback_decide','feedback_id':fid,**spec.model_dump(mode='json')}
        with self.search.database.transaction(write=True) as c:
            row=self._row(c,fid,principal)
            replay=self.terms._replay(c,principal,spec.request_id,data)
            if replay:return replay
            if row['revision']!=spec.expected_revision or row['state'] not in ('pending','needs_info'):raise RevisionConflict('feedback revision or state changed')
            cid=None;state={'needs_info':'needs_info','reject':'rejected','promote':'draft_created'}[spec.action]
            if spec.action=='promote':
                head=self.terms._head(c)
                if not head['lexicon_release_id']:raise ValueError('publish initial taxonomy and concepts before promoting aliases')
                _,lexicon=self.terms._release(c,head['lexicon_release_id']);payload=json.loads(row['payload_json'])
                events=c.execute("SELECT payload_json,actor_id,event_id FROM feedback_events WHERE feedback_id=? AND action IN ('submit','revise') ORDER BY event_id DESC LIMIT 20",(fid,)).fetchall()
                sources=[{'kind':'human','locator':f"feedback:{fid}:event:{e['event_id']}:actor:{e['actor_id']}",
                    'quote':json.loads(e['payload_json'])['quote'] or json.loads(e['payload_json'])['expression'],
                    'usage':'User-provided project terminology candidate; external usage scope not asserted'} for e in events]
                alias=Alias.model_validate_json(encode({'entry_id':'feedback-alias-'+uuid4().hex,'sources':sources,'rationale':spec.note,
                    'expression':payload['expression'],'target_id':payload['target_id'],'relation':payload['relation'],'context':payload['context'],'default_expand':False}))
                changed=lexicon.model_dump(mode='json');changed['aliases'].append(alias.model_dump(mode='json'))
                changed=Lexicon.model_validate_json(encode(changed));cid='candidate-'+uuid4().hex
                c.execute('INSERT INTO terminology_candidates VALUES(?,?,?,?,?,?,?,?,?)',
                    (cid,'lexicon',principal.actor_id,head['lexicon_release_id'],1,'draft',encode(changed.model_dump(mode='json')),'{}',utc_now()))
                c.execute('INSERT INTO terminology_events(actor_id,candidate_id,action,payload_json,created_at) VALUES(?,?,?,?,?)',
                    (principal.actor_id,cid,'feedback_candidate',encode({'feedback_id':fid,'note':spec.note}),utc_now()))
            revision=row['revision']+1
            c.execute('UPDATE terminology_feedback SET revision=?,state=?,candidate_id=? WHERE feedback_id=?',(revision,state,cid,fid))
            c.execute('INSERT INTO feedback_events(feedback_id,actor_id,action,payload_json,created_at) VALUES(?,?,?,?,?)',
                (fid,principal.actor_id,spec.action,encode(spec.model_dump(mode='json')|{'candidate_id':cid}),utc_now()))
            return self.terms._done(c,principal,spec.request_id,data,{'feedback_id':fid,'revision':revision,'state':state,'candidate_id':cid,'published':False},'feedback_decide',cid)

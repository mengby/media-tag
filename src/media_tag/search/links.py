"""Human concepts bind actual observations, source hashes and immutable word meanings."""
import json
from uuid import uuid4
from media_tag.catalog import AccessDenied,RevisionConflict,NotFound
from media_tag.catalog.database import utc_now
from media_tag.catalog.service import encode
from media_tag.review.policy import MEDICAL
from .service import concept_fingerprint
from media_tag.terminology.lexicon.service import fingerprint


class ConceptLinks:
    def __init__(self,search):self.search=search

    def _current(self,c):
        head=self.search.resolver.indices.head(c)
        lexid=head['lexicon_release_id'] or self.search.terminology._head(c)['lexicon_release_id']
        if not lexid:raise NotFound('published vocabulary unavailable')
        _,lexicon=self.search.terminology._release(c,lexid)
        _,taxonomy=self.search.terminology._release(c,lexicon.taxonomy_release_id)
        return lexid,lexicon,taxonomy

    @staticmethod
    def current(c,row,concept,source_sha):
        if not row['active'] or row['source_sha256']!=source_sha or concept is None or concept.status!='active' or row['concept_sha256']!=concept_fingerprint(concept):return False
        payload=json.loads(row['payload_json']);bases=payload.get('field_bases',{})
        if payload.get('medical_concept') and not bases:return False
        for field,digest in bases.items():
            effective=c.execute('SELECT payload FROM effective_values WHERE asset_id=? AND field=?',(row['asset_id'],field)).fetchone()
            if not effective or fingerprint(json.loads(effective[0]))!=digest:return False
        return True

    def detail(self,asset_id,*,principal):
        self.search.files.authorize(asset_id,None,principal=principal,for_bytes=True)
        with self.search.database.transaction() as c:
            asset=self.search.catalog._authorize(c,asset_id,principal)
            try:lexid,lexicon,_=self._current(c)
            except NotFound:lexid=None;lexicon=None
            concepts={con.entry_id:con for con in lexicon.concepts} if lexicon else {}
            links=[]
            for row in c.execute('SELECT * FROM asset_concept_links WHERE asset_id=? ORDER BY created_at,link_id',(asset_id,)):
                con=concepts.get(row['concept_id'])
                valid=self.current(c,row,con,asset['blob_sha256'])
                links.append({k:row[k] for k in ('link_id','concept_id','active','polarity','direction','start_seconds','end_seconds','actor_id','created_at')}|
                    {'name':con.name if con else row['concept_id'],'current':valid,'payload':json.loads(row['payload_json'])})
            can_edit=principal.role in ('editor','admin')
            if can_edit:
                try:self.search.catalog._authorize(c,asset_id,principal,'edit')
                except AccessDenied:can_edit=False
            return {'asset_id':asset_id,'source_sha256':asset['blob_sha256'],'revision':asset['revision'],'lexicon_release_id':lexid,'can_edit':can_edit,
                    'links':links,'medical_reviewer':bool(self.search.reviews and principal.actor_id in self.search.reviews.config.reviewers)}

    def create(self,asset_id,spec,*,principal):
        reviews=self.search.reviews
        if reviews is None:raise AccessDenied('human observation service required')
        opened=self.search.files.open(asset_id,spec.source_sha256,principal=principal);mime=opened.mime;opened.close()
        with self.search.database.transaction(write=True) as c:
            asset=reviews._asset(c,asset_id,principal,spec.source_sha256)
            digest,previous=reviews._previous(c,principal,spec,'concept_link:'+asset_id)
            if previous:return previous
            self.search.catalog._expected(asset,spec.expected_revision)
            lexid,lexicon,taxonomy=self._current(c)
            if spec.lexicon_release_id!=lexid:raise RevisionConflict('concept vocabulary changed')
            concept=next((con for con in lexicon.concepts if con.entry_id==spec.concept_id and con.status=='active'),None)
            if concept is None:raise ValueError('active concept required')
            options={o.entry_id:o for o in taxonomy.options if o.status=='active'}
            medical=concept.concept_type!='other' or any(options[oid].field in MEDICAL for oid in concept.option_ids if oid in options)
            if medical:reviews._medical(principal)
            field_bases={}
            for oid in concept.option_ids:
                option=options.get(oid)
                if option and option.field in MEDICAL:
                    effective=c.execute('SELECT payload FROM effective_values WHERE asset_id=? AND field=?',(asset_id,option.field)).fetchone()
                    value=json.loads(effective[0]) if effective else None
                    decision=c.execute("SELECT payload_json FROM review_decisions WHERE asset_id=? AND field=? AND asset_revision=? AND decision!='reject'",
                        (asset_id,option.field,value['revision'] if value else 0)).fetchone()
                    if not value or value['value']['state']!='observed' or option.value not in value['value']['values'] or not decision or json.loads(decision[0]).get('effective')!=value:
                        raise ValueError('confirm the corresponding medical field in D06 before linking a concept')
                    field_bases[option.field]=fingerprint(value)
            if medical and not field_bases:
                raise ValueError('medical concepts require a released medical option and its D06 field confirmation')
            if len(set(spec.observation_ids))!=len(spec.observation_ids):raise ValueError('unique observations required')
            observations=[]
            for oid in spec.observation_ids:
                row=c.execute('SELECT * FROM review_observations WHERE observation_id=? AND asset_id=?',(oid,asset_id)).fetchone()
                if not row or row['actor_id']!=principal.actor_id or row['source_sha256']!=spec.source_sha256:raise ValueError('own actual observations required')
                value=json.loads(row['payload_json'])
                if value['kind']=='human_record' or not value['viewed']:raise ValueError('media observation required')
                observations.append(value)
            start,end=spec.start_seconds,spec.end_seconds
            continuous=[o for o in observations if o['kind']=='video_segment']
            dynamic=spec.direction in ('increasing','decreasing','transition')
            if dynamic and not continuous:raise ValueError('direction requires continuous video observation')
            if mime.startswith('video/') or continuous:
                if start is None and continuous:start,end=continuous[0]['start_seconds'],continuous[0]['end_seconds']
                if start is None or not any(o['start_seconds']<=start<end<=o['end_seconds'] for o in continuous):
                    raise ValueError('video concept scope must be covered by a continuous observation')
            elif start is not None:
                audio=[o for o in observations if o['kind']=='audio_segment']
                if not any(o['start_seconds']<=start<end<=o['end_seconds'] for o in audio):raise ValueError('interval lacks audio observation')
            elif dynamic:raise ValueError('static media cannot prove direction')
            link_id='link-'+uuid4().hex;revision=asset['revision']+1
            payload=spec.model_dump(mode='json')|{'actual_start_seconds':start,'actual_end_seconds':end,'medical_concept':medical,'field_bases':field_bases}
            c.execute('INSERT INTO asset_concept_links VALUES(?,?,?,?,?,?,1,?,?,?,?,?,?,?,?)',
                (link_id,asset_id,spec.source_sha256,spec.concept_id,lexid,concept_fingerprint(concept),spec.polarity,spec.direction,
                 start,end,principal.actor_id,revision,encode(payload),utc_now()))
            c.execute('INSERT INTO concept_link_events(link_id,actor_id,action,payload_json,created_at) VALUES(?,?,?,?,?)',
                (link_id,principal.actor_id,'confirm',encode(payload),utc_now()))
            c.execute('UPDATE assets SET revision=? WHERE asset_id=?',(revision,asset_id))
            self.search.catalog._event(c,asset_id,revision,principal,'human_concept_link',{}, {'link_id':link_id,'concept_id':spec.concept_id})
            return reviews._remember(c,principal,spec,digest,{'asset_id':asset_id,'link_id':link_id,'revision':revision,'medical_confirmation_of_asset':False})

    def revoke(self,asset_id,link_id,spec,*,principal):
        reviews=self.search.reviews
        if reviews is None:raise AccessDenied('human review capability required')
        with self.search.database.transaction(write=True) as c:
            asset=reviews._asset(c,asset_id,principal,spec.source_sha256)
            digest,previous=reviews._previous(c,principal,spec,'concept_revoke:'+asset_id+':'+link_id)
            if previous:return previous
            row=c.execute('SELECT * FROM asset_concept_links WHERE asset_id=? AND link_id=?',(asset_id,link_id)).fetchone()
            if row is None:raise NotFound('concept link unavailable')
            if json.loads(row['payload_json'])['medical_concept']:reviews._medical(principal)
            self.search.catalog._expected(asset,spec.expected_revision)
            revision=asset['revision']+1
            c.execute('UPDATE asset_concept_links SET active=0 WHERE link_id=?',(link_id,))
            c.execute('INSERT INTO concept_link_events(link_id,actor_id,action,payload_json,created_at) VALUES(?,?,?,?,?)',
                (link_id,principal.actor_id,'revoke',encode(spec.model_dump(mode='json')),utc_now()))
            c.execute('UPDATE assets SET revision=? WHERE asset_id=?',(revision,asset_id))
            self.search.catalog._event(c,asset_id,revision,principal,'revoke_concept_link',{'link_id':link_id},{'active':False})
            return reviews._remember(c,principal,spec,digest,{'asset_id':asset_id,'link_id':link_id,'revision':revision,'active':False})

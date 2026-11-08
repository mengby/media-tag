"""Immutable local vectors; activate only a complete verified vocabulary/index tuple."""
from array import array
from hashlib import sha256
import json
import math
import os
import sys
import time
from uuid import uuid4
from media_tag.catalog import RevisionConflict,NotFound
from media_tag.catalog.database import utc_now
from media_tag.catalog.service import encode
from media_tag.storage import IntegrityError,StorageError
from media_tag.models.contracts import ModelFailure
from media_tag.terminology.lexicon.service import fingerprint
from media_tag.terminology.lexicon.validation import issues
from media_tag.terminology.resolve.matching import concept_documents
from media_tag.terminology.resolve.models import PREPROCESS_VERSION
from media_tag.workers.evidence import _directory


def checked_vectors(rows,count):
    if not isinstance(rows,list) or len(rows)!=count:raise IntegrityError('text vector row count')
    for vector in rows:
        if not isinstance(vector,list) or len(vector)!=512 or any(type(v) not in (float,int) or not math.isfinite(v) for v in vector):
            raise IntegrityError('text vector shape or finite check')
        if abs(math.fsum(v*v for v in vector)-1)>0.0001:raise IntegrityError('text vector is not normalized')
    return rows


class IndexStore:
    def __init__(self,terminology,registry,encoder,options):
        self.terminology,self.router,self.database=terminology,terminology.router,terminology.database
        self.registry,self.encoder,self.options=registry,encoder,options

    @staticmethod
    def head(c):return dict(c.execute('SELECT * FROM terminology_serving_head WHERE singleton=1').fetchone())

    def pair(self,c,taxonomy_id,lexicon_id):
        taxrow,taxonomy=self.terminology._release(c,taxonomy_id)
        lexrow,lexicon=self.terminology._release(c,lexicon_id)
        if taxrow['kind']!='taxonomy' or lexrow['kind']!='lexicon' or lexicon.taxonomy_release_id!=taxonomy_id:
            raise IntegrityError('text vocabulary pair mismatch')
        if issues(taxonomy,self.terminology.contract) or issues(lexicon,self.terminology.contract,taxonomy):
            raise IntegrityError('text vocabulary is invalid')
        return taxonomy,lexicon,{'taxonomy_version':taxrow['version'],'lexicon_version':lexrow['version']}

    def read(self,c,index_id,*,taxonomy_id,lexicon_id,documents=None):
        row=c.execute('SELECT * FROM terminology_text_indices WHERE index_id=?',(index_id,)).fetchone()
        if row is None:raise NotFound('text index unavailable')
        if (row['taxonomy_release_id'],row['lexicon_release_id'],row['model_revision'],row['preprocess_version'])!=(taxonomy_id,lexicon_id,self.registry.revision,PREPROCESS_VERSION):
            raise IntegrityError('text index version mismatch')
        bundle=json.loads(row['bundle_json']);values={}
        for name,digest in bundle['files'].items():
            with self.router.files.open_read(f"{bundle['directory']}/{name}") as stream:raw=stream.read(32*1024**2+1)
            if len(raw)>32*1024**2 or sha256(raw).hexdigest()!=digest:raise IntegrityError('text index file changed')
            values[name]=raw
        metadata=json.loads(values['index.json']);docs=json.loads(values['documents.json'])
        if metadata!={'schema_version':'d14-text-index-v1','taxonomy_release_id':taxonomy_id,'lexicon_release_id':lexicon_id,
            'model_revision':self.registry.revision,'preprocess_version':PREPROCESS_VERSION,'dimension':512,
            'documents_sha256':sha256(values['documents.json']).hexdigest(),'rows':len(docs)}:
            raise IntegrityError('text index metadata mismatch')
        if documents is not None and docs!=documents:raise IntegrityError('text index corpus changed')
        if not 1<=len(docs)<=5000 or len(values['vectors.f32'])!=len(docs)*512*4:raise IntegrityError('text index shape mismatch')
        packed=array('f');packed.frombytes(values['vectors.f32'])
        if sys.byteorder!='little':packed.byteswap()
        vectors=[list(packed[i*512:(i+1)*512]) for i in range(len(docs))]
        checked_vectors(vectors,len(docs))
        return {'index_id':index_id,'fingerprint':row['fingerprint'],'documents':docs,'vectors':vectors}

    def _write(self,folder,name,raw):
        if len(raw)>32*1024**2:raise ValueError('text index file budget')
        with (folder/name).open('xb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())

    def activate(self,spec,*,principal):
        self.terminology._manager(principal,publish=True)
        if not self.options.model_enabled or self.encoder is None:raise ModelFailure('text_model_disabled')
        data={'action':'text_index_activate',**spec.model_dump(mode='json')}
        with self.database.transaction() as c:
            replay=self.terminology._replay(c,principal,spec.request_id,data)
            if replay:
                self.read(c,replay['index_id'],taxonomy_id=spec.taxonomy_release_id,lexicon_id=spec.lexicon_release_id)
                return replay
            published=self.terminology._head(c);serving=self.head(c)
            self._expected(published,serving,spec)
            _,lexicon,versions=self.pair(c,spec.taxonomy_release_id,spec.lexicon_release_id)
            documents=concept_documents(lexicon)
            if not documents:raise ValueError('no active concept documents')
            logical=fingerprint({'documents':documents,'taxonomy':spec.taxonomy_release_id,'lexicon':spec.lexicon_release_id,
                                 'model':self.registry.revision,'preprocess':PREPROCESS_VERSION})
            reuse=None
            for row in c.execute('SELECT index_id FROM terminology_text_indices WHERE fingerprint=? ORDER BY created_at DESC',(logical,)):
                try:self.read(c,row['index_id'],taxonomy_id=spec.taxonomy_release_id,lexicon_id=spec.lexicon_release_id,documents=documents)
                except (StorageError,ValueError,NotFound):continue
                reuse=row['index_id'];break
        # Building cannot block the Router DB or occupy a GPU hold. The final CAS fences
        # edits/rollback during encoding; until commit, queries retain the old pair.
        folder=None;started=time.monotonic();bundle=None
        try:
            if reuse is None:
                relative='jobs/text-index-'+uuid4().hex;folder=_directory(self.router.workspace,relative)
                (folder/'README.md').write_text('Private D14 candidate vectors. Only the serving DB tuple activates this snapshot.\n',encoding='utf-8')
                self._write(folder,'inputs.json',encode({'request':data,'model_revision':self.registry.revision,'preprocess_version':PREPROCESS_VERSION,
                    'document_fingerprint':logical,'versions':versions}).encode())
                vectors=[]
                for offset in range(0,len(documents),16):
                    remaining=self.options.build_timeout_seconds-(time.monotonic()-started)
                    if remaining<=0:raise ModelFailure('text_model_timeout')
                    batch=documents[offset:offset+16]
                    result=self.encoder.encode([d['text'] for d in batch],timeout_seconds=remaining)
                    vectors.extend(checked_vectors(result,len(batch)))
                docsraw=encode(documents).encode();packed=array('f',(v for vector in vectors for v in vector))
                if sys.byteorder!='little':packed.byteswap()
                metadata={'schema_version':'d14-text-index-v1','taxonomy_release_id':spec.taxonomy_release_id,'lexicon_release_id':spec.lexicon_release_id,
                    'model_revision':self.registry.revision,'preprocess_version':PREPROCESS_VERSION,'dimension':512,
                    'documents_sha256':sha256(docsraw).hexdigest(),'rows':len(documents)}
                values={'documents.json':docsraw,'vectors.f32':packed.tobytes(),'index.json':encode(metadata).encode()}
                bundle={'directory':relative,'files':{name:sha256(raw).hexdigest() for name,raw in values.items()}}
                for name,raw in values.items():self._write(folder,name,raw)
                reuse='text-index-'+uuid4().hex
            with self.database.transaction(write=True) as c:
                replay=self.terminology._replay(c,principal,spec.request_id,data)
                if replay:return replay
                published=self.terminology._head(c);serving=self.head(c);self._expected(published,serving,spec)
                self.pair(c,spec.taxonomy_release_id,spec.lexicon_release_id)
                if bundle:
                    c.execute('INSERT INTO terminology_text_indices VALUES(?,?,?,?,?,?,?,?)',(reuse,logical,spec.taxonomy_release_id,spec.lexicon_release_id,
                        self.registry.revision,PREPROCESS_VERSION,encode(bundle),utc_now()))
                self.read(c,reuse,taxonomy_id=spec.taxonomy_release_id,lexicon_id=spec.lexicon_release_id,documents=documents)
                c.execute('UPDATE terminology_serving_head SET epoch=epoch+1,vocabulary_generation=?,taxonomy_release_id=?,lexicon_release_id=?,index_id=? WHERE singleton=1',
                          (published['generation'],spec.taxonomy_release_id,spec.lexicon_release_id,reuse))
                result={'serving_epoch':serving['epoch']+1,'vocabulary_generation':published['generation'],'taxonomy_release_id':spec.taxonomy_release_id,
                    'lexicon_release_id':spec.lexicon_release_id,'index_id':reuse,'model_revision':self.registry.revision,'preprocess_version':PREPROCESS_VERSION,
                    'document_count':len(documents),'reused':bundle is None,'elapsed_seconds':time.monotonic()-started}
                if folder:self._write(folder,'validation.json',encode(result).encode())
                return self.terminology._done(c,principal,spec.request_id,data,result,'text_index_activate')
        except Exception as exc:
            if folder:
                try:self._write(folder,'failure.json',encode({'state':'failed','code':exc.code if isinstance(exc,ModelFailure) else type(exc).__name__,
                    'elapsed_seconds':time.monotonic()-started,'old_serving_tuple_preserved':True}).encode())
                except OSError:pass
            raise

    @staticmethod
    def _expected(published,serving,spec):
        if (published['generation'],published['taxonomy_release_id'],published['lexicon_release_id'],serving['epoch'])!=(
            spec.expected_generation,spec.taxonomy_release_id,spec.lexicon_release_id,spec.expected_serving_epoch):
            raise RevisionConflict('published or serving version changed')

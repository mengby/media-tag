import json
import pytest
from media_tag.catalog import RevisionConflict,AccessDenied
from media_tag.models.contracts import ModelFailure
from media_tag.terminology.resolve.models import ResolveInput,IndexActivation
from ..lexicon.conftest import publish,lexicon,concept,alias


def next_lex(resolved,**changes):
    env=resolved.env
    payload=resolved.payload.model_dump(mode='json')|changes
    from media_tag.terminology.contracts import Lexicon
    release=publish(env,Lexicon.model_validate_json(json.dumps(payload)),'synthetic-v2',resolved.lex['release_id'])
    spec=resolved.activation.model_copy(update={'request_id':'activate-v2','expected_generation':3,'expected_serving_epoch':1,'lexicon_release_id':release['release_id']})
    return release,spec


def test_new_words_never_use_old_vectors_and_build_failure_keeps_old_serving_pair(resolved):
    env=resolved.env;release,spec=next_lex(resolved,aliases=[alias(expression='全新已发布说法')])
    old=resolved.resolver.resolve(ResolveInput(query='合成圆圈'),principal=env.owner)
    assert old['state']=='resolved' and old['pending_publication'] and old['versions']['lexicon_release_id']==resolved.lex['release_id']
    resolved.encoder.fail='text_model_timeout'
    with pytest.raises(ModelFailure):resolved.resolver.indices.activate(spec,principal=env.publisher)
    assert resolved.resolver.status(principal=env.owner)['serving']['index_id']==resolved.index['index_id']
    resolved.encoder.fail=None
    switched=resolved.resolver.indices.activate(spec,principal=env.publisher)
    assert switched['serving_epoch']==2
    result=resolved.resolver.resolve(ResolveInput(query='全新已发布说法'),principal=env.owner)
    assert result['state']=='resolved' and result['versions']['embedding_index_id']==switched['index_id']
    assert not result['pending_publication']
    assert resolved.resolver.indices.activate(spec,principal=env.publisher)==switched


def test_activation_cas_during_model_build_and_replay_does_not_duplicate(resolved):
    env=resolved.env;release,spec=next_lex(resolved,concepts=[concept(name='合成圆形',definition='Changed synthetic semantics'),concept('concept-b',oid='option-b',name='合成方形')])
    from media_tag.terminology.contracts import Rollback
    def rollback():
        resolved.encoder.on_encode=None
        env.service.rollback(Rollback(request_id='during-build',expected_generation=3,taxonomy_release_id=resolved.tax['release_id'],
            lexicon_release_id=resolved.lex['release_id'],note='Synthetic concurrent rollback'),principal=env.publisher)
    resolved.encoder.on_encode=rollback
    with pytest.raises(RevisionConflict):resolved.resolver.indices.activate(spec,principal=env.publisher)
    assert resolved.resolver.status(principal=env.owner)['serving']['epoch']==1
    assert resolved.resolver.indices.activate(resolved.activation,principal=env.publisher)==resolved.index


@pytest.mark.parametrize('name',['documents.json','vectors.f32','index.json','validation.json'])
def test_write_failure_never_switches_head(resolved,monkeypatch,name):
    env=resolved.env;_,spec=next_lex(resolved,aliases=[])
    original=resolved.resolver.indices._write
    def fail(folder,filename,raw):
        if filename==name:raise OSError('synthetic injected failure')
        return original(folder,filename,raw)
    monkeypatch.setattr(resolved.resolver.indices,'_write',fail)
    with pytest.raises(OSError):resolved.resolver.indices.activate(spec,principal=env.publisher)
    assert resolved.resolver.status(principal=env.owner)['serving']['epoch']==1


def test_corrupt_index_does_not_hide_behind_cache_or_use_random_vectors(resolved):
    env=resolved.env;request=ResolveInput(query='某个近义表达')
    assert resolved.resolver.resolve(request,principal=env.owner)['model_state']=='used'
    with env.database.transaction() as c:
        row=c.execute('SELECT bundle_json FROM terminology_text_indices WHERE index_id=?',(resolved.index['index_id'],)).fetchone()
    bundle=json.loads(row[0]);path=env.workspace/bundle['directory']/'vectors.f32';path.write_bytes(b'bad')
    result=resolved.resolver.resolve(request,principal=env.owner)
    assert result['state']=='degraded' and not result['cache_hit'] and 'text_index_unavailable' in result['errors']
    assert resolved.resolver.resolve(ResolveInput(query='合成圆圈'),principal=env.owner)['state']=='resolved'
    # A new request can create a fresh immutable replica of the same logical index.
    spec=resolved.activation.model_copy(update={'request_id':'repair','expected_serving_epoch':1})
    repaired=resolved.resolver.indices.activate(spec,principal=env.publisher)
    assert repaired['index_id']!=resolved.index['index_id']


def test_rollback_reuses_verified_old_bundle_without_encoding(resolved):
    env=resolved.env;_,spec=next_lex(resolved,aliases=[])
    resolved.resolver.indices.activate(spec,principal=env.publisher)
    from media_tag.terminology.contracts import Rollback
    env.service.rollback(Rollback(request_id='rollback',expected_generation=3,taxonomy_release_id=resolved.tax['release_id'],lexicon_release_id=resolved.lex['release_id'],note='Synthetic paired rollback'),principal=env.publisher)
    resolved.encoder.fail='text_model_missing'
    restore=resolved.activation.model_copy(update={'request_id':'restore','expected_generation':4,'expected_serving_epoch':2})
    result=resolved.resolver.indices.activate(restore,principal=env.publisher)
    assert result['reused'] and result['index_id']==resolved.index['index_id']
    with env.database.transaction() as c:
        assert c.execute('SELECT count(*) FROM effective_values').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM router_jobs').fetchone()[0]==0


def test_two_activation_requests_cannot_overwrite_each_other(resolved):
    from concurrent.futures import ThreadPoolExecutor
    env=resolved.env;_,spec=next_lex(resolved,aliases=[])
    def activate(number):
        try:return resolved.resolver.indices.activate(spec.model_copy(update={'request_id':'concurrent-'+str(number)}),principal=env.publisher)
        except RevisionConflict:return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(activate,range(2)))
    assert results.count('conflict')==1
    assert resolved.resolver.status(principal=env.owner)['serving']['epoch']==2

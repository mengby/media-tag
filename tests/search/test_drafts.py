from media_tag.contracts.models import Versions,TaskLimits
from media_tag.router.models import StepSpec,JobSpec
from media_tag.router.runtime import LocalExecutor
from media_tag.terminology.resolve.models import ResolveInput,ResolveTask,PREPROCESS_VERSION
from media_tag.workers.terminology import terminology_stages,TerminologyHandlers
from .test_query import query


def test_only_current_verified_worker_reports_enter_opt_in_draft_search(sought):
    env=sought.env;resolver=sought.resolver
    env.router.stages.update(terminology_stages(resolver.registry))
    env.router.register_worker(env.worker.actor_id,('terminology_resolve',),principal=env.admin)
    request=ResolveTask(taxonomy_release_id=sought.tax['release_id'],lexicon_release_id=sought.lex['release_id'],
        embedding_index_id=sought.activation['index_id'],request=ResolveInput(query='方块说法',purpose='tag'))
    step=StepSpec(key='terms',stage='terminology_resolve',config=request.model_dump(mode='json'),
        versions=Versions(rules_version=env.contract.version,preprocess_version=PREPROCESS_VERSION,
            taxonomy_version=sought.tax['version'],lexicon_version=sought.lex['version'],model_revision=resolver.registry.revision),
        limits=TaskLimits(timeout_seconds=30,max_attempts=1,gpu_slots=0))
    job=env.router.submit(JobSpec(asset_id=sought.asset.asset_id,steps=(step,)),request_id='draft-search',principal=env.owner)
    assert LocalExecutor(env.router,env.worker,TerminologyHandlers(resolver)).run_once()['accepted']
    assert not query(sought,'方块说法')['items']
    result=query(sought,'方块说法',include_drafts=True)
    reasons=result['items'][0]['versions'][0]['reasons']
    assert all(r['kind']=='ai_draft' and not r['medical_evidence'] for r in reasons)
    with env.database.transaction(write=True) as c:
        assert c.execute('SELECT count(*) FROM effective_values').fetchone()[0]==1
        c.execute('UPDATE router_jobs SET cancelled=1 WHERE job_id=?',(job['job_id'],))
    assert not query(sought,'方块说法',include_drafts=True)['items']

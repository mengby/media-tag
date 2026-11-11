import pytest

from media_tag.catalog import AccessDenied
from media_tag.cleaning import CleaningRecipe
from media_tag.processing_queue import CleaningSubmission, ProcessingDeployment, ProcessingService, stages
from media_tag.variants.models import Recipe, MaskSpec
from variants.conftest import plan, produced, register


def test_pending_privacy_derivative_cannot_be_reprocessed_as_original(variants, tools):
    env=variants
    planned=plan(env,recipe=Recipe(mask=MaskSpec(revision='synthetic-mask')))
    rendered=produced(env,planned)
    result=register(env,planned,rendered)
    # The existing derivative remains behind the existing FileService privacy gate.
    derived_id=result.asset_id
    with env.database.transaction() as connection:
        row=connection.execute('SELECT * FROM assets WHERE asset_id=?',(derived_id,)).fetchone()
        revision,sha=row['revision'],row['blob_sha256']
        before=connection.execute('SELECT count(*) FROM router_jobs').fetchone()[0]
    env.router.stages.update(stages(tools))
    service=ProcessingService(env.router,tools,options=ProcessingDeployment(enabled=True))
    spec=CleaningSubmission(request_id='blocked',asset_id=derived_id,expected_revision=revision,source_sha256=sha,
        recipe=CleaningRecipe(source_sha256=sha,content_kind='video'))
    with pytest.raises(AccessDenied):
        service.request(spec,principal=env.owner)
    with env.database.transaction() as connection:
        assert connection.execute('SELECT count(*) FROM router_jobs').fetchone()[0]==before

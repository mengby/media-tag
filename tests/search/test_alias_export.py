import csv
from io import StringIO
import json
import pytest
from media_tag.export.service import ExportService
from media_tag.export.models import ExportSubmission,AssetSelection
from media_tag.search.models import ConceptLink
from media_tag.catalog import RevisionConflict
from media_tag.terminology.resolve.models import IndexActivation
from terminology.lexicon.conftest import publish,alias


def test_alias_csv_is_opt_in_finite_and_does_not_change_human_values(sought):
    env=sought.env
    linked=sought.search.links.create(sought.asset.asset_id,ConceptLink(request_id='link',expected_revision=sought.asset.revision,source_sha256=sought.asset.sha256,
        lexicon_release_id=sought.lex['release_id'],concept_id='concept-a',observation_ids=(sought.observation,),note='Synthetic actual geometry association'),principal=env.owner)
    service=ExportService(sought.reviews)
    selection=AssetSelection(asset_id=sought.asset.asset_id,expected_revision=linked['revision'],source_sha256=sought.asset.sha256)
    plain=service.create(ExportSubmission(request_id='plain',assets=(selection,)),principal=env.owner)
    default=list(csv.DictReader(StringIO(service.content(plain['export_id'],'assets.csv',principal=env.owner).decode('utf-8-sig'))))[0]
    assert default['中文检索词']==''
    exported=service.create(ExportSubmission(request_id='aliases',assets=(selection,),include_confirmed_aliases=True),principal=env.owner)
    row=list(csv.DictReader(StringIO(service.content(exported['export_id'],'assets.csv',principal=env.owner).decode('utf-8-sig'))))[0]
    assert row['中文检索词']=='合成圆圈' and len(row)==35
    with env.database.transaction() as c:assert not c.execute("SELECT 1 FROM effective_values WHERE asset_id=? AND field='中文检索词'",(sought.asset.asset_id,)).fetchone()
    payload=sought.payload.model_dump(mode='json');payload['aliases'].append(alias('new-alias',expression='新的合成别名'))
    from media_tag.terminology.contracts import Lexicon
    new=publish(env,Lexicon.model_validate_json(json.dumps(payload)),'synthetic-v2',sought.lex['release_id'])
    sought.resolver.indices.activate(IndexActivation(request_id='activate-v2',expected_generation=3,expected_serving_epoch=1,taxonomy_release_id=sought.tax['release_id'],lexicon_release_id=new['release_id'],note='Synthetic alias version change'),principal=env.publisher)
    with pytest.raises(RevisionConflict):service.content(exported['export_id'],'assets.csv',principal=env.owner)
    assert service.content(plain['export_id'],'assets.csv',principal=env.owner)

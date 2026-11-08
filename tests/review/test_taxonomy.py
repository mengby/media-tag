from hashlib import sha256
import json
import pytest
from media_tag.review.models import ReviewDeployment
from media_tag.review.policy import ReviewPolicy
from media_tag.contracts.models import FieldValue


def published(env,tmp_path,**patch):
    content={'schema_version':'d06-taxonomy-options-v1','template_fingerprint':env.contract.fingerprint,'fields':{'科室':['合成科室-仅测试']}}
    data=json.dumps(content).encode();(tmp_path/'options.json').write_bytes(data)
    manifest={'kind':'taxonomy','version':'synthetic-v1','status':'published','content_file':'options.json','content_sha256':sha256(data).hexdigest(),
        'published_by':'synthetic-publisher','published_at':'2026-11-08T10:00:00+08:00',**patch}
    raw=json.dumps(manifest).encode();path=tmp_path/'manifest.json';path.write_bytes(raw)
    return ReviewDeployment(enabled=True,taxonomy_manifest=str(path),taxonomy_version='synthetic-v1',taxonomy_manifest_sha256=sha256(raw).hexdigest(),trusted_publishers=('synthetic-publisher',))


def test_pinned_published_consumer_accepts_only_explicit_options(reviewed,tmp_path):
    env=reviewed;config=published(env,tmp_path)
    policy=ReviewPolicy(env.contract,config);definition=policy.asset_fields['科室']
    assert policy.options(definition)==('合成科室-仅测试',)
    policy.validate(definition,FieldValue(state='observed',values=('合成科室-仅测试',)))
    with pytest.raises(ValueError):policy.validate(definition,FieldValue(state='observed',values=('五官科',)))
    with pytest.raises(ValueError):ReviewPolicy(env.contract,config.model_copy(update={'trusted_publishers':()}))
    with pytest.raises(ValueError):ReviewPolicy(env.contract,config.model_copy(update={'taxonomy_manifest_sha256':'0'*64}))


@pytest.mark.parametrize('patch',[{'status':'draft'},{'version':'other'},{'content_sha256':'0'*64}])
def test_draft_wrong_version_or_content_hash_is_rejected(reviewed,tmp_path,patch):
    with pytest.raises(ValueError):ReviewPolicy(reviewed.contract,published(reviewed,tmp_path,**patch))

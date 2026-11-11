"""Real isolated registered derived output and existing FileService privacy gate."""
import pytest
from variants.conftest import variants,env,intake,tools,plan,produced,register
from media_tag.variants.models import Recipe,MaskSpec
from media_tag.review.policy import ReviewPolicy
from media_tag.review.models import ReviewDeployment
from media_tag.files.service import FileError,FileService
from media_tag.topic_evaluation import freeze_catalog_export


def test_actual_unreviewed_variant_cannot_be_frozen(variants,frozen):
    v=variants
    planned=plan(v,recipe=Recipe(mask=MaskSpec(revision='synthetic-mask')))
    item=register(v,planned,produced(v,planned))
    v.catalog.review_policy=ReviewPolicy(v.contract,ReviewDeployment())
    case,_=frozen.add('synthetic-derived')
    case.update(source_sha256=item.sha256,content_kind='video')
    dataset,_=frozen.save()
    with pytest.raises(FileError) as ordinary:
        FileService(v.router).authorize(item.asset_id,item.sha256,principal=v.owner,for_bytes=True)
    assert ordinary.value.code=='privacy_review_pending'
    with pytest.raises(FileError) as exported:
        freeze_catalog_export(v.router,dataset,{'synthetic-derived':item.asset_id},'synthetic-local',
            principal=v.owner,private_output=frozen.output)
    assert exported.value.code=='privacy_review_pending' and not list(frozen.output.iterdir())

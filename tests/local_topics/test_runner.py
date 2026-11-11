from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path

from PIL import Image
import pytest

from media_tag.local_topics.contracts import LocalTopicOptions
from media_tag.local_topics.prompt import PROMPT_VERSION
from media_tag.models.contracts import ModelFailure
from media_tag.semantic.registry import SemanticRegistry


@pytest.mark.parametrize('change,code', [('prompt_version','local_topic_prompt_changed'),
    ('model_revision','local_topic_model_changed'), ('image_sha256','local_topic_model_image_changed')])
def test_runner_rejects_bad_bindings_before_native_model(tmp_path,change,code):
    import media_tag.local_topics.runner as runner
    image=BytesIO();Image.new('RGB',(2,2),'white').save(image,format='PNG')
    path=tmp_path/'image.png';path.write_bytes(image.getvalue())
    registry=SemanticRegistry(tmp_path)
    options=LocalTopicOptions(model_revision=registry.revision,policy_digest='a'*64)
    request={'prompt_version':PROMPT_VERSION,'model_root':str(tmp_path),'model_revision':registry.revision,
        'image_path':str(path),'image_sha256':sha256(image.getvalue()).hexdigest(),
        'options':options.model_dump(mode='json'),'field_prompts':[]}
    request[change]='b'*64
    with pytest.raises(ModelFailure) as error:runner.execute(request)
    assert error.value.code==code


def test_runner_never_executes_before_assignment_gate(tmp_path,monkeypatch):
    import media_tag.local_topics.runner as runner
    request=tmp_path/'request.json';response=tmp_path/'response.json'
    request.write_text(json.dumps({'start_gate':str(tmp_path/'start.ready')}))
    monkeypatch.setattr(runner,'watch_parent',lambda pid:None)
    ticks=iter([0.,11.])
    monkeypatch.setattr(runner.time,'monotonic',lambda:next(ticks))
    def should_not_execute(request):raise AssertionError('native execution before gate')
    monkeypatch.setattr(runner,'execute',should_not_execute)
    monkeypatch.setattr(runner.sys,'argv',['runner',str(request),str(response)])
    assert runner.main()==2 and not response.exists()

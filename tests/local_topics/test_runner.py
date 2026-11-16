from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path

from PIL import Image
import pytest

from media_tag.local_topics.contracts import Generation, LocalTopicOptions
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


@pytest.mark.parametrize('batch_worker', [False, True])
def test_runner_single_image_response_matches_request_protocol(tmp_path, monkeypatch, batch_worker):
    import media_tag.local_topics.runner as runner

    image = tmp_path / 'input.png'
    Image.new('RGB', (2, 2), 'white').save(image)
    digest = sha256(image.read_bytes()).hexdigest()
    revision = SemanticRegistry(tmp_path).revision
    options = LocalTopicOptions(model_revision=revision, policy_digest='a' * 64)
    request = {
        'prompt_version': PROMPT_VERSION, 'model_root': str(tmp_path), 'model_revision': revision,
        'image_path': str(image), 'image_sha256': digest, 'field_prompts': ['describe'],
        'options': options.model_dump(mode='json'), 'start_gate': str(tmp_path / 'start.ready'),
    }
    if batch_worker:
        request['microbatch_size'] = 4
    generation = Generation(backend='synthetic_protocol', model_revision=revision,
                            prompt_version=PROMPT_VERSION, image_sha256=digest, answers=('example',))
    monkeypatch.setattr(runner.LocalQwenGenerator, 'generate_batch', lambda self, **kwargs: [generation])
    expected = generation.model_dump(mode='json')
    if batch_worker:
        assert runner.execute(request) == {'ok': True, 'generation': expected, 'prefetched': []}
    else:
        del expected['retried_answers']
        assert runner.execute(request) == {'ok': True, 'generation': expected}


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

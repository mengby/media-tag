import json

import pytest

from media_tag.precision.contracts import PrecisionDeployment, PrecisionProvider, PrecisionReply, ProviderUsage


def test_precision_is_disabled_and_model_selection_is_explicit():
    config = PrecisionDeployment()
    assert not config.enabled and not config.auto_dispatch and not config.providers
    assert config.visual_provider is None and config.audio_provider is None


def test_provider_accounting_is_optional_unverified_and_rejects_false_verification():
    usage = ProviderUsage.model_validate_json(json.dumps({'input_tokens': 12, 'output_tokens': 5,
        'cost_micros': 42, 'currency': 'USD'}))
    assert not usage.verified and usage.cost_micros == 42
    for data in ({'cost_micros': 1}, {'currency': 'USD'}, {'verified': True}, {'input_tokens': -1}):
        with pytest.raises(ValueError):
            ProviderUsage.model_validate_json(json.dumps(data))


def test_response_rejects_approval_extra_fields_duplicate_topics_and_unresolved_candidate_abuse():
    reply = {'request_id': 'synthetic', 'source_sha256': 'a'*64, 'provider_revision': 'b'*64,
        'fields': [{'field': '画面主体', 'value': {'state': 'observed', 'values': ['synthetic']}}]}
    assert PrecisionReply.model_validate_json(json.dumps(reply)).usage is None
    with pytest.raises(ValueError):
        PrecisionReply.model_validate_json(json.dumps(reply | {'privacy_confirmation': True}))
    with pytest.raises(ValueError):
        PrecisionReply.model_validate_json(json.dumps(reply | {'fields': reply['fields']*2}))
    reply['fields'][0]['candidates'] = ['unresolved']
    with pytest.raises(ValueError):
        PrecisionReply.model_validate_json(json.dumps(reply))


def test_external_execution_is_not_silently_selected(tmp_path):
    (tmp_path/'README.md').write_text('Synthetic precision provider schema checks only.\n')
    data = {'provider_id': 'synthetic', 'adapter_path': str(tmp_path/'adapter.py'), 'adapter_sha256': 'a'*64,
        'model_id': 'not-selected', 'model_revision': 'protocol-only', 'inputs': ['image']}
    assert PrecisionProvider.model_validate_json(json.dumps(data)).execution == 'local'
    with pytest.raises(ValueError):
        PrecisionProvider.model_validate_json(json.dumps(data | {'execution': 'external'}))
    with pytest.raises(ValueError):
        PrecisionDeployment.model_validate_json(json.dumps({'enabled': True, 'visual_provider': 'not-configured'}))


def test_provider_revision_uses_code_and_model_identity_instead_of_local_adapter_address(tmp_path):
    provider = PrecisionProvider(provider_id='synthetic', adapter_path=str(tmp_path/'first.py'),
        adapter_sha256='a'*64, model_id='synthetic', model_revision='protocol-only', inputs=('image', 'audio'))
    moved = provider.model_copy(update={'adapter_path': str(tmp_path/'second.py'), 'inputs': ('audio', 'image')})
    assert provider.revision == moved.revision
    assert provider.revision != provider.model_copy(update={'adapter_sha256': 'b'*64}).revision


def test_local_bridge_does_not_inherit_unrelated_service_or_cloud_credentials(monkeypatch):
    from media_tag.precision.runtime import bridge_environment
    for name in ('HF_TOKEN', 'OPENAI_API_KEY', 'AWS_SECRET_ACCESS_KEY', 'OSS_ACCESS_KEY_SECRET', 'SYNTHETIC_API_TOKEN'):
        monkeypatch.setenv(name, 'synthetic-not-a-secret')
    environment = bridge_environment()
    assert not any(name in environment for name in ('HF_TOKEN', 'OPENAI_API_KEY', 'AWS_SECRET_ACCESS_KEY', 'OSS_ACCESS_KEY_SECRET', 'SYNTHETIC_API_TOKEN'))
    assert environment['HF_HUB_OFFLINE'] == environment['OMP_NUM_THREADS'] == '1'

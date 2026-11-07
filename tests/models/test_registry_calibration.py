from hashlib import sha256
from io import BytesIO
import json

import pytest

from media_tag.models.calibration import calibrate, candidate_fingerprint, evaluate, rank
from media_tag.models.contracts import DiagnosticCandidate, ModelFailure
from media_tag.models.registry import ModelRegistry


def tiny_registry(tmp_path):
    data = b'synthetic-not-a-model'
    record = {'path': 'ocr/synthetic.bin', 'size_bytes': len(data), 'sha256': sha256(data).hexdigest(), 'url': 'https://example.invalid/synthetic'}
    return ModelRegistry(tmp_path, manifest={'artifacts': [record]}), data


def test_verified_weights_cannot_be_silently_replaced(tmp_path):
    registry, data = tiny_registry(tmp_path)
    (tmp_path / 'ocr').mkdir(); path = tmp_path / 'ocr/synthetic.bin'; path.write_bytes(data)
    assert registry.verify('ocr')['synthetic.bin'] == path
    path.write_bytes(b'tampered')
    with pytest.raises(ModelFailure, match='model_missing_or_changed'):
        registry.verify('ocr')
    with pytest.raises(ModelFailure, match='model_not_registered'):
        registry.verify('unknown')


@pytest.mark.parametrize('bad', [b'wrong bytes', b'synthetic-not-a-model-extra'])
def test_download_failure_never_installs_unverified_weight(tmp_path, monkeypatch, bad):
    registry, _ = tiny_registry(tmp_path)
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **k: BytesIO(bad))
    with pytest.raises(ModelFailure):
        registry.fetch()
    assert not (tmp_path / 'ocr/synthetic.bin').exists()
    assert not list(tmp_path.rglob('.fetch-*'))


def test_missing_or_network_failure_is_explicit(tmp_path, monkeypatch):
    registry, _ = tiny_registry(tmp_path / 'missing')
    with pytest.raises(ModelFailure, match='model_missing'):
        registry.verify('ocr')
    def unavailable(*args, **kwargs):
        raise OSError('synthetic network failure')
    monkeypatch.setattr('urllib.request.urlopen', unavailable)
    with pytest.raises(ModelFailure, match='model_download_failed'):
        registry.fetch()


def observations(split='calibration'):
    return [dict(field='synthetic', group_id=f'{split}-{i}', split=split, top_cosine=score, margin=margin, correct=correct)
            for i, (score, margin, correct) in enumerate([(.6, .2, True), (.7, .25, True), (.3, .3, False), (.8, .01, False)])]


def test_field_thresholds_reject_unknown_and_never_publish_or_become_probability():
    candidates = (DiagnosticCandidate(concept_id='one', field='synthetic', text='synthetic red'),
                  DiagnosticCandidate(concept_id='two', field='synthetic', text='synthetic blue'))
    fingerprint = candidate_fingerprint(candidates, model_revision='x', preprocess_version='y')
    rules = calibrate(observations(), fingerprint=fingerprint)
    assert not rules['synthetic']['published']
    assert rank([.8, .1], candidates, top_k=2, calibration=rules, fingerprint=fingerprint)[0]['state'] == 'candidate'
    unknown = rank([.2, .19], candidates, top_k=2, calibration=rules, fingerprint=fingerprint)[0]
    assert unknown['state'] == 'no_match' and unknown['score_kind'] == 'cosine_not_probability'
    assert not unknown['effective_value_written']
    assert rank([.8, .1], candidates, top_k=2, calibration=rules, fingerprint='changed')[0]['state'] == 'uncalibrated'
    before = json.dumps(rules, sort_keys=True)
    report = evaluate(observations('evaluation'), rules, fingerprint=fingerprint)
    assert report['fields']['synthetic'] == {'samples': 4, 'accepted': 2, 'correct_accepted': 2, 'false_accepted': 0, 'rejected': 2}
    assert json.dumps(rules, sort_keys=True) == before and not report['business_acceptance']


@pytest.mark.parametrize('fault', ['evaluation', 'leakage', 'nonfinite', 'same-original', 'ambiguous'])
def test_calibration_rejects_leakage_or_reports_insufficient_evidence(fault):
    rows = observations(); rules = calibrate(rows, fingerprint='synthetic')
    if fault == 'evaluation':
        rows[0]['split'] = 'evaluation'
    elif fault == 'nonfinite':
        rows[0]['top_cosine'] = float('nan')
    elif fault == 'same-original':
        for row in rows:
            row['group_id'] = 'same-original'
        assert calibrate(rows, fingerprint='synthetic')['synthetic']['state'] == 'insufficient_samples'
        return
    elif fault == 'ambiguous':
        rows[-1].update(top_cosine=.9, margin=.9)
        assert calibrate(rows, fingerprint='synthetic')['synthetic']['state'] == 'insufficient_separation'
        return
    else:
        rows = observations('evaluation'); rows[0]['group_id'] = 'calibration-0'
        with pytest.raises(ValueError, match='leakage'):
            evaluate(rows, rules, fingerprint='synthetic')
        return
    with pytest.raises(ValueError):
        calibrate(rows, fingerprint='synthetic')


@pytest.mark.parametrize('score', [float('inf'), float('nan'), 1.1])
def test_rank_rejects_invalid_scores(score):
    with pytest.raises(ValueError):
        rank([score], [DiagnosticCandidate(concept_id='one', field='synthetic', text='synthetic')], top_k=1)


def test_calibration_cli_is_reproducible_and_does_not_overwrite(tmp_path):
    from media_tag.models.calibration import main
    observations_path, output = tmp_path / 'observations.json', tmp_path / 'rules.json'
    observations_path.write_text(json.dumps(observations()))
    args = ['--observations', str(observations_path), '--output', str(output), '--fingerprint', 'synthetic']
    assert main(args) == 0
    before = output.read_bytes()
    assert main(args) == 2 and output.read_bytes() == before
    observations_path.write_text(json.dumps(observations('evaluation')))
    assert main(args[:-3] + [str(tmp_path / 'evaluation.json'), '--fingerprint', 'synthetic', '--rules', str(output)]) == 0

"""Routing decisions only; these do not prove any real model's label accuracy."""
import pytest

from media_tag.analysis.routing import route_local_visual
from media_tag.contracts.models import FieldValue


@pytest.mark.parametrize('state', ['observed', 'unknown'])
@pytest.mark.parametrize('kind', ['image', 'video', 'animation'])
def test_no_uncalibrated_local_draft_can_silently_skip_precision_or_continuous_evidence(state, kind):
    value = FieldValue(state=state, values=('synthetic',)) if state == 'observed' else FieldValue(state=state, reason='synthetic uncertainty')
    action, reason = route_local_visual(value, kind)
    assert action == ('precision' if kind == 'image' else 'evidence')
    assert reason


@pytest.mark.parametrize('kind', ['image', 'video', 'animation'])
def test_unmapped_closed_option_goes_to_vocabulary_instead_of_automatic_adoption(kind):
    assert route_local_visual(FieldValue(state='no_match', reason='synthetic outside options'), kind)[0] == 'vocabulary'


def test_failed_inference_requires_repair_and_audio_is_not_a_visual_fallback():
    value = FieldValue(state='failed', reason='synthetic failure')
    assert route_local_visual(value, 'image')[0] == 'local'
    with pytest.raises(ValueError):
        route_local_visual(value, 'audio')
    with pytest.raises(ValueError):
        route_local_visual(FieldValue(state='not_applicable', reason='synthetic'), 'image')

from media_tag.analysis.contracts import TOPIC_FIELDS
from media_tag.models.contracts import ModelFailure
from media_tag.precision.contracts import VERSION, PrecisionOptions
from media_tag.precision.runtime import PrecisionHandlers
from media_tag.router.models import StageDefinition

AUDIO_FIELDS = tuple(field for field in TOPIC_FIELDS if field not in ('画面主体','表现形式'))


def stage():
    return StageDefinition(PrecisionOptions,'ct-local-audio-v1',gpu_allowed=True,metadata_sensitive=False,
        required_preprocess_version=VERSION,allowed_target_fields=AUDIO_FIELDS)


class AudioTopicHandlers(PrecisionHandlers):
    analysis_layer='local_audio'
    request_prefix='local-audio-'

    def __call__(self,lease,stopped):
        if lease.task.stage!='audio_topics' or lease.config.get('content_kind')!='audio':
            raise ModelFailure('local_audio_requires_actual_audio')
        return super().__call__(lease,stopped)

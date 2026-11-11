from fractions import Fraction
from hashlib import sha256
import json
from typing import Annotated, Literal

from pydantic import Field, model_validator
from media_tag.contracts.models import ContractModel, ArtifactRef, Sha256
from media_tag.analysis.contracts import AnalysisDocument


class Options(ContractModel):
    max_input_bytes: Annotated[int,Field(ge=1,le=64*1024**2)] = 32*1024**2
    max_seconds: Annotated[int,Field(ge=1,le=6)] = 3
    max_source_frames: Annotated[int,Field(ge=2,le=96)] = 48
    max_model_frames: Annotated[int,Field(ge=2,le=96)] = 64
    max_source_pixels: Annotated[int,Field(ge=1,le=4*1024**2)] = 1024**2
    max_total_source_pixels: Annotated[int,Field(ge=1,le=64*1024**2)] = 16*1024**2
    model_edge: Literal[128,256] = 128
    max_new_tokens: Annotated[int,Field(ge=64,le=384)] = 192
    max_options_characters: Annotated[int,Field(ge=256,le=12000)] = 4000
    tool_timeout_seconds: Annotated[int,Field(ge=1,le=60)] = 30
    video_stream_index: Annotated[int,Field(ge=0)] | None = None


class Rational(ContractModel):
    numerator: int
    denominator: Annotated[int,Field(ge=1)]
    @classmethod
    def of(cls,value):
        value=Fraction(value)
        return cls(numerator=value.numerator,denominator=value.denominator)
    def fraction(self):return Fraction(self.numerator,self.denominator)


class Frame(ContractModel):
    index: Annotated[int,Field(ge=0)]
    source_pts: int | None = None
    source_time: Rational
    normalized_time: Rational
    duration: Rational
    display_rgba_sha256: Sha256
    model_image: ArtifactRef


class Sequence(ContractModel):
    schema_version: Literal['ct-continuous-visual-sequence-v1'] = 'ct-continuous-visual-sequence-v1'
    source_sha256: Sha256
    source_origin: Rational
    duration: Rational
    source_time_base: Rational | None = None
    selected_video_stream: int | None = None
    coverage: Literal['complete_selected_video_stream','one_complete_animation_cycle']
    animation_plays: int | None = None
    excluded_poster_rgba_sha256: Sha256 | None = None
    frames: tuple[Frame,...]
    model_frame_sources: tuple[int,...]
    model_fps: Rational
    input_sha256: Sha256
    decoder_identity: str
    audio_policy: Literal['not_analyzed'] = 'not_analyzed'
    model_pixel_policy: Literal['white_composite_contain_square_no_frame_drop_v1'] = 'white_composite_contain_square_no_frame_drop_v1'
    processor_padding_frames: Literal[0,1] = 0
    processor_timestamp_policy: Literal['qwen3vl_pair_centers_text_0.1s_v1'] = 'qwen3vl_pair_centers_text_0.1s_v1'

    @model_validator(mode='after')
    def check_clock(self):
        if not 2<=len(self.frames)<=96 or not 2<=len(self.model_frame_sources)<=96:
            raise ValueError('bounded_continuous_frames_required')
        duration=self.duration.fraction();fps=self.model_fps.fraction()
        if duration<=0 or fps<=0:raise ValueError('positive_continuous_clock_required')
        if self.processor_padding_frames!=len(self.model_frame_sources)%2:
            raise ValueError('processor_padding_policy_mismatch')
        expected=[];position=Fraction(0)
        for index,frame in enumerate(self.frames):
            span=frame.duration.fraction()
            if (frame.index!=index or span<=0 or frame.normalized_time.fraction()!=position
                    or frame.source_time.fraction()!=self.source_origin.fraction()+position
                    or (span*fps).denominator!=1):raise ValueError('frame_clock_or_order_mismatch')
            count=int(span*fps)
            if count>96 or len(expected)+count>96:raise ValueError('model_frame_mapping_budget')
            expected.extend([index]*count);position+=span
        if position!=duration or tuple(expected)!=self.model_frame_sources:
            raise ValueError('complete_continuous_frame_mapping_required')
        if any(frame.model_image.kind!='temporal_model_frame' for frame in self.frames):
            raise ValueError('actual_temporal_frame_artifacts_required')
        if self.coverage=='complete_selected_video_stream':
            if self.source_time_base is None or self.selected_video_stream is None:
                raise ValueError('selected_video_pts_time_base_required')
            if any(frame.source_pts is None or frame.source_pts*self.source_time_base.fraction()!=frame.source_time.fraction() for frame in self.frames):
                raise ValueError('selected_video_pts_binding_mismatch')
        elif self.source_time_base is not None or self.selected_video_stream is not None or any(frame.source_pts is not None for frame in self.frames):
            raise ValueError('animation_cycle_has_no_container_pts')
        digest=sha256(json.dumps({'frames':[self.frames[index].model_image.sha256 for index in self.model_frame_sources],
            'fps':self.model_fps.model_dump(),'duration':self.duration.model_dump(),'policy':'all_frames_exact_hold_grid_v1'},
            sort_keys=True,separators=(',',':')).encode()).hexdigest()
        if digest!=self.input_sha256:raise ValueError('continuous_input_digest_mismatch')
        return self


class Generation(ContractModel):
    backend: Literal['local_qwen_video','synthetic_protocol']
    model_revision: Sha256
    prompt_version: str
    input_sha256: Sha256
    processor_identity: str | None = None
    answers: Annotated[tuple[Annotated[str,Field(max_length=16000)],...],Field(min_length=10,max_length=10)]


class Result(ContractModel):
    schema_version: Literal['ct-temporal-topic-drafts-v1'] = 'ct-temporal-topic-drafts-v1'
    document: AnalysisDocument
    sequence: Sequence
    manifest: ArtifactRef
    raw_report: ArtifactRef
    backend: Literal['local_qwen_video','synthetic_protocol']
    calibration_status: Literal['not_calibrated'] = 'not_calibrated'
    precision_exemption: Literal[False] = False
    medical_confirmation: Literal[False] = False
    privacy_confirmation: Literal[False] = False
    effective_values_written: Literal[False] = False

    @model_validator(mode='after')
    def check_evidence(self):
        expected='video' if self.sequence.coverage=='complete_selected_video_stream' else 'animation'
        if (self.document.content_sha256!=self.sequence.source_sha256 or self.document.content_kind!=expected
                or len(self.document.evidence)!=1 or self.manifest.kind!='temporal_input_manifest'
                or self.raw_report.kind!='temporal_raw_report'):
            raise ValueError('continuous_document_source_scope_mismatch')
        sample=self.document.evidence[0]
        if (sample.kind!='video_segment' or sample.artifact!=self.manifest or sample.start_seconds!=0.
                or sample.end_seconds!=float(self.sequence.duration.fraction())):
            raise ValueError('continuous_segment_evidence_mismatch')
        return self


class Pending(ValueError):
    def __init__(self,code):
        self.code=code
        super().__init__(code)

from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path

from PIL import Image
from media_tag.analysis.contracts import AnalysisDocument, TOPIC_FIELDS
from media_tag.contracts.models import ArtifactRef,Evidence,Versions
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.semantic.registry import SemanticRegistry
from .contracts import Frame,Generation,Options,Pending,Rational,Sequence,Result
from .decode import animation,video,grid,model_image
from .native import input_digest
from .normalizer import assess
from .prompt import PROMPT_VERSION,PREPROCESS_VERSION,prompts


class TemporalTopicService:
    """Bounded whole selected-stream/cycle visual evidence, no persistence to catalogue."""
    def __init__(self,workspace,*,policy,generator,tools=None,options=None):
        self.backend=FilesystemBackend(Path(workspace),writable=True)
        self.policy,self.generator,self.tools=policy,generator,tools
        self.options=options or Options()
    def store(self,data,kind):
        digest=sha256(data).hexdigest();key='obj-'+digest[:24]
        path=self.backend._resolve(key,allow_missing=True)
        try:
            with path.open('xb') as stream:stream.write(data)
        except FileExistsError:pass
        with self.backend.open_verified(key,digest,len(data)):pass
        return ArtifactRef(kind=kind,ref=key,sha256=digest)
    def run(self,source,source_key,*,expected_source_sha256,asset_id,asset_revision,analysis_id,content_kind):
        if not isinstance(source,FilesystemBackend):raise ValueError('local_source_backend_required')
        if (self.backend.root==source.root or self.backend.root in source.root.parents or source.root in self.backend.root.parents):
            raise ValueError('source_and_output_must_be_disjoint')
        if content_kind not in ('video','animation'):raise ValueError('continuous_video_or_animation_required')
        with source.open_read(source_key) as stream:raw=stream.read(self.options.max_input_bytes+1)
        if len(raw)>self.options.max_input_bytes or sha256(raw).hexdigest()!=expected_source_sha256:
            raise ValueError('source_hash_or_byte_budget')
        try:
            with (self.backend.root/'README.md').open('x',encoding='utf8') as stream:
                stream.write('Private continuous-topic evidence. Never a public preview or approved output.\n')
        except FileExistsError:pass
        source_ref=self.store(raw,'temporal_source')
        if content_kind=='animation':
            rows,metadata=animation(raw,self.options)
        else:
            if self.tools is None:raise Pending('verified_ffmpeg_toolchain_required')
            rows,metadata=video(self.backend.root/source_ref.ref,self.tools,self.options)
        step,mapping=grid([row[2] for row in rows],self.options.max_model_frames)
        fps=Rational.of(1/step);duration=Rational.of(metadata['duration'])
        frames=[];images=[]
        for index,(rgba,time,span,pts) in enumerate(rows):
            encoded=model_image(rgba,self.options.model_edge);images.append(encoded)
            frames.append(Frame(index=index,source_pts=pts,source_time=Rational.of(metadata['origin']+time),
                normalized_time=Rational.of(time),duration=Rational.of(span),display_rgba_sha256=sha256(rgba.tobytes()).hexdigest(),
                model_image=self.store(encoded,'temporal_model_frame')))
        model_frames=tuple(images[index] for index in mapping)
        digest=input_digest(model_frames,fps,duration)
        sequence=Sequence(source_sha256=expected_source_sha256,source_origin=Rational.of(metadata['origin']),
            duration=duration,source_time_base=Rational.of(metadata['time_base']) if metadata['time_base'] else None,
            selected_video_stream=metadata['stream'],coverage=metadata['coverage'],animation_plays=metadata['plays'],
            excluded_poster_rgba_sha256=metadata['poster'],frames=tuple(frames),model_frame_sources=mapping,
            model_fps=fps,input_sha256=digest,decoder_identity=metadata['decoder'],processor_padding_frames=len(mapping)%2)
        manifest=self.store(json.dumps({'sequence':sequence.model_dump(mode='json'),
            'source':source_ref.model_dump(mode='json')},sort_keys=True).encode(),'temporal_input_manifest')
        questions=prompts(self.policy,self.options)
        generation=self.generator(frame_bytes=model_frames,fps=fps,duration=duration,input_sha256=digest,
            field_prompts=questions,options=self.options)
        if (not isinstance(generation,Generation) or generation.input_sha256!=digest
                or generation.prompt_version!=PROMPT_VERSION or generation.model_revision!=self.generator.model_revision):
            raise ValueError('temporal_generation_binding')
        if generation.backend=='local_qwen_video' and (generation.model_revision!=SemanticRegistry(Path('.')).revision
                or generation.processor_identity is None):
            raise ValueError('temporal_selected_local_model_binding')
        versions=Versions(rules_version=self.policy.digest,preprocess_version=PREPROCESS_VERSION,
            taxonomy_version=self.policy.config.taxonomy_version,model_revision=generation.model_revision)
        evidence_id='continuous_visual_segment'
        fields=tuple(assess(answer,field,policy=self.policy,versions=versions,evidence_id=evidence_id)
            for field,answer in zip(TOPIC_FIELDS,generation.answers,strict=True))
        evidence=Evidence(evidence_id=evidence_id,kind='video_segment',source_sha256=expected_source_sha256,
            artifact=manifest,start_seconds=0.,end_seconds=float(duration.fraction()))
        document=AnalysisDocument(analysis_id=analysis_id,asset_id=asset_id,asset_revision=asset_revision,
            content_sha256=expected_source_sha256,content_kind=content_kind,fields=fields,evidence=(evidence,))
        report={'schema_version':'ct-temporal-raw-v1','source':source_ref.model_dump(mode='json'),
            'manifest':manifest.model_dump(mode='json'),'generation':generation.model_dump(mode='json'),
            'prompt_version':PROMPT_VERSION,'policy_digest':self.policy.digest,'versions':versions.model_dump(mode='json'),
            'questions':questions,'options':self.options.model_dump(mode='json'),
            'audio_policy':'not_analyzed','medical_confirmation':False,'privacy_confirmation':False,'effective_values_written':False}
        encoded=json.dumps(report,ensure_ascii=False,sort_keys=True).encode()
        if len(encoded)>1024*1024:raise ValueError('temporal_report_budget')
        raw_report=self.store(encoded,'temporal_raw_report')
        return Result(document=document,sequence=sequence,manifest=manifest,raw_report=raw_report,backend=generation.backend)

from datetime import datetime,timezone
from hashlib import sha256
from io import BytesIO
import json
from types import SimpleNamespace
import wave

from PIL import Image
import pytest
from media_tag.analysis.contracts import AnalysisDocument,AnalysisProducer,FieldAssessment,TOPIC_FIELDS
from media_tag.contracts.models import ArtifactRef,Evidence,FieldValue,Versions
from media_tag.topic_evaluation import Dataset,Export


@pytest.fixture
def frozen(tmp_path):
    root=tmp_path/'synthetic-bundle';root.mkdir();(root/'README.md').write_text('Explicit synthetic protocol only.')
    output=tmp_path/'synthetic-report';output.mkdir()
    versions=Versions(rules_version='synthetic-rules-v1',preprocess_version='synthetic-pixels-v1',
        model_revision='synthetic-model-protocol-not-inference-v1')
    cases=[];predictions=[]
    def artifact(name,data,kind):
        (root/name).write_bytes(data)
        return ArtifactRef(kind=kind,ref=name,sha256=sha256(data).hexdigest())
    def add(case_id='visual',kind='image',split='acceptance',group=None,*,states=None,values=None,elapsed=1.):
        states,values=states or {},values or {}
        data=BytesIO()
        if kind=='audio':
            with wave.open(data,'wb') as wav:
                wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(8000);wav.writeframes(b'\0\0'*800)
        elif kind=='animation':
            Image.new('RGB',(2,2),'red').save(data,format='PNG',save_all=True,
                append_images=[Image.new('RGB',(2,2),'blue')],duration=[100,100],loop=0)
        else:
            # Distinct actual source bytes for independently grouped test cases.
            Image.new('RGB',(2,2),(len(cases)*31%255,20,30)).save(data,format='PNG')
        content=data.getvalue();digest=sha256(content).hexdigest()
        source_ref=artifact(case_id+'-source.bin',content,'synthetic_media')
        eid='synthetic-evidence-'+case_id
        evidence=Evidence(evidence_id=eid,kind='audio_segment' if kind=='audio' else 'video_segment' if kind=='animation' else 'image',
            source_sha256=digest,artifact=source_ref,start_seconds=0. if kind!='image' else None,
            end_seconds=.1 if kind=='audio' else .2 if kind=='animation' else None)
        raw_report=artifact(case_id+'-producer.json',json.dumps({'synthetic_protocol':True,'model_executed':False,
            'source_sha256':digest}).encode(),'synthetic_producer_report')
        pid='synthetic-producer-'+case_id
        producer=AnalysisProducer(producer_id=pid,task_id='synthetic-task-'+case_id,stage='synthetic_protocol',lease_epoch=1,
            source_sha256=digest,input_fingerprint=sha256(b'synthetic-input').hexdigest(),result_sha256=sha256(b'synthetic-result').hexdigest(),
            report=raw_report,versions=versions,prompt_version='synthetic-prompt-v1',evidence_ids=(eid,))
        gold=[];fields=[]
        for field in TOPIC_FIELDS:
            na=kind=='audio' and field in ('画面主体','表现形式')
            expected=('synthetic-'+field,)
            gold.append({'field':field,'state':'not_applicable' if na else 'labelled','complete_value_set':not na,
                'values':[] if na else list(expected),'annotation_note':'Explicit synthetic protocol annotation, not actual human review.'})
            state='not_applicable' if na else states.get(field,'observed')
            value=FieldValue(state='observed',values=tuple(values.get(field,expected))) if state=='observed' else FieldValue(state=state,reason='synthetic-'+state)
            fields.append(FieldAssessment(field=field,value=value,next_step='none' if na else 'human' if field in ('科室','器官/系统','疾病/症状','过程/状态') else 'precision',
                reason='synthetic-protocol-only',origin='local',versions=versions,evidence_ids=(eid,),producer_ids=(pid,),
                dynamic=kind=='animation' and field=='过程/状态'))
        case={'case_id':case_id,'source_sha256':digest,'content_kind':kind,'near_group':group or 'synthetic-group-'+case_id,
            'split':split,'annotation_reference':'synthetic fixture, no real reviewer or sample','gold':gold}
        document=AnalysisDocument(analysis_id='synthetic-analysis-'+case_id,asset_id='synthetic-asset-'+case_id,asset_revision=1,
            content_sha256=digest,content_kind=kind,fields=tuple(fields),evidence=(evidence,),producers=(producer,))
        doc_ref=artifact(case_id+'-document.json',document.model_dump_json().encode(),'topic_analysis_document')
        prediction={'case_id':case_id,'source_sha256':digest,'content_kind':kind,'document':doc_ref.model_dump(mode='json'),
            'elapsed_seconds':elapsed}
        cases.append(case);predictions.append(prediction)
        return case,prediction
    def save():
        dataset=Dataset.model_validate_json(json.dumps({'dataset_id':'synthetic-topic-dataset','purpose':'synthetic_engineering',
            'frozen_at':'2026-01-01T00:00:00Z','annotators':['synthetic-annotator'],'adjudicator':'synthetic-adjudicator',
            'independently_human_annotated':False,'annotation_reference':'Synthetic protocol only; no real annotations.',
            'runs':[{'run_id':'synthetic-local','producer_pins':[{'versions':versions.model_dump(mode='json'),
                'prompt_version':'synthetic-prompt-v1'}]}],'cases':cases}))
        raw=dataset.model_dump_json().encode();(root/'dataset.json').write_bytes(raw)
        export=Export.model_validate_json(json.dumps({'purpose':'synthetic_engineering','dataset_sha256':sha256(raw).hexdigest(),
            'run_id':'synthetic-local','exported_at':'2026-01-02T00:00:00Z','predictions':predictions}))
        (root/'export.json').write_text(export.model_dump_json(),encoding='utf8')
        return root/'dataset.json',root/'export.json'
    def rewrite_document(prediction,change):
        path=root/prediction['document']['ref'];document=json.loads(path.read_text(encoding='utf8'))
        change(document);raw=json.dumps(document,ensure_ascii=False).encode();path.write_bytes(raw)
        prediction['document']['sha256']=sha256(raw).hexdigest()
    return SimpleNamespace(root=root,output=output,versions=versions,cases=cases,predictions=predictions,
        add=add,save=save,rewrite_document=rewrite_document)

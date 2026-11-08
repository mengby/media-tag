"""Explicit, resumable-source batch -> current AI draft ledger and formal export."""
from hashlib import sha256
from io import StringIO
import csv
import json
import os
from media_tag.catalog.service import encode
from media_tag.deployment.local import write_json
from media_tag.workers.evidence import _directory
from .inventory import load_batch,csv_bytes
from .models import IntakeError


def materialize(batch,client):
    batch,inventory,progress=load_batch(batch);actor=client.editor()
    if progress.get('state')!='imported' or progress.get('actor_id')!=actor or progress.get('service_origin')!=client.origin:
        raise IntakeError('draft_batch_binding_or_import_incomplete')
    records=[];selection=[]
    for source in inventory.entries:
        if source.state!='ready':continue
        row=progress['rows'][source.key]
        detail=client.call('GET','/v1/assets/'+row['asset_id'])
        if detail['sha256']!=source.sha256 or detail['size_bytes']!=source.size_bytes:raise IntakeError('draft_source_binding_changed')
        draft=client.call('GET','/search/assets/'+row['asset_id']+'/ai-draft')
        if draft['source_sha256']!=source.sha256:raise IntakeError('draft_report_binding_changed')
        records.append({'source_key':source.key,'asset_revision':detail['revision'],**draft})
        selection.append({'asset_id':detail['asset_id'],'expected_revision':detail['revision'],'source_sha256':source.sha256})
    if len(selection)>500:raise IntakeError('draft_formal_export_selection_budget')
    fingerprint=sha256(encode(records).encode()).hexdigest()
    folder=_directory(batch,'ai-drafts-'+fingerprint[:12])
    (folder/'README.md').write_text('AI draft snapshot. assets.csv uses the unchanged 35-column original template; medical fields remain unconfirmed. ai-labels.csv / ai-drafts.jsonl preserve model observations, not approvals. quality.json explains empty cells and provenance.\n',encoding='utf8')
    exported=client.call('POST','/review/exports',payload={'request_id':'ai-drafts-'+fingerprint,'assets':selection,'include_ai_drafts':True})
    if exported['state']!='ready':raise IntakeError('draft_formal_export_unavailable')
    manifest=exported['manifest'];identifier=exported['export_id']
    for name in ('assets.csv','gaps.csv','quality.json','manifest.json'):
        raw=client.export_content(identifier,name)
        if name=='manifest.json':
            if json.loads(raw)!=manifest:raise IntakeError('draft_export_manifest_changed')
        elif sha256(raw).hexdigest()!=manifest['files'][name]['sha256'] or len(raw)!=manifest['files'][name]['size_bytes']:
            raise IntakeError('draft_export_content_changed')
        temporary=folder/(name+'.tmp');temporary.write_bytes(raw);os.replace(temporary,folder/name)
    table=list(csv.reader(StringIO((folder/'assets.csv').read_text(encoding='utf-8-sig'),newline='')))
    if len(table)!=len(records)+1 or len(table[0])!=35 or any(len(row)!=35 for row in table):raise IntakeError('draft_formal_export_contract')
    payloads={'ai-drafts.jsonl':(''.join(encode(r)+'\n' for r in records)).encode()}
    labels=[{'asset_id':r['asset_id'],'source_key':r['source_key'],'source_sha256':r['source_sha256'],**label} for r in records for label in r['labels']]
    columns=('asset_id','source_key','source_sha256','draft_id','kind','text','state','confidence','field','value','task_id','lease_epoch','model_revision','preprocess_version','report_sha256','evidence_id','frame_sha256','start_seconds')
    payloads['ai-labels.csv']=csv_bytes(labels,columns)
    for name,raw in payloads.items():
        temporary=folder/(name+'.tmp');temporary.write_bytes(raw);os.replace(temporary,folder/name)
    ready=sum(r['stages']['ocr']=='ready' and r['stages']['face']=='ready' and r['visual_features']['frames']>0 for r in records)
    result={'state':'ready' if ready==len(records) else 'partial','asset_rows':len(records),'ready_drafts':ready,
        'label_rows':len(labels),'ocr_labels':sum(l['kind']=='ocr_text' for l in labels),
        'face_detected_assets':sum(any(l.get('value')=='有-需打码' for l in r['labels']) for r in records),
        'visual_indexed_assets':sum(r['visual_features']['frames']>0 for r in records),'classification_state':'missing_published_vocabulary',
        'formal_columns':35,'export_id':identifier,'output_dir':str(folder),'snapshot_sha256':fingerprint,
        'files':{name:sha256((folder/name).read_bytes()).hexdigest() for name in (*payloads,'assets.csv','gaps.csv','quality.json','manifest.json')},
        'medical_confirmation':False,'privacy_confirmation':False,'license_status':'unverified','effective_values_written':False}
    write_json(batch,'ai-drafts-result.json',result)
    return result

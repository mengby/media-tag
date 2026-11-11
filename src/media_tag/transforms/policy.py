"""Exact output/recipe review only. No inherited approval or raw-original fallback."""

def reviewed(connection, variant):
    return review_state(connection,variant)=='approved'


def review_state(connection, variant):
    if variant['privacy_status']=='not_applicable': return 'not_applicable'
    row=connection.execute('SELECT * FROM privacy_reviews WHERE asset_id=? ORDER BY review_id DESC LIMIT 1', (variant['asset_id'],)).fetchone()
    if row and row['output_sha256']==variant['output_sha256'] and row['recipe_hash']==variant['recipe_hash']:
        return row['decision']
    return 'pending'


def has_privacy_lineage(connection, asset):
    seen=set()
    while asset['kind']=='derived':
        if asset['asset_id'] in seen: raise ValueError('cyclic variant lineage')
        seen.add(asset['asset_id'])
        variant=connection.execute('SELECT * FROM variants WHERE asset_id=?',(asset['asset_id'],)).fetchone()
        if variant is None: raise ValueError('variant lineage unavailable')
        if variant['privacy_status']=='pending': return True
        asset=connection.execute('SELECT * FROM assets WHERE asset_id=? AND deleted_at IS NULL',(asset['parent_asset_id'],)).fetchone()
        if asset is None: raise ValueError('source lineage unavailable')
    return False


def processing_contract(connection, variant):
    """Recognize the versioned processing contract without reinterpreting old video recipes."""
    import json
    from hashlib import sha256
    from media_tag.catalog.service import encode
    plan=connection.execute('SELECT spec_json FROM variant_plans WHERE plan_id=?',(variant['plan_id'],)).fetchone()
    if plan is None:raise ValueError('variant plan unavailable')
    raw=json.loads(plan[0])
    marker=raw.get('schema_version','')
    if not isinstance(marker,str) or not marker.startswith('ct-processing-variant-'):return None
    from media_tag.processing_queue.derived_contracts import ProcessingVariantContract,CONTRACT_VERSION
    contract=ProcessingVariantContract.model_validate_json(encode(raw))
    record=connection.execute('SELECT * FROM processing_derivations WHERE asset_id=?',(variant['asset_id'],)).fetchone()
    digest=sha256(encode(contract.model_dump(mode='json')).encode()).hexdigest()
    if (record is None or record['contract_version']!=CONTRACT_VERSION or record['contract_sha256']!=digest
            or variant['recipe_hash']!=digest or variant['source_sha256']!=contract.source_sha256
            or variant['output_sha256']!=contract.output_sha256 or variant['task_id']!=contract.producer_task_id
            or variant['lease_epoch']!=contract.producer_lease_epoch or record['quality_required']!=1
            or bool(record['watermark_required'])!=contract.review_requirements.watermark):
        raise ValueError('processing derivation binding unavailable')
    return contract


def processing_review_states(connection, variant):
    contract=processing_contract(connection,variant)
    if contract is None:return None
    states={}
    for dimension,required in (('quality',True),('watermark',contract.review_requirements.watermark)):
        if not required:
            states[dimension]='not_applicable';continue
        row=connection.execute('SELECT * FROM processing_output_reviews WHERE asset_id=? AND dimension=? ORDER BY review_id DESC LIMIT 1',
            (variant['asset_id'],dimension)).fetchone()
        states[dimension]=(row['decision'] if row and row['output_sha256']==variant['output_sha256'] and
            row['recipe_hash']==variant['recipe_hash'] and row['contract_version']==contract.schema_version else 'pending')
    return states


def has_reviewed_face_processing(connection, asset):
    """Actual registered face transformation in a currently approved, intact lineage.

    A privacy review, manual label or identical blob alone never proves masking.
    Processing proofs were derived from the bound actual core report at registration;
    legacy video needs a mask recipe plus the real transform producer/report contract.
    """
    import json
    from media_tag.contracts.models import TaskEnvelope,WorkerResult,validate_result_for_task
    found,seen=False,set()
    try:
        while asset['kind']=='derived':
            if asset['asset_id'] in seen:return False
            seen.add(asset['asset_id'])
            variant=connection.execute('SELECT * FROM variants WHERE asset_id=?',(asset['asset_id'],)).fetchone()
            if variant is None or variant['output_sha256']!=asset['blob_sha256'] or review_state(connection,variant) not in ('approved','not_applicable'):return False
            states=processing_review_states(connection,variant)
            if states and any(value not in ('approved','not_applicable') for value in states.values()):return False
            execution=connection.execute('''SELECT t.active,t.state,t.lease_epoch,a.envelope_json,a.result_json,a.released_at,j.cancelled
                FROM router_tasks t JOIN router_jobs j USING(job_id) JOIN router_attempts a USING(task_id)
                WHERE t.task_id=? AND a.lease_epoch=?''',(variant['task_id'],variant['lease_epoch'])).fetchone()
            if (execution is None or not execution['active'] or execution['state']!='succeeded' or execution['cancelled']
                    or execution['lease_epoch']!=variant['lease_epoch'] or execution['released_at'] is None):return False
            task=TaskEnvelope.model_validate_json(execution['envelope_json']);result=WorkerResult.model_validate_json(execution['result_json'])
            validate_result_for_task(task,result)
            if result.status!='succeeded' or task.input_refs[0].sha256!=variant['source_sha256'] or not any(ref.sha256==variant['output_sha256'] for ref in result.artifacts):return False
            contract=processing_contract(connection,variant)
            if contract:
                proof=contract.face_processing
                if (proof.applied and any(ref.kind=='processing_core_report' and ref.sha256==proof.core_report_sha256 for ref in result.artifacts)
                        and any(ref.kind=='processing_output' and ref.sha256==variant['output_sha256'] for ref in result.artifacts)):
                    found=True
            else:
                raw=json.loads(connection.execute('SELECT spec_json FROM variant_plans WHERE plan_id=?',(variant['plan_id'],)).fetchone()[0])
                recipe=raw['spec']['recipe']
                report_kind={'redaction_render':'redaction_coverage','retime_render':'retime_report'}.get(task.stage)
                if recipe.get('mask') and report_kind and any(ref.kind==report_kind for ref in result.artifacts):found=True
            parent=connection.execute('SELECT * FROM assets WHERE asset_id=? AND deleted_at IS NULL',(asset['parent_asset_id'],)).fetchone()
            if parent is None or parent['blob_sha256']!=variant['source_sha256']:return False
            asset=parent
        return found
    except (ValueError,KeyError,TypeError):return False

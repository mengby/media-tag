"""Metadata fences shared by suggestion inspection and atomic human decisions."""
from dataclasses import dataclass
from hashlib import sha256
from collections.abc import Mapping
from types import MappingProxyType
import math

from media_tag.catalog import NotFound, RevisionConflict
from media_tag.catalog.service import encode
from media_tag.contracts.models import TagSuggestion, TaskEnvelope, WorkerResult


@dataclass(frozen=True)
class QualifiedMetadata:
    suggestion: TagSuggestion
    task: TaskEnvelope
    result: WorkerResult
    live: dict
    binding: str
    human_priority: str | None


def metadata(reviews, connection, asset, run_id, suggestion_id):
    saved=connection.execute('''SELECT s.payload,r.task_json,r.result_json FROM tag_suggestions s
        JOIN suggestion_runs r USING(run_id) WHERE s.asset_id=? AND s.run_id=? AND s.suggestion_id=?''',
        (asset['asset_id'],run_id,suggestion_id)).fetchone()
    if saved is None:raise NotFound('suggestion unavailable')
    task=TaskEnvelope.model_validate_json(saved['task_json'])
    result=WorkerResult.model_validate_json(saved['result_json'])
    suggestion=TagSuggestion.model_validate_json(saved['payload'])
    live=connection.execute('''SELECT t.*,t.rowid AS task_order,a.envelope_json,a.result_json,a.released_at,
        j.cancelled,j.paused FROM router_tasks t JOIN router_jobs j USING(job_id)
        LEFT JOIN router_attempts a ON a.task_id=t.task_id AND a.lease_epoch=t.lease_epoch
        WHERE t.task_id=?''',(task.task_id,)).fetchone()
    if (task.asset_id!=asset['asset_id'] or task.input_refs[0].sha256!=asset['blob_sha256']
            or live is None or not live['active'] or live['state']!='succeeded' or live['cancelled'] or live['paused']
            or live['lease_epoch']!=task.lease_epoch or live['released_at'] is None
            or not live['envelope_json'] or TaskEnvelope.model_validate_json(live['envelope_json'])!=task
            or not live['result_json'] or WorkerResult.model_validate_json(live['result_json'])!=result
            or suggestion not in result.suggestions or suggestion.field not in task.target_fields):
        raise RevisionConflict('suggestion producer or source is no longer current')
    # Include cancelled and unfinished current scopes: neither permits an older
    # successful producer of the same field to become eligible again.
    from media_tag.analysis.scopes import latest_topic_fields
    winner=latest_topic_fields(connection,asset_id=asset['asset_id']).get(suggestion.field)
    if winner is None:
        # Non-topic and legacy custom stages retain their exact current field
        # selection; production topic stages use the shared global scope rule.
        latest=connection.execute('''SELECT t.task_id FROM router_tasks t JOIN router_jobs j USING(job_id)
            WHERE j.asset_id=? AND t.active=1 AND EXISTS
            (SELECT 1 FROM json_each(t.spec_json,'$.target_fields') f WHERE f.value=?)
            ORDER BY t.rowid DESC LIMIT 1''',(asset['asset_id'],suggestion.field)).fetchone()
        winner=latest[0] if latest else None
    if winner!=task.task_id:
        raise RevisionConflict('suggestion is superseded by a newer field scope')
    if reviews.files._gate(connection,asset):raise RevisionConflict('suggestion is privacy or producer gated')
    effective=connection.execute('SELECT payload FROM effective_values WHERE asset_id=? AND field=?',
        (asset['asset_id'],suggestion.field)).fetchone()
    rejected=[row[0] for row in connection.execute('''SELECT payload_json FROM review_decisions
        WHERE asset_id=? AND decision='reject' AND json_extract(payload_json,'$.run_id')=?
        AND json_extract(payload_json,'$.suggestion_id')=? ORDER BY decision_id''',
        (asset['asset_id'],run_id,suggestion_id))]
    priority='human_effective_value' if effective else 'human_rejected_suggestion' if rejected else None
    definition=reviews.router.stages.get(task.stage)
    if definition is None:raise RevisionConflict('suggestion stage is no longer configured')
    if task.stage in ('precision','local_topics','audio_topics') and (
            definition.required_model_revision is not None and definition.required_model_revision!=task.versions.model_revision
            or definition.required_preprocess_version is not None and definition.required_preprocess_version!=task.versions.preprocess_version):
        raise RevisionConflict('suggestion model or preprocessing is no longer configured')
    binding=sha256(encode([dict(live),saved['payload'],saved['task_json'],saved['result_json'],
        asset['revision'],asset['blob_sha256'],reviews.policy.digest,
        getattr(definition,'implementation_version',None),effective[0] if effective else None,rejected]).encode()).hexdigest()
    return QualifiedMetadata(suggestion,task,result,dict(live),binding,priority)


def verify(reviews, item, asset) -> Mapping[str, tuple[tuple[float, float], ...]]:
    """Return declared dynamic evidence ranges only after report verification."""
    from media_tag.models.contracts import ModelFailure
    try:
        return _verify(reviews,item,asset)
    except ModelFailure:
        # Model/probe integrity failures are stale review inputs, never an
        # unhandled model exception or an implicit approval of old suggestions.
        raise RevisionConflict('suggestion report evidence is unavailable') from None


def _verify(reviews, item, asset):
    if item.task.stage in ('precision','audio_topics'):
        from media_tag.precision.projection import verified
        from media_tag.precision.contracts import PrecisionOptions
        import json
        options=PrecisionOptions.model_validate_json(json.dumps(json.loads(item.live['spec_json'])['config']))
        task,result,report,_=verified(reviews.router,item.live,asset,options.content_kind,
            layer='local_audio' if item.task.stage=='audio_topics' else 'precision')
        document=report.document
    elif item.task.stage=='local_topics':
        from media_tag.local_topics.projection import verified
        task,result,report,_=verified(reviews.router,item.live,asset)
        document=report.result.document
    else:
        reviews.router._artifacts(item.task,item.result)
        return MappingProxyType({})
    if task!=item.task or result!=item.result:raise RevisionConflict('qualified suggestion attempt changed')
    evidence={sample.evidence_id:sample for sample in document.evidence}
    requirements={}
    for field in document.fields:
        directional=field.field=='过程/状态' and any('→' in value or '->' in value
            for value in (*(field.value.values or ()),*field.candidates))
        if not field.dynamic and not directional:continue
        ranges=tuple((evidence[key].start_seconds,evidence[key].end_seconds) for key in field.evidence_ids
            if evidence[key].kind=='video_segment')
        if field.value.state=='observed' and not ranges:
            raise RevisionConflict('dynamic suggestion lacks declared continuous evidence ranges')
        requirements[field.field]=ranges
    return MappingProxyType(requirements)


def covers_dynamic_ranges(observations, actor_id, required):
    """Cover each actual declared interval; adjoining views may join, gaps may not."""
    if not required:return False
    actual=[]
    for item in observations:
        if item['kind']!='video_segment' or not item['viewed'] or item['actor_id']!=actor_id:continue
        start,end=item.get('start_seconds'),item.get('end_seconds')
        if (type(start) in (int,float) and type(end) in (int,float)
                and math.isfinite(start) and math.isfinite(end) and start<end):actual.append((start,end))
    actual.sort()
    for start,end in required:
        covered=start
        for left,right in actual:
            if right<=covered:continue
            if left>covered:break
            covered=max(covered,right)
            if covered>=end:break
        if covered<end:return False
    return True

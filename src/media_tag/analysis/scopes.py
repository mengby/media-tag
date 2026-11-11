"""Shared newest-field scope selection; qualification remains producer-specific."""
import json

from .contracts import TOPIC_FIELDS

TOPIC_STAGES = ('visual_fields', 'local_topics', 'audio_topics', 'precision')


def latest_topic_fields(connection, *, asset_id=None, job_id=None, rows=None):
    if rows is None:
        if (asset_id is None) == (job_id is None):
            raise ValueError('one topic scope required')
        predicate = 'j.asset_id=?' if asset_id is not None else 't.job_id=?'
        rows = connection.execute('''SELECT t.task_id,t.spec_json FROM router_tasks t
            JOIN router_jobs j ON j.job_id=t.job_id WHERE t.active=1 AND '''+predicate+'''
            AND json_extract(t.spec_json,'$.stage') IN ('visual_fields','local_topics','audio_topics','precision')
            ORDER BY t.rowid DESC''', (asset_id if asset_id is not None else job_id,)).fetchall()
    else:
        rows = sorted(rows, key=lambda row: row['task_order'], reverse=True)
    winners = {}
    for row in rows:
        spec = json.loads(row['spec_json'])
        if spec['stage'] in TOPIC_STAGES:
            for field in spec.get('target_fields', ()):
                if field in TOPIC_FIELDS:
                    winners.setdefault(field, row['task_id'])
    return winners

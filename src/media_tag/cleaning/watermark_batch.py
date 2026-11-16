"""Reuse watermark observations only for the leased image and its verified parents."""
from media_tag.analysis.probe import semantic_content_kind
from media_tag.contracts.models import ArtifactRef
from media_tag.models.contracts import FrameInput
from media_tag.router.batching import StageBatch, cache_key
from media_tag.router.models import StepSpec
from .pretag import STAGE, one_report


class WatermarkBatch(StageBatch):
    def __init__(self, router, size=32):
        super().__init__(router, size, STAGE)

    def _is_still(self, refs):
        return semantic_content_kind(one_report(self.router.files, refs, 'technical_report')) == 'image'

    def binding(self, task, frames):
        if len(frames) != 1 or self.size <= 1:
            return None
        if not self._is_still(task.input_refs):
            return None
        with self.router.database.transaction() as conn:
            row = conn.execute('SELECT spec_json FROM router_tasks WHERE task_id=? AND lease_epoch=? AND active=1',
                               (task.task_id, task.lease_epoch)).fetchone()
        if row is None:
            return None
        spec = StepSpec.model_validate_json(row['spec_json'])
        key = cache_key(task.task_id, task.lease_epoch, task.input_refs[0].sha256, spec, task.input_refs[1:])
        return key, spec

    def read_image(self, row, source, refs, index):
        if source['kind'] != 'original' or not self._is_still(refs):
            return None
        manifest = one_report(self.router.files, refs, 'evidence_manifest')
        if manifest['status'] != 'succeeded' or len(manifest['frames']) != 1:
            return None
        sample = manifest['frames'][0]
        artifact = ArtifactRef(**{k: sample['artifact'][k] for k in ('kind', 'ref', 'sha256')})
        if artifact not in refs or sample['source_sha256'] != source['blob_sha256']:
            return None
        frame = FrameInput(artifact=artifact, source_sha256=source['blob_sha256'],
                           normalized_seconds=sample['normalized_seconds'])
        return {'frame': frame.model_dump(mode='json'), 'source_sha256': frame.source_sha256}

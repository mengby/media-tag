"""Prepare verified still images for local topic inference."""
from hashlib import sha256

from media_tag.router.batching import StageBatch
from .service import prepare_image


class TopicBatch(StageBatch):
    def __init__(self, router, size):
        super().__init__(router, size, 'local_topics')

    def candidates(self, task, spec, options, folder, check):
        def read_item(row, source, refs, index):
            digest = source['blob_sha256']
            source_ref = f"jobs/{row['job_id']}/inputs/objects/{digest[:2]}/{digest}"
            with self.router.files.open_read(source_ref) as stream:
                raw = stream.read(options.max_input_bytes + 1)
            if len(raw) > options.max_input_bytes or sha256(raw).hexdigest() != digest:
                return None
            image = prepare_image(raw, options)
            path = folder / f'candidate-{index}.png'
            path.write_bytes(image)
            return {'image_path': str(path), 'image_sha256': sha256(image).hexdigest()}

        return self.collect(task, spec, read_item, check)

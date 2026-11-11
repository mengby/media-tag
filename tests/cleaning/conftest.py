from types import SimpleNamespace

import pytest

from upload.conftest import tools
from media_tag.cleaning import CleaningService, CleaningRecipe
from media_tag.workers.probe import file_sha256


@pytest.fixture
def clean(tmp_path, tools):
    originals = tmp_path/'originals'
    originals.mkdir()
    (originals/'README.md').write_text('Synthetic original files; service reads only.\n')
    service = CleaningService(tmp_path/'w', tools)
    def recipe(path, kind, **kwargs):
        return CleaningRecipe(source_sha256=file_sha256(path), content_kind=kind, **kwargs)
    def video(name='synthetic.mp4', audio=False, second_audio=False):
        target = originals/name
        args = ['-v', 'error', '-f', 'lavfi', '-i', 'color=c=red:s=32x24:r=10:d=4',
                '-f', 'lavfi', '-i', 'color=c=blue:s=32x24:r=10:d=4']
        if audio:
            args += ['-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=8']
        if second_audio:
            args += ['-f', 'lavfi', '-i', 'sine=frequency=880:sample_rate=48000:duration=8']
        args += ['-filter_complex', '[0:v][1:v]concat=n=2:v=1:a=0[v]', '-map', '[v]']
        if audio:
            args += ['-map', '2:a', '-c:a', 'aac']
        if second_audio:
            args += ['-map', '3:a']
        args += ['-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(target)]
        tools.ffmpeg_run(args, timeout=20.0)
        return target
    return SimpleNamespace(originals=originals, service=service, tools=tools, recipe=recipe, video=video)

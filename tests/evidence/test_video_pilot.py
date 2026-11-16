from hashlib import sha256

from PIL import Image
import pytest

from media_tag.video_pilot.__main__ import request_peak
from media_tag.video_pilot.api import endpoint, validate_tags
from media_tag.video_pilot.sampling import plan_windows, prepare


def test_plan_keeps_sequences_inside_selected_shots():
    shots = [(i * 10, (i + 1) * 10) for i in range(9)]
    selected, windows = plan_windows(shots, [(12, 1), (53, 8)])
    assert len(selected) == len(set(selected)) == 4
    assert (selected[0], selected[-1]) == (0, 8)
    assert len(windows) == 5
    for shot, start, end in windows:
        assert shot in selected
        assert shots[shot][0] <= start < end <= shots[shot][1]
        assert end - start <= 3


def test_local_sequences_have_ordered_source_clocks_and_no_cross_cut_frames(tmp_path, media_tools):
    source = tmp_path / 'cut.mp4'
    media_tools.ffmpeg_run([
        '-v', 'error', '-f', 'lavfi', '-i', 'color=black:size=64x48:rate=10:d=1.6',
        '-f', 'lavfi', '-i', 'color=white:size=64x48:rate=10:d=1.6',
        '-filter_complex', '[0:v][1:v]concat=n=2:v=1:a=0[out]', '-map', '[out]',
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(source),
    ], timeout=30)
    original = sha256(source.read_bytes()).hexdigest()
    out = tmp_path / 'result'
    manifest = prepare(source, out, 'shots', media_tools)
    assert manifest['source_sha256'] == sha256(source.read_bytes()).hexdigest() == original
    assert len(manifest['shots']) == 2
    sequences = [e for e in manifest['evidence'] if e['kind'] == 'sequence']
    assert len(sequences) == 2
    for sequence in sequences:
        start, end = manifest['shots'][sequence['shot'] - 1]
        times = sequence['frame_seconds']
        assert len(times) >= 2 and times == sorted(set(times))
        assert start <= times[0] < times[-1] < end
        path = out / sequence['path']
        assert sha256(path.read_bytes()).hexdigest() == sequence['sha256']
        with Image.open(path) as sheet:
            assert sheet.format == 'JPEG'
            value = sheet.convert('L').getpixel((60, 60))
            assert value < 10 if start == 0 else value > 245
    baseline = prepare(source, tmp_path / 'baseline', 'three', media_tools)
    assert [e['kind'] for e in baseline['evidence']] == ['image'] * 3
    with pytest.raises(FileExistsError):
        prepare(source, out, 'shots', media_tools)


def test_tunnel_evidence_references_and_actual_overlap():
    assert endpoint('http://127.0.0.1:18080/v1/') == 'http://127.0.0.1:18080/v1'
    with pytest.raises(ValueError):
        endpoint('https://example.com/v1')
    tags = dict(subjects=['鱼'], form='特写', keywords=[], actions=[dict(text='横移', evidence=['C2'])])
    with pytest.raises(ValueError, match='invalid action evidence'):
        validate_tags(tags, [dict(id='F1')])
    assert validate_tags(tags, [dict(id='C2')]) == tags
    tags['actions'][0]['evidence'] = ['C2_1244s', 'C2 12.44s']
    validate_tags(tags, [dict(id='C2', frame_seconds=[12.44])])
    assert tags['actions'][0]['evidence'] == ['C2']
    tags['actions'][0]['evidence'] = ['C2_1999s']
    with pytest.raises(ValueError, match='invalid action evidence'):
        validate_tags(tags, [dict(id='C2', frame_seconds=[12.44])])
    assert request_peak([dict(ok=True, request_start=0, request_end=2),
                         dict(ok=True, request_start=1, request_end=3),
                         dict(ok=True, request_start=2, request_end=4),
                         dict(ok=False)]) == 2

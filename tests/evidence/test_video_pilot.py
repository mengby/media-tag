import base64
from hashlib import sha256
import json

from PIL import Image
import pytest
import httpx

from media_tag.video_pilot.__main__ import request_peak, run
from media_tag.video_pilot.api import endpoint, infer, parse_tag_lines, parse_tag_reply, validate_tags, visual_content
from media_tag.video_pilot.sampling import plan_stills, plan_windows, prepare
from media_tag.video_pilot.tags import ACTION, BASE_FIELDS, VERSION, bind_actions, load_policy
from media_tag.workers.probe import Toolchain


def test_reply_wrapper_preserves_single_complete_document():
    tags = dict.fromkeys(BASE_FIELDS) | {'画面主体': '主体', ACTION: []}
    doc = json.dumps(tags)
    assert parse_tag_reply(doc + '```') == tags
    assert parse_tag_reply('```json\n' + doc + '\n```') == tags
    for invalid in (doc + doc, doc[:-1], doc + ' unrelated explanation'):
        with pytest.raises(ValueError):
            parse_tag_reply(invalid)


def test_retry_format_requires_completion_and_unique_named_fields():
    lines = '画面主体|"器械"\n画面动作|[]\nend'
    assert parse_tag_lines(lines) == {'画面主体': '器械', ACTION: []}
    for invalid in (lines[:-3], lines.replace('end', 'unrelated\nend'),
                    lines.replace('end', '画面主体|"动画"\nend'), lines.replace('[]', 'not JSON')):
        with pytest.raises(ValueError):
            parse_tag_lines(invalid)


@pytest.mark.parametrize('reason,first', [
    ('length', json.dumps(dict.fromkeys(BASE_FIELDS) | {ACTION: []})),
    ('stop', '{"subjects":[],"form":"other","actions":[],"keywords":[]}'),
    ('stop', json.dumps(dict.fromkeys(BASE_FIELDS))),
])
def test_retry_requires_all_image_fields_and_actions(tmp_path, monkeypatch, reason, first):
    tags = dict.fromkeys(BASE_FIELDS) | {'画面主体': '人物', '表现形式': '实拍', ACTION: []}
    lines = '\n'.join(f'{name}|{json.dumps(value)}' for name, value in tags.items()) + '\nend'
    replies = iter([(reason, first), ('stop', lines)])
    def respond(request):
        reason, content = next(replies)
        return httpx.Response(200, json={'choices': [{'message': {'content': content}, 'finish_reason': reason}]})
    client = httpx.Client(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: client)
    result = infer(tmp_path, {'evidence': []}, 'http://127.0.0.1:18080/v1', 'models', 5,
                   policy=load_policy('asset-index-template'))
    assert result['model_calls'] == 2
    assert result['tags'] == tags
    assert (tmp_path / 'reply-first.txt').read_text(encoding='utf8') == first


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


def test_still_budget_covers_single_takes_and_cuts():
    long_take = plan_stills([(0, 29)])
    assert len(long_take) == 8
    times = [t for _, t in long_take]
    assert times == sorted(set(times)) and times[0] == 0 and times[-1] > 25
    shots = [(i, i + 1) for i in range(12)]
    selected = plan_stills(shots)
    assert len(set(i for i, _ in selected)) == 8
    assert selected[0][0] == 0 and selected[-1][0] == 11
    assert all(shots[i][0] <= t < shots[i][1] for i, t in selected)


def test_coverage_deduplicates_unchanged_frames_and_keeps_cut_clocks(tmp_path, media_tools):
    source = tmp_path / 'cut.mp4'
    media_tools.ffmpeg_run([
        '-v', 'error', '-f', 'lavfi', '-i', 'color=black:size=64x48:rate=5:d=5',
        '-f', 'lavfi', '-i', 'color=white:size=64x48:rate=5:d=5',
        '-filter_complex', '[0:v][1:v]concat=n=2:v=1:a=0[out]', '-map', '[out]',
        '-c:v', 'libx264', '-output_ts_offset', '3', str(source),
    ], timeout=30)
    out = tmp_path / 'coverage'
    evidence = prepare(source, out, 'coverage', media_tools)['evidence']
    stills = [e for e in evidence if e['kind'] == 'image']
    assert [e['shot'] for e in stills] == [1, 2]
    assert stills[0]['start'] == 0 and 5 <= stills[1]['start'] < 10
    for e in stills:
        with Image.open(out / e['path']) as frame:
            assert frame.convert('L').getpixel((0, 0)) == (0 if e['shot'] == 1 else 255)
    assert {e['shot'] for e in evidence if e['kind'] == 'sequence'} == {1, 2}


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


@pytest.mark.parametrize('method', ['three', 'shots', 'coverage'])
@pytest.mark.parametrize('source_name', ['vfr', 'offset'])
def test_parallel_decode_preserves_evidence(tmp_path, generated_media, media_tools, method, source_name):
    source = getattr(generated_media, source_name)
    reference = prepare(source, tmp_path / 'single', method, media_tools)
    parallel = prepare(source, tmp_path / 'parallel', method, Toolchain(decode_threads=8))
    assert parallel == reference  # Includes source clocks, shot bounds and hashes of the JPEGs.


def test_still_sheet_sends_one_grid_and_preserves_action_evidence(tmp_path):
    entries = []
    originals = []
    for index, (ident, kind, color) in enumerate([
        ('F1', 'image', 'red'), ('F2', 'image', 'green'), ('C1', 'sequence', 'blue'),
    ]):
        path = tmp_path / f'{ident}.jpg'
        Image.new('RGB', (640, 320), color).save(path)
        raw = path.read_bytes()
        originals.append(raw)
        entries.append(dict(id=ident, kind=kind, path=path.name, bytes=len(raw),
                            sha256=sha256(raw).hexdigest(), start=index, end=index))

    def payloads(content):
        return [base64.b64decode(part['image_url']['url'].split(',')[1])
                for part in content if part['type'] == 'image_url']

    manifest = {'evidence': entries}
    separate, _, count = visual_content(tmp_path, manifest)
    assert count == 3 and payloads(separate) == originals
    manifest['still_sheet_edge'] = 320
    packed, sent_bytes, count = visual_content(tmp_path, manifest)
    sheet, sequence = payloads(packed)
    assert count == 2 and sent_bytes == len(sheet) + len(sequence) and sequence == originals[2]
    assert sheet == (tmp_path / 'still-sheet.jpg').read_bytes()
    mapping = json.loads((tmp_path / 'still-sheet.json').read_bytes())
    assert mapping['sha256'] == sha256(sheet).hexdigest()
    assert [c['source_sha256'] for c in mapping['cells']] == [e['sha256'] for e in entries[:2]]
    assert [(c['id'], c['seconds']) for c in mapping['cells']] == [('F1', 0), ('F2', 1)]
    with Image.open(tmp_path / 'still-sheet.jpg') as image:
        assert image.width == 320 and image.height > 320
        assert image.getpixel((160, 80))[0] > 240
        assert image.getpixel((160, image.height // 2 + 80))[1] > 110
    (tmp_path / 'F2.jpg').write_bytes(b'x' * len(originals[1]))
    with pytest.raises(ValueError, match='evidence changed'):
        visual_content(tmp_path, manifest)


def test_smaller_separate_stills_keep_clocks_and_action_sheets(tmp_path, media_tools):
    source = tmp_path / 'moving.mp4'
    media_tools.ffmpeg_run(['-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=640x360:rate=8:d=2',
                            '-c:v', 'libx264', str(source)], timeout=30)
    full = prepare(source, tmp_path / 'original', 'coverage', media_tools, still_edge=640)
    folder = tmp_path / 'small'
    small = prepare(source, folder, 'coverage', media_tools)
    assert small['still_edge'] == 320 and 'still_sheet_edge' not in small
    assert [e['id'] for e in small['evidence']] == [e['id'] for e in full['evidence']]
    for new, old in zip(small['evidence'], full['evidence']):
        if new['kind'] == 'image':
            assert (new['start'], new['shot']) == (old['start'], old['shot'])
            with Image.open(folder / new['path']) as frame:
                assert frame.size == (320, 180)
        else:
            assert new == old


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


def test_tag_written_from_sampled_frames(tmp_path, monkeypatch, generated_media):
    from media_tag.publish.video_draft import VideoDraft
    policy = load_policy('asset-index-template')
    source = generated_media.video
    out = tmp_path / 'batch'
    action = {'text': '图形移动', 'evidence': [{'id': 'C1', 'first_frame': 1, 'last_frame': 2}]}

    def respond(request):
        messages = json.loads(request.content)['messages']
        assert all(name in messages[0]['content'] for name in (*BASE_FIELDS, ACTION))
        assert any(part['type'] == 'image_url' for part in messages[1]['content'])
        tags = dict.fromkeys(BASE_FIELDS) | {'画面主体': '图形', '英文检索词': ['moving shapes'],
            '科室': '未发布分类', '口语化表达': '图形在动', ACTION: [action]}
        choice = {'message': {'content': json.dumps(tags)}, 'finish_reason': 'stop'}
        return httpx.Response(200, json={'choices': [choice]})

    client = httpx.Client(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: client)
    summary = run([source], out, 'coverage', 2, 'http://127.0.0.1:18080/v1', 'test', 5, policy=policy)
    assert summary['completed'] == 1, summary['results']
    folder = out / 'v01'
    report = json.loads((folder / 'tags.json').read_bytes())
    draft = VideoDraft(folder, sha256(source.read_bytes()).hexdigest(), 'test')
    clip = next(e for e in draft.manifest['evidence'] if e['id'] == 'C1')
    assert report['schema_version'] == VERSION and set(report['tags']) == {*BASE_FIELDS, ACTION}
    assert report['tags']['科室'] is None and report['candidates']['科室'] == ['未发布分类']
    assert report['tags']['口语化表达'] == '图形在动' and report['tags']['英文检索词'] == 'moving shapes'
    assert draft.actions[0]['evidence'] == [dict(action['evidence'][0], start_seconds=clip['frame_seconds'][0],
        end_seconds=clip['frame_seconds'][1], sha256=sha256((folder / clip['path']).read_bytes()).hexdigest())]


def test_disjoint_intervals_and_invalid_citations():
    entries = [dict(id='C1', kind='sequence', start=1, end=2, frame_seconds=[1, 1.5, 2], sha256='a'*64),
               dict(id='C2', kind='sequence', start=8, end=9, frame_seconds=[8, 8.5, 9], sha256='b'*64),
               dict(id='F1', kind='image')]
    cite = dict(id='C1', first_frame=1, last_frame=2)
    def bind(*citations):
        return bind_actions([dict(text='转动', evidence=list(citations))], entries)
    bound = bind(cite, dict(cite, id='C2', first_frame=2, last_frame=3))
    assert [(s['start_seconds'], s['end_seconds']) for s in bound[0]['evidence']] == [(1, 1.5), (8.5, 9)]
    for invalid in (dict(cite, id='F1'), dict(cite, id='C9'), dict(cite, first_frame=2),
                    dict(cite, last_frame=4), dict(cite, first_frame=True)):
        with pytest.raises(ValueError):
            bind(invalid)
    bound[0]['evidence'][0]['end_seconds'] = 8.5
    with pytest.raises(ValueError, match='differs from manifest'):
        bind_actions(bound, entries, stored=True)

"""Real synthetic intake/transactions, not actual semantic model acceptance."""

from hashlib import sha256
from io import BytesIO
import math
import sqlite3
import struct
from urllib.parse import unquote
import wave

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from media_tag.analysis.intake import ensure_intent
from media_tag.analysis.service import AnalysisService
from media_tag.catalog import Principal
from media_tag.files.http import disposition
from media_tag.files.service import FileService
from media_tag.upload.media import upload_name_parts
from media_tag.upload.models import UploadSpec

from .conftest import append, upload
from .test_api import app, headers


@pytest.mark.parametrize('extension,declared,codec,label', [
    ('.mov', '.mp4', 'mpeg4', '视频'), ('.mp4', '.mov', 'mpeg4', '视频'),
    ('.webm', '.mkv', 'libvpx-vp9', '视频'), ('.mkv', '.webm', 'mpeg4', '视频'),
    ('.m4a', '.mp4', 'aac', '音频'),
])
def test_upload_uses_actual_container_for_name_and_download(intake, tmp_path, extension, declared, codec, label):
    env = intake
    target = tmp_path / ('synthetic' + extension)
    source = 'sine=frequency=440:sample_rate=8000' if label == '音频' else 'color=c=red:s=32x24:r=4'
    # Relative fragment offsets stay valid when a legal padding box precedes ftyp.
    mux_options = ['-movflags', '+frag_keyframe+empty_moov+default_base_moof'] if extension == '.mp4' else []
    env.uploads.tools.ffmpeg_run(['-v', 'error', '-f', 'lavfi', '-i', source, '-t', '0.25',
                                  '-c', codec, *mux_options, str(target)], timeout=20)
    data = target.read_bytes()
    if extension == '.mp4':
        data = struct.pack('>I4s', 8, b'free') + data
    digest = sha256(data).hexdigest()
    spec = UploadSpec(request_id='container-family', filename='private-name' + declared,
                      size_bytes=len(data), sha256=digest)
    session = env.uploads.create(spec, principal=env.owner)
    for offset in range(0, len(data), session['chunk_bytes']):
        append(env, session, data[offset:offset + session['chunk_bytes']], offset)
    done = env.uploads.complete(session['upload_id'], principal=env.owner)
    asset_id = done['asset_id']
    expected = f"{label}-{asset_id.removeprefix('asset-')}{extension}"
    assert done['display_name'] == expected
    opened = FileService(env.router).open(asset_id, digest, principal=env.owner)
    try:
        header = disposition(opened.name, asset_id, opened.mime, False, opened.upload_extension)
        assert unquote(header).endswith("UTF-8''" + expected)
    finally:
        opened.close()


def test_matroska_payload_marker_is_not_a_webm_doctype():
    validation = {'kind': 'video', 'raw_metadata': {'format': {'format_name': 'matroska,webm'}}}
    # EBML Void carries a convincing marker before the actual Matroska DocType.
    elements = b'\xec\x87\x42\x82\x84webm\x42\x82\x88matroska'
    data = b'\x1a\x45\xdf\xa3' + bytes([0x80 + len(elements)]) + elements
    assert upload_name_parts(validation, data) == ('视频', '.mkv')


@pytest.mark.parametrize('kind,suffix', [('image', '.png'), ('animation', '.gif'), ('video', '.mp4'), ('audio', '.wav')])
@pytest.mark.parametrize('purpose', ['processing', 'library'])
def test_all_actual_media_kinds_and_purposes_have_persistent_analysis_intents(intake, tmp_path, kind, suffix, purpose):
    env = intake
    if kind == 'animation':
        stream = BytesIO()
        images = [Image.new('RGBA', (32, 24), color) for color in ('red', 'blue')]
        images[0].save(stream, format='GIF', save_all=True, append_images=images[1:], duration=200, loop=0)
        env.data = stream.getvalue()
    elif kind == 'video':
        output = tmp_path / 'synthetic.mp4'
        env.uploads.tools.ffmpeg_run(['-v', 'error', '-f', 'lavfi', '-i', 'color=c=red:s=32x24:r=4',
                                     '-t', '0.5', '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(output)], timeout=10.0)
        env.data = output.read_bytes()
    elif kind == 'audio':
        stream = BytesIO()
        with wave.open(stream, 'wb') as out:
            out.setnchannels(1); out.setsampwidth(2); out.setframerate(8000)
            out.writeframes(b''.join(struct.pack('<h', int(1000 * math.sin(2 * math.pi * 440 * i / 8000))) for i in range(4000)))
        env.data = stream.getvalue()
    spec = UploadSpec(request_id='actual-intake', filename='synthetic'+suffix,
                      size_bytes=len(env.data), sha256=sha256(env.data).hexdigest(), purpose=purpose)
    session = env.uploads.create(spec, principal=env.owner)
    for offset in range(0, len(env.data), env.uploads.options.chunk_bytes):
        chunk = env.data[offset:offset + env.uploads.options.chunk_bytes]
        env.uploads.append(session['upload_id'], offset, chunk, sha256(chunk).hexdigest(), principal=env.owner)
    result = env.uploads.complete(session['upload_id'], principal=env.owner)
    kind_label = {'image': '图片', 'animation': '动画', 'video': '视频', 'audio': '音频'}[kind]
    assert result['display_name'] == f"{kind_label}-{result['asset_id'].removeprefix('asset-')}{suffix}"
    assert result['filename'] == spec.filename
    state = AnalysisService(env.router).status(result['asset_id'], principal=env.owner)
    assert state['content_kind'] == kind and state['state'] == 'pending_analysis'
    assert not state['analysis_complete'] and not state['runtime_dispatched']
    assert len(state['topic_fields']) == 10
    # Processing keeps its independent fast technical job, not a dependency on tagging.
    if purpose == 'processing':
        assert len(env.router.get_job(result['job_id'], principal=env.owner)['tasks']) == 1
    assert env.uploads.complete(session['upload_id'], principal=env.owner)['asset_id'] == result['asset_id']
    with env.database.transaction() as c:
        assert c.execute('SELECT count(*) FROM analysis_intents WHERE asset_id=?', (result['asset_id'],)).fetchone()[0] == 1


def test_intent_failure_rolls_back_registration_and_upload_ack_together(intake, monkeypatch):
    env = intake
    original = ensure_intent
    tables = ('assets', 'analysis_intents', 'upload_outbox')
    with env.database.transaction() as c:
        before = {name: c.execute('SELECT count(*) FROM '+name).fetchone()[0] for name in tables}
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError('synthetic intent write failure')
    monkeypatch.setattr('media_tag.analysis.intake.ensure_intent', fail)
    with pytest.raises(sqlite3.OperationalError):
        upload(env)
    with env.database.transaction() as c:
        assert {name: c.execute('SELECT count(*) FROM '+name).fetchone()[0] for name in tables} == before
        row = c.execute('SELECT upload_id,state FROM uploads').fetchone()
        assert row['state'] == 'receiving'
    monkeypatch.setattr('media_tag.analysis.intake.ensure_intent', original)
    result = env.uploads.complete(row['upload_id'], principal=env.owner)
    assert AnalysisService(env.router).status(result['asset_id'], principal=env.owner)['state'] == 'pending_analysis'


def test_analysis_status_is_authenticated_requester_scoped_and_survives_database_reopen(intake):
    env = intake
    result = upload(env, purpose='processing')
    client = TestClient(app(env))
    path = '/analysis/assets/'+result['asset_id']
    assert client.get(path).status_code == 401
    assert client.get(path, headers=headers('worker')).status_code == 403
    assert client.get(path, headers=headers('other')).status_code == 403
    state = client.get(path, headers=headers()).json()
    assert state['state'] == 'pending_analysis'
    assert 'source_sha256' not in state and 'requested_by' not in state
    from media_tag.catalog import Database
    reopened = Database(env.database.path)
    reopened.initialize()
    with reopened.transaction() as c:
        assert c.execute('SELECT content_kind FROM analysis_intents WHERE intent_id=?', (state['intent_id'],)).fetchone()[0] == 'image'


def test_identical_bytes_do_not_merge_analysis_requests_or_owners(intake):
    env = intake
    tables = ('blobs', 'analysis_intents', 'asset_sources')
    with env.database.transaction() as c:
        before = {name: c.execute('SELECT count(*) FROM '+name).fetchone()[0] for name in tables}
    first = upload(env, request='source-one')
    second = upload(env, request='source-two')
    other = Principal(actor_id='synthetic-other')
    third = upload(env, request='source-three', principal=other)
    with env.database.transaction() as c:
        assert c.execute('SELECT count(*) FROM blobs').fetchone()[0] == before['blobs'] + 1
        assert c.execute('SELECT count(*) FROM analysis_intents').fetchone()[0] == before['analysis_intents'] + 3
        assert c.execute('SELECT count(*) FROM asset_sources').fetchone()[0] == before['asset_sources'] + 3
        owners = {row['asset_id']: row['owner_id'] for row in c.execute('SELECT asset_id,owner_id FROM analysis_intents')}
        assert owners[first['asset_id']] == owners[second['asset_id']] == env.owner.actor_id
        assert owners[third['asset_id']] == other.actor_id
    assert first['asset_id'] != second['asset_id']


def test_shared_read_upload_does_not_run_as_owner_or_transfer_edit_authority(intake):
    env = intake
    first = upload(env, request='owned-source')
    other = Principal(actor_id='synthetic-other')
    asset = env.catalog.get_asset(first['asset_id'], principal=env.owner)
    env.catalog.change_access(asset.asset_id, principal=env.owner, expected_revision=asset.revision,
                              subject_id=other.actor_id, permission='read', reason='synthetic read grant')
    service = AnalysisService(env.router)
    assert service.status(asset.asset_id, principal=other)['state'] == 'not_requested'
    repeated = upload(env, request='known-read-source', principal=other, known_asset_id=asset.asset_id)
    assert repeated['asset_id'] == asset.asset_id
    assert repeated['display_name'] == first['display_name'] == asset.name
    state = service.status(asset.asset_id, principal=other)
    assert state['state'] == 'awaiting_edit_permission' and not state['runtime_dispatched']
    assert env.catalog.get_asset(asset.asset_id, principal=env.owner).owner_id == env.owner.actor_id

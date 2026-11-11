import pytest
from router.conftest import env
from upload.conftest import tools

from media_tag.analysis.probe import semantic_content_kind,probe_kind_matches


@pytest.mark.parametrize('count,expected',[(1,'image'),(2,'animation'),(None,'animation'),(True,'animation'),(1.0,'animation'),('1','animation')])
def test_gif_requires_actual_strict_single_frame_proof(count,expected):
    info={'kind':'gif','raw_metadata':{'image_metadata':{'format':'GIF','frame_count':count}}}
    before=repr(info)
    assert semantic_content_kind(info)==expected
    assert repr(info)==before


def test_unproven_gif_and_other_modalities_are_not_reinterpreted():
    assert semantic_content_kind({'kind':'gif'})=='animation'
    assert semantic_content_kind({'kind':'gif','raw_metadata':{'image_metadata':{'format':'PNG','frame_count':1}}})=='animation'
    for kind in ('image','animation','audio','video'):assert semantic_content_kind({'kind':kind})==kind
    with pytest.raises(ValueError):semantic_content_kind({'kind':'unknown'})


def test_legacy_single_gif_match_is_explicit_not_a_static_topic_completion():
    info={'kind':'gif','raw_metadata':{'image_metadata':{'format':'GIF','frame_count':1}}}
    assert probe_kind_matches(info,'image')
    assert not probe_kind_matches(info,'animation')
    assert probe_kind_matches(info,'animation',allow_legacy_single_gif=True)


def test_unbound_legacy_intent_refines_only_from_actual_verified_single_gif(env,tools,tmp_path):
    from PIL import Image
    from media_tag.analysis.intake import ensure_intent
    from media_tag.storage.filesystem import FilesystemBackend
    from media_tag.workers.probe import probe_media,ProbeOptions
    folder=tmp_path/'actual-gif';folder.mkdir();(folder/'README.md').write_text('Synthetic single GIF only.\n')
    path=folder/'one.gif';Image.new('RGB',(16,16),'red').save(path)
    source=env.catalog.register_file(FilesystemBackend(folder),path.name,source_backend_id='synthetic-probe',principal=env.owner,request_id='actual-gif',name=path.name)
    info=probe_media(path,tools,ProbeOptions()).as_dict()
    with env.database.transaction(write=True) as connection:
        identifier=ensure_intent(connection,source.asset_id,env.owner.actor_id,content_kind='gif')
        assert connection.execute('SELECT content_kind FROM analysis_intents WHERE intent_id=?',(identifier,)).fetchone()[0]=='animation'
        ensure_intent(connection,source.asset_id,env.owner.actor_id,content_kind='gif',probe_info=info)
        assert connection.execute('SELECT content_kind FROM analysis_intents WHERE intent_id=?',(identifier,)).fetchone()[0]=='image'

from copy import deepcopy
import json
from PIL import Image
import pytest
from media_tag.transforms.models import Keyframe,TrackEdit,ScanOptions
from media_tag.transforms.redaction import mask_frame,revise
from media_tag.workers.face_track import Tracker


def observation(track='one',box=None):
    return {'track_id':track,'box':box or [.1,.1,.3,.4],'state':'detected','scene':0}


def report():
    return {'source_sha256':'a'*64,'source_display':[100,80],'source_clock':{'duration_seconds':3},'options':{},'risks':[],
            'frames':[{'index':i,'seconds':i*.5,'duration':.5,'observations':[observation()]} for i in range(6)]}


def test_tracker_crossing_movement_disappearance_and_cut_are_flagged():
    tracker=Tracker(ScanOptions()); image=Image.new('RGB',(100,80),'black')
    frame=lambda i:{'index':i,'seconds':i*.1}
    first=tracker.update(image,frame(0),[[.1,.1,.3,.3],[.5,.1,.7,.3]])
    assert len({o['track_id'] for o in first})==2
    assert len(tracker.update(image,frame(1),[[.2,.1,.4,.3],[.4,.1,.6,.3]]))==2
    held=tracker.update(image,frame(2),[])
    assert held and all(o['state']=='conservative_hold' for o in held)
    assert not tracker.update(image,frame(4),[])
    cut=tracker.update(Image.new('RGB',(100,80),'white'),frame(5),[[.1,.1,.3,.3]])
    assert cut[0]['track_id'] not in {o['track_id'] for o in first}
    assert {'scene_cut','detection_gap','rapid_motion','multiple_faces_association_uncertain'}<={r['reason'] for r in tracker.risks}


def test_split_merge_trim_exclude_and_manual_interpolation_preserve_source_report():
    original=report(); before=deepcopy(original)
    edits=[TrackEdit(action='split',track_ids=('one',),new_track_id='second',start_seconds=1.5),
           TrackEdit(action='merge',track_ids=('one','second'),new_track_id='merged'),
           TrackEdit(action='trim',track_ids=('merged',),start_seconds=.5,end_seconds=2.),
           TrackEdit(action='exclude',track_ids=('merged',)),
           TrackEdit(action='manual',new_track_id='manual',start_seconds=0.,end_seconds=3.,
                     keyframes=(Keyframe(seconds=0.,box=(.1,.1,.3,.3)),Keyframe(seconds=3.,box=(.4,.2,.8,.7))))]
    output=revise(original,edits)
    assert original==before and all(f['observations'][0]['track_id']=='manual' for f in output['frames'])
    assert output['frames'][3]['observations'][0]['box']==pytest.approx([.25,.15,.55,.5])
    assert output['excluded_tracks']==['merged'] and not output['privacy_confirmation']


@pytest.mark.parametrize('edit',[
    dict(action='exclude',track_ids=['unknown']),dict(action='merge',track_ids=['one','one'],new_track_id='new'),
    dict(action='split',track_ids=['one'],new_track_id='split',start_seconds=0.),
    dict(action='trim',track_ids=['one'],start_seconds=2.,end_seconds=1.),
    dict(action='manual',new_track_id='new',start_seconds=0.,end_seconds=2.,keyframes=[]),
    dict(action='manual',new_track_id='one',start_seconds=0.,end_seconds=2.,keyframes=[])])
def test_invalid_edits_do_not_silently_drop_masks(edit):
    with pytest.raises(ValueError): revise(report(),[TrackEdit.model_validate_json(json.dumps(edit))])


@pytest.mark.parametrize('box',[(.5,.1,.1,.2),(-.1,.1,.3,.2),(.1,.1,1.1,.3),(float('nan'),.1,.3,.4)])
def test_manual_box_requires_finite_normalized_display_coordinates(box):
    with pytest.raises(ValueError): Keyframe(seconds=0.,box=box)


@pytest.mark.parametrize('mode',['mosaic','solid'])
def test_masks_expand_clip_and_modify_exact_regions(mode):
    import numpy as np
    pixels=np.random.default_rng(1).integers(0,256,(80,100,3),dtype=np.uint8); image=Image.fromarray(pixels)
    output,regions=mask_frame(image,[observation(box=[0.,0.,.2,.2])],mode=mode,strength=20,padding=.5)
    assert regions[0]['box_xyxy']==[0,0,31,25]
    result=np.asarray(output)
    assert np.array_equal(result[30:,40:],pixels[30:,40:]) and not np.array_equal(result[:10,:10],pixels[:10,:10])
    if mode=='solid': assert result[:10,:10].max()==0

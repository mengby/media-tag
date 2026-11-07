"""Immutable track edits and display-frame masks; no privacy approval."""
from copy import deepcopy
import math
from PIL import Image
from media_tag.models.contracts import ModelFailure
from media_tag.workers.face_track import decode_frames, frame_clock
from media_tag.workers.probe import file_sha256
from .models import ScanOptions


def revise(report, edits):
    frames=deepcopy(report['frames']); exclusions=[]
    known={o['track_id'] for f in frames for o in f['observations']}
    duration=report['source_clock']['duration_seconds']
    for edit in edits:
        ids=set(edit.track_ids)
        if len(ids)!=len(edit.track_ids) or not ids<=known:
            raise ValueError('unknown or duplicated source tracks')
        if edit.action in ('exclude','trim','split') and len(ids)!=1:
            raise ValueError('one track required')
        if edit.action in ('split','merge','manual'):
            if not edit.new_track_id or edit.new_track_id in known:
                raise ValueError('a unique new track ID is required')
        if edit.action=='exclude':
            if edit.keyframes or edit.new_track_id or edit.start_seconds is not None or edit.end_seconds is not None:
                raise ValueError('irrelevant exclusion parameters')
            for f in frames: f['observations']=[o for o in f['observations'] if o['track_id'] not in ids]
            exclusions.extend(sorted(ids))
        elif edit.action in ('trim','split'):
            if edit.start_seconds is None or edit.start_seconds>duration or edit.keyframes:
                raise ValueError('valid source time required')
            if edit.action=='trim' and (edit.end_seconds is None or not edit.start_seconds<edit.end_seconds<=duration or edit.new_track_id):
                raise ValueError('valid trim interval required')
            if edit.action=='split' and edit.end_seconds is not None:
                raise ValueError('split uses only start_seconds')
            touched=[f for f in frames if any(o['track_id'] in ids for o in f['observations'])]
            if edit.action=='split' and (not any(f['seconds']<edit.start_seconds for f in touched) or not any(f['seconds']>=edit.start_seconds for f in touched)):
                raise ValueError('split must leave both parts nonempty')
            for f in frames:
                if edit.action=='trim' and not (edit.start_seconds<=f['seconds']<edit.end_seconds):
                    f['observations']=[o for o in f['observations'] if o['track_id'] not in ids]
                elif edit.action=='split' and f['seconds']>=edit.start_seconds:
                    for o in f['observations']:
                        if o['track_id'] in ids: o['track_id']=edit.new_track_id
            if edit.action=='split': known.add(edit.new_track_id)
        elif edit.action=='merge':
            if len(ids)<2 or edit.keyframes or edit.start_seconds is not None or edit.end_seconds is not None:
                raise ValueError('two nonoverlapping tracks required')
            if any(sum(o['track_id'] in ids for o in f['observations'])>1 for f in frames):
                raise ValueError('overlapping tracks cannot be merged into one face')
            for f in frames:
                for o in f['observations']:
                    if o['track_id'] in ids: o['track_id']=edit.new_track_id
            known.add(edit.new_track_id)
        else:
            if ids or edit.start_seconds is None or edit.end_seconds is None or not 0<=edit.start_seconds<edit.end_seconds<=duration or len(edit.keyframes)<2:
                raise ValueError('manual track needs a bounded interval and endpoint keyframes')
            keys=edit.keyframes
            if keys[0].seconds!=edit.start_seconds or keys[-1].seconds!=edit.end_seconds or any(b.seconds<=a.seconds for a,b in zip(keys,keys[1:])):
                raise ValueError('ordered keyframes must cover the exact manual interval')
            for f in frames:
                if f['seconds']>=edit.end_seconds or f['seconds']+f['duration']<=edit.start_seconds: continue
                t=max(edit.start_seconds,f['seconds'])
                a,b=next((a,b) for a,b in zip(keys,keys[1:]) if a.seconds<=t<=b.seconds)
                fraction=(t-a.seconds)/(b.seconds-a.seconds)
                box=[x+(y-x)*fraction for x,y in zip(a.box,b.box)]
                f['observations'].append({'track_id':edit.new_track_id,'box':box,'state':'manual_interpolated','scene':None})
            known.add(edit.new_track_id)
    return {'schema_version':'d10-mask-revision-v1','source_sha256':report['source_sha256'],
            'source_display':report['source_display'],'source_clock':report['source_clock'],'frames':frames,
            'options':report['options'],'edits':[e.model_dump() for e in edits],'excluded_tracks':exclusions,'risks':report['risks'],
            'privacy_confirmation':False}


def pixel_box(box, size, padding):
    x1,y1,x2,y2=box; width,height=size
    px=(x2-x1)*padding; py=(y2-y1)*padding
    return (max(0,math.floor((x1-px)*width)),max(0,math.floor((y1-py)*height)),
            min(width,math.ceil((x2+px)*width)),min(height,math.ceil((y2+py)*height)))


def mask_frame(image, observations, *, mode, strength, padding):
    image=image.convert('RGB').copy(); regions=[]
    for observation in observations:
        box=pixel_box(observation['box'],image.size,padding)
        x1,y1,x2,y2=box
        if x2<=x1 or y2<=y1: raise ModelFailure('empty_mask')
        if mode=='solid':
            image.paste((0,0,0),box)
        else:
            block=max(2,strength)
            tile=image.crop(box)
            tile=tile.resize((max(1,(x2-x1)//block),max(1,(y2-y1)//block)),Image.Resampling.BOX)
            image.paste(tile.resize((x2-x1,y2-y1),Image.Resampling.NEAREST),box)
        regions.append({'track_id':observation['track_id'],'box_xyxy':list(box),'state':observation['state']})
    return image,regions


def render(path, output, tools, revision, contract, render_parameters):
    options=ScanOptions.model_validate_json(__import__('json').dumps(revision['options']))
    if contract['time_map']['clock']['duration_seconds']>options.max_seconds:
        raise ModelFailure('duration_budget')
    origin=revision['source_clock']['origin_seconds']; index=contract['source_video_stream']
    end=origin+revision['source_clock']['duration_seconds']
    frames=frame_clock(path,tools,index,origin=origin,video_end=end,options=options)
    expected=revision['frames']
    if [(f['source_pts'],f['seconds'],f['duration']) for f in frames]!=[(f['source_pts'],f['seconds'],f['duration']) for f in expected]:
        raise ModelFailure('scan_timeline_changed')
    paths=decode_frames(path,output/'frames',tools,index,frames,contract['source_display'],options)
    covered=[]
    for frame,original,image_path in zip(frames,expected,paths):
        if file_sha256(image_path)!=original['decoded_sha256']:
            raise ModelFailure('scan_pixels_changed')
        with Image.open(image_path) as image:
            masked,regions=mask_frame(image,original['observations'],**render_parameters)
        masked.save(image_path,'PNG')
        covered.append({'frame':frame['index'],'seconds':frame['seconds'],'duration':frame['duration'],
                        'source_pts':frame['source_pts'],'regions':regions,'masked_frame_sha256':file_sha256(image_path)})
    from .retime import encode_video
    speed=contract['time_map']['speed']; offset=contract['stream_offsets'][str(index)]
    mapped=[dict(frame,seconds=frame['seconds']+offset) for frame in frames]
    encoded=encode_video(path,output,tools,contract,mapped,frame_paths=paths,filename='redacted.mp4')
    for frame in covered:
        frame['output_seconds']=(frame['seconds']+offset)/speed
        frame['output_duration']=frame['duration']/speed
    risks=[dict(r,output_seconds=(r['seconds']+offset)/speed) for r in revision['risks']]
    return encoded | {'schema_version':'d11-redaction-coverage-v1','frames':covered,'all_source_frames_rendered':True,
        'parameters':render_parameters,'risks':risks,'no_automatic_privacy_approval':True,
        'scope':'face_masks_only_not_names_or_voices'}

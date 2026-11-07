"""Complete, bounded visual decoding and per-frame YuNet detection. No identity recognition."""
import math

from PIL import Image
from media_tag.models.contracts import FaceOptions, ModelFailure
from media_tag.models.inference import FaceModel
from media_tag.workers.probe import file_sha256


def frame_clock(path, tools, video_index, *, origin, video_end, options):
    if video_end-origin>options.max_seconds+.001:
        raise ModelFailure('duration_budget')
    data=tools.probe_run(path,timeout=120,extra=['-select_streams',str(video_index),'-show_frames','-show_entries',
        'frame=best_effort_timestamp_time,pts,best_effort_timestamp,duration_time,width,height'])
    rows=data.get('frames',[])
    if not rows or len(rows)>options.max_frames:
        raise ModelFailure('frame_budget')
    times=[float(r['best_effort_timestamp_time'])-origin for r in rows]
    if any(not math.isfinite(t) or t < -.000001 for t in times) or any(b<=a for a,b in zip(times,times[1:])):
        raise ModelFailure('invalid_frame_clock')
    durations=[b-a for a,b in zip(times,times[1:])] + [video_end-origin-times[-1]]
    if durations[-1]<=0:
        raise ModelFailure('inconsistent_stream_extent')
    if times[-1]+durations[-1]>options.max_seconds+.001:
        raise ModelFailure('duration_budget')
    return [{'index':i,'seconds':max(0.,t),'source_seconds':origin+t,'duration':duration,'source_pts':r.get('pts',r.get('best_effort_timestamp'))}
            for i,(r,t,duration) in enumerate(zip(rows,times,durations))]


def decode_frames(path, folder, tools, video_index, frames, size, options):
    width,height=size
    if width*height>options.max_pixels or width*height*len(frames)>options.max_total_pixels:
        raise ModelFailure('pixel_budget')
    metadata=tools.probe_run(path,timeout=20,extra=['-select_streams',str(video_index),'-show_entries','stream=sample_aspect_ratio'])['streams'][0]
    if metadata.get('sample_aspect_ratio') not in (None,'N/A','0:1','1:1'):
        raise ModelFailure('unsupported_pixel_aspect_ratio')
    folder.mkdir(); (folder/'README.md').write_text('Private unredacted display-oriented frames. Never a public preview.\n')
    tools.ffmpeg_run(['-v','error','-xerror','-protocol_whitelist','file,pipe','-i',str(path),'-map',f'0:{video_index}',
        '-an','-sn','-dn','-fps_mode','passthrough','-frames:v',str(options.max_frames+1),str(folder/'frame-%06d.png')],timeout=120)
    paths=sorted(folder.glob('frame-*.png'))
    if len(paths)!=len(frames):
        raise ModelFailure('decoded_frame_count_mismatch')
    for p in paths:
        with Image.open(p) as image:
            if list(image.size)!=list(size):
                raise ModelFailure('dynamic_or_unexpected_dimensions')
            image.verify()
    return paths


def iou(a,b):
    intersection=max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))
    area=lambda r:(r[2]-r[0])*(r[3]-r[1])
    return intersection/max(1e-12,area(a)+area(b)-intersection)


class Tracker:
    def __init__(self, options):
        self.options=options; self.active={}; self.next_id=1; self.scene=0; self.previous=None; self.risks=[]

    def update(self, image, frame, detections, scores=None):
        import numpy as np
        pixels=np.asarray(image.convert('RGB').resize((48,48))).astype(float)/255
        cut=self.previous is not None and float(np.abs(pixels-self.previous).mean())>=self.options.cut_threshold
        self.previous=pixels
        if cut:
            self.active.clear(); self.scene+=1
            self.risks.append({'frame':frame['index'],'seconds':frame['seconds'],'reason':'scene_cut'})
        matches=[]; edges=[]
        for index,box in enumerate(detections):
            for track,state in self.active.items():
                similarity=iou(box,state['box'])
                if similarity>.1:
                    edges.append((similarity,track,index))
        used_tracks=set(); used_boxes=set()
        for score,track,index in sorted(edges,reverse=True):
            if track in used_tracks or index in used_boxes: continue
            used_tracks.add(track); used_boxes.add(index); matches.append((track,index))
        for index in range(len(detections)):
            if index not in used_boxes:
                track=f'track-{self.next_id:06d}'; self.next_id+=1; matches.append((track,index))
                self.risks.append({'frame':frame['index'],'seconds':frame['seconds'],'reason':'new_track','track_id':track})
        observations=[]
        for track,index in matches:
            box=detections[index]
            prior=self.active.get(track)
            if prior:
                displacement=max(abs(a-b) for a,b in zip(box,prior['box']))
                if displacement>.08:
                    self.risks.append({'frame':frame['index'],'seconds':frame['seconds'],'reason':'rapid_motion','track_id':track})
                if len(detections)>1:
                    self.risks.append({'frame':frame['index'],'seconds':frame['seconds'],'reason':'multiple_faces_association_uncertain','track_id':track})
            score=scores[index] if scores is not None else None
            self.active[track]={'box':box,'last':frame['seconds'],'score':score}
            observations.append({'track_id':track,'box':box,'detection_score':score,'state':'detected','scene':self.scene})
        for track,state in list(self.active.items()):
            if any(t==track for t,_ in matches): continue
            gap=frame['seconds']-state['last']
            self.risks.append({'frame':frame['index'],'seconds':frame['seconds'],'reason':'detection_gap','track_id':track})
            if gap<=self.options.max_gap_seconds:
                observations.append({'track_id':track,'box':state['box'],'detection_score':None,'last_detection_score':state['score'],
                                     'state':'conservative_hold','scene':self.scene})
            else:
                del self.active[track]
        return observations


def scan(path, output, tools, registry, contract, options):
    origin=contract['time_map']['clock']['origin_seconds']; index=contract['source_video_stream']
    end=origin+contract['stream_offsets'][str(index)]+contract['stream_durations'][str(index)]
    frames=frame_clock(path,tools,index,origin=origin,video_end=end,options=options)
    paths=decode_frames(path,output/'frames',tools,index,frames,contract['source_display'],options)
    model=FaceModel(registry); tracker=Tracker(options); width,height=contract['source_display']
    observations_count=0
    for frame,image_path in zip(frames,paths):
        with Image.open(image_path) as image:
            result=model.infer(image,FaceOptions(score_threshold=options.score_threshold))
            detections=[[v/width if i%2==0 else v/height for i,v in enumerate(face['box_xyxy'])] for face in result['faces']]
            frame['observations']=tracker.update(image,frame,detections,[f['detection_score'] for f in result['faces']])
            frame['detections']=result['faces']; frame['decoded_sha256']=file_sha256(image_path)
            observations_count+=len(frame['observations'])
            if observations_count>50000 or len(tracker.risks)>50000:
                raise ModelFailure('tracking_report_budget')
    return {'schema_version':'d10-scan-v1','source_sha256':file_sha256(path),'model_revision':registry.revision,
        'source_display':contract['source_display'],'source_clock':contract['time_map']['clock'], 'frames':frames,
        'options':options.model_dump(),'risks':tracker.risks,'all_decoded_frames_observed':True,
        'frame_count':len(frames),'identity_inference':False,'privacy_confirmation':False,'medical_confirmation':False,
        'tracking_policy':'greedy_iou_current_box_no_smoothing_short_hold_reset_on_cut'}

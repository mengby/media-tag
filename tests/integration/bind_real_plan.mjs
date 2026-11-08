// Bind a real reviewed still use to the new task. Never sets medical or full AV approval.
import fs from 'node:fs/promises';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {isDeepStrictEqual} from 'node:util';
import {bindRhythmPlan} from 'file:///D:/ai/auto-video/editor/server/rhythm-design.mjs';
import {buildRhythmLedger} from 'file:///D:/ai/auto-video/editor/server/rhythm-events.mjs';
import {avBinding,candidateBinding} from 'file:///D:/ai/auto-video/editor/server/av-design.mjs';
import {completionBinding} from 'file:///D:/ai/auto-video/editor/server/material-completion.mjs';
const batch=path.resolve(process.argv[2]);
const read=async p=>JSON.parse(await fs.readFile(p,'utf8'));
const encode=v=>JSON.stringify(v,null,2)+'\n',sha=v=>createHash('sha256').update(v).digest('hex');
const state=await read(path.join(batch,'state.json')),edit=path.join(state.task,'edit'),run=path.join(edit,'automation/runs/d20');
let plan=await read(path.join(edit,'preparation.json'));
plan.evidence_inputs.script=path.resolve(edit,plan.evidence_inputs.script);
const candidate=await read(path.join(run,'candidate.json')),context=await read(path.join(batch,'current-context.json'));
const reviewed=await read(path.join(run,'candidate-reviewed.json'));for(const item of reviewed.media??[])delete item.candidate;
if(!isDeepStrictEqual(reviewed,candidate))throw Error('Candidate changed beyond the accepted flag');
const execution=await read(path.join(edit,'editorial-execution.json'));
const result=await read(path.join(batch,'codex/answer.json'));
if(result.verdict!=='pass'||result.medical_approved||result.continuous_video_viewed||result.audio_listened)throw Error('Unexpected actual review scope');
const reason='D20本次实际Codex像素检查通过：只在首句展示真实西柚静帧，不作药理证据；其余时间保留既定医生画面。';
const request={id:'d20-u04-fruit',query_zh:'西柚',query_en:'grapefruit',purpose:'对应首句水果名称，限静态外观示意'};
const unit=plan.units.find(u=>u.id==='u04');unit.presentation={kind:'image',reason,fallback:'doctor'};unit.asset_requests=[request];
const story=plan.storyboard.find(u=>u.unit_id==='u04');story.kind='image';story.visual='首句西柚静帧与圆形医生窗，随后返回医生';story.choice_reason=reason;story.alternative='无法验收则保留医生原画面';
story.av_design.shots[0].visual=story.visual;
const material=plan.material_plan.find(u=>u.unit_id==='u04');Object.assign(material,{status:'candidate',reason,requests:[request],
 search_record:'automation/runs/d20/api-search.json',fallback:'doctor',source_acceptance:'automation/runs/d20/material-acceptance.json',candidate_review:'automation/runs/d20/model-review.json'});
const rhythm=plan.rhythm_plan.find(u=>u.unit_id==='u04');rhythm.reason=reason;rhythm.action='首句素材进入，按原词界返回医生';
rhythm.actions=[{id:'u04-d20-fruit-enter',kind:'event',anchor:{word:unit.word_start,edge:'start'},window:{start:unit.start,end:23.248},
 expected:{type:'enter',collection:'media',id:'d20-grapefruit'},purpose:request.purpose,fallback:'doctor'}];
plan=bindRhythmPlan(plan,context);
const ledger=buildRhythmLedger(candidate,{plan});
const event=ledger.events.find(e=>e.type==='enter'&&e.ref?.collection==='media'&&e.ref.id==='d20-grapefruit');
if(!event)throw Error('No actual source entry event');
const visual={kind:'visual',status:'passed',evidence:'实际Codex审看源图和候选静帧；记录见automation/runs/d20/model-review.json。仅静态范围。'};
const row=execution.shots.find(u=>u.unit_id==='u04');Object.assign(row,{actual_kind:'image',reason,rhythm_result:rhythm.action,elements:[{collection:'media',index:0,id:'d20-grapefruit'}],
 rhythm_execution:[{action_id:rhythm.actions[0].id,status:'implemented',reason,event_ids:[event.id],checks:[visual]}]});
Object.assign(row.av_execution[0],{reason,layout_result:'原图等比显示，圆形医生窗、完整字幕/压字及用药提示可见。',elements:row.elements,checks:[{...visual,status:'pass'},{kind:'audio',status:'pending',evidence:'尚未试听，沿用原人声，静态素材没有音轨。'}]});
execution.rhythm_binding=ledger.binding;
execution.rough_review={plan_sha256:avBinding(plan),candidate_sha256:candidateBinding(candidate),checks:[
 {kind:'materials',status:'pass',evidence:'当前唯一新素材已完成真实静态检查、来源/哈希与本地隐私处理验证。'},
 {kind:'layout',status:'pending',evidence:'已检查变更窗口入场/展示/退场静帧；整片其他窗口沿原输入，未重新完整观看。'},
 {kind:'continuity',status:'pending',evidence:'未作整片连续观看，保持原时间轴与顺序。'},
 {kind:'sound',status:'pending',evidence:'未作整片试听，源静帧无音频，保持既有人声。'}]};
const completion={version:1,output_binding:completionBinding(plan),scope:'D20显式新任务选择复核，非医学或全片视听通过',units:plan.units.map(u=>({
 unit_id:u.id,source:u.id==='u04'?'retrieved':['diagram','evidence'].includes(u.presentation.kind)?'template':'doctor',
 reason:u.id==='u04'?reason:'复用用户选定任务的既有时序与呈现；本轮没有为此单元新增素材，不继承旧医学/动态批准。',
 ai:{status:'not_needed',reason:u.id==='u04'?'既有真实来源可用，未调用AI生成':'本轮沿用该单元既定医生/模板呈现，不新增生成内容'},
 files:u.id==='u04'?[{path:candidate.media[0].src,sha256:state.asset_sha256}]:[],
 ...(u.id==='u04'?{checks:{file:'passed',content:'passed',privacy:'passed'}}:{})}))};
const raw=encode(completion);await fs.writeFile(path.join(run,'material-completion.json'),raw);
plan.material_completion={version:1,path:'automation/runs/d20/material-completion.json',sha256:sha(raw)};
await fs.writeFile(path.join(edit,'preparation.json'),encode(plan));
await fs.writeFile(path.join(edit,'editorial-execution.json'),encode(execution));
await fs.writeFile(path.join(run,'rhythm-events.json'),encode(ledger));
console.log('Bound current plan, completion and actual ledger; full AV and medicine remain pending.');

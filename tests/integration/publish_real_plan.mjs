// Publish only the actually accepted candidate after the repository's full package gate.
// This is an explicit session handoff, not a simulated AI worker or medical approval.
import fs from 'node:fs/promises';
import path from 'node:path';
import http from 'node:http';
import {execFile} from 'node:child_process';
import {promisify,isDeepStrictEqual} from 'node:util';
import {createApi} from 'file:///D:/ai/auto-video/editor/server/api.mjs';
import {atomic,encode,digest} from 'file:///D:/ai/auto-video/editor/server/versions.mjs';
import {acquirePipelineLease} from 'file:///D:/ai/auto-video/editor/server/pipeline-lease.mjs';
const batch=path.resolve(process.argv[2]),read=async p=>JSON.parse(await fs.readFile(p,'utf8'));
const state=await read(path.join(batch,'state.json')),root='D:\\ai\\auto-video',edit=path.join(state.task,'edit');
const run=path.join(edit,'automation/runs/d20'),candidate=await read(path.join(run,'candidate.json'));
const reviewed=await read(path.join(run,'candidate-reviewed.json'));
for(const item of reviewed.media??[])delete item.candidate;
if(!isDeepStrictEqual(candidate,reviewed))throw Error('Accepted candidate changed after actual review');
const proof=await read(path.join(batch,'codex/verified.json'));
if(proof.actual_host!=='Codex CLI'||!proof.api_mcp_equal||proof.medical_approved)throw Error('Actual host proof required');
for(const [file,expected] of Object.entries(proof.input_hashes)){
 const actual=file===path.join(run,'candidate.json')?path.join(run,'candidate-reviewed.json'):file;
 if(digest(await fs.readFile(actual))!==expected)throw Error('Reviewed input changed');
}
await promisify(execFile)('node',[path.join(root,'scripts/check_stage.mjs'),'package',state.target_job,
 path.join(run,'candidate.json'),path.join(edit,'editorial-execution.json')],{env:process.env,maxBuffer:1024*1024});
// Initialize this new task from the already checked, accepted artifact, then use the
// unmodified editor HTTP apply handler (including real check_edit and permissions).
const lease=await acquirePipelineLease(edit,'d20-session-handoff');
try{
 await lease.assert();
 const current=await fs.readFile(path.join(edit,'edit.json'),'utf8').catch(e=>{if(e.code==='ENOENT')return null;throw e;});
 if(current&&!isDeepStrictEqual(JSON.parse(current),candidate))throw Error('Existing task changed; refusing overwrite');
 if(!current)await atomic(path.join(edit,'edit.json'),encode(candidate));
}finally{await lease.release();}
const handler=createApi({root}),server=http.createServer(handler);
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
try{
 const url=`http://127.0.0.1:${server.address().port}/api/jobs/${state.target_job}`;
 const current=await fetch(url).then(r=>r.json());
 const response=await fetch(url+'/apply',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({version:current.version,draftVersion:current.draftVersion,edit:candidate})});
 const verdict=await response.json();
 await atomic(path.join(batch,'editor-apply.json'),encode({http_status:response.status,...verdict,medical_approved:false}));
 if(!response.ok||!verdict.ok)throw Error('Original editor apply rejected the artifact; inspect editor-apply.json');
 if(!isDeepStrictEqual(await read(path.join(edit,'edit.json')),candidate))throw Error('Applied candidate mismatch');
 state.stage='accepted_candidate_applied';state.applied_sha256=digest(await fs.readFile(path.join(edit,'edit.json')));
 await atomic(path.join(batch,'state.json'),encode(state));
 console.log(JSON.stringify({state:state.stage,http_status:response.status,medical_approved:false}));
}finally{await handler.shutdown();await new Promise(resolve=>server.close(resolve));}

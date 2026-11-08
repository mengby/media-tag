import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash,randomBytes} from 'node:crypto';
import {SHA256,digestFile} from '../../src/media_tag/web/sha256.js';
import {speedOf,makeEdit,audioTrack} from '../../src/media_tag/web/app.js';

test('SHA256 standard vectors and streaming block/padding boundaries',()=>{
  for(const input of [Buffer.alloc(0),Buffer.from('abc'),Buffer.alloc(1000000,97),...Array.from({length:75},(_,n)=>randomBytes(n)),randomBytes(5*1024*1024)]){
    const hash=new SHA256();for(let p=0;p<input.length;p+=113)hash.update(input.subarray(p,p+113));
    assert.equal(hash.hex(),createHash('sha256').update(input).digest('hex'));
    assert.throws(()=>hash.update(input));
  }
});
test('large file digest streams fixed-size slices and supports stopping',async()=>{
  const data=randomBytes(9*1024*1024+57);let slices=0;
  const file={size:data.length,slice(a,b){assert.ok(b-a<=4*1024*1024);slices++;return new Blob([data.subarray(a,b)]);}};
  assert.equal(await digestFile(file),createHash('sha256').update(data).digest('hex'));
  assert.equal(slices,3);
  await assert.rejects(digestFile(file,()=>{},()=>true));
});
test('speed defaults and explicit bounds; no inherited speed',()=>{
  assert.equal(audioTrack('drop','2',3),null);assert.equal(audioTrack('preserve_pitch','',1),null);assert.equal(audioTrack('preserve_pitch','2',3),2);assert.throws(()=>audioTrack('preserve_pitch','',3));
  assert.equal(speedOf(false,2),1);assert.equal(speedOf(true,'1.25'),1.25);
  for(const value of ['bad',Infinity,0,4.1])assert.throws(()=>speedOf(true,value));
});
test('track actions serialize only their relevant contract fields',()=>{
  assert.deepEqual(makeEdit('exclude','track-1','','','','unused'),{action:'exclude',track_ids:['track-1']});
  assert.deepEqual(makeEdit('split','track-1','new',.5,999,[]),{action:'split',track_ids:['track-1'],new_track_id:'new',start_seconds:.5});
  const keyframes=[{seconds:0,box:[.1,.1,.3,.3]},{seconds:1,box:[.2,.2,.4,.4]}];
  assert.deepEqual(makeEdit('manual','','new',0,1,keyframes).keyframes,keyframes);
});

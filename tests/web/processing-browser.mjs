import {readFile,writeFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {createHash} from 'node:crypto';
import assert from 'node:assert/strict';
const c=JSON.parse(await readFile(process.argv[2],'utf8'));
const {chromium}=createRequire(import.meta.url)(c.playwright);
const browser=await chromium.launch({headless:true,executablePath:c.chrome,args:['--disable-gpu']});
try{
 const context=await browser.newContext({viewport:{width:1440,height:1000}});
 const login=await context.request.post(c.url+'/ui/session',{headers:{Authorization:'Bearer owner',Origin:c.url}});
 assert.equal(login.status(),200);
 const page=await context.newPage();
 const errors=[];page.on('pageerror',error=>errors.push(error.message));
 await page.goto(c.url+'/ui/processing?asset='+c.asset,{waitUntil:'networkidle'});
 await page.waitForFunction(kind=>kind==='video'?
  document.querySelector('#source-media video')?.readyState>=2&&document.querySelector('#output-media video')?.readyState>=2:kind==='audio'?
  document.querySelector('#source-media audio')?.readyState>0&&document.querySelector('#output-media audio')?.readyState>0:
  document.querySelector('#source-media img')?.naturalWidth>0&&document.querySelector('#output-media img')?.naturalWidth>0,c.kind);
 assert.equal(await page.locator('#title').textContent(),c.name);
 assert.equal(await page.locator('#title img').count(),0);
 assert.equal(await page.evaluate(()=>window.__processing_xss),undefined);
 assert.equal(await page.locator('.review').count(),3);
 assert.equal(await page.locator('.review input:checked').count(),0);
 assert.equal(await page.locator('#download').isVisible(),false);
 if(c.kind==='audio'){
  assert.equal(await page.locator('#audio-note').isVisible(),true);
  assert.match(await page.locator('#audio-note').textContent(),/不表示声源隐私或许可已经批准/);
  assert.equal(await page.locator('[data-approve="privacy"]').count(),0);
 }
 if(c.kind==='video'){
  const summary=await page.locator('#summary').textContent();
  assert.match(summary,/实际源片段2 秒 ～ 6 秒/);
  assert.match(summary,/结果时长4 秒/);
  for(const selector of ['#source-media video','#output-media video']){
   await page.locator(selector).evaluate(async video=>{
    await video.play();
    await new Promise(resolve=>video.requestVideoFrameCallback(resolve));
    video.pause();
   });
  }
 }
 await page.screenshot({path:c.screenshot,fullPage:true});
 const dimensions=c.kind==='audio'?['quality']:c.kind==='video'?['quality','privacy']:['quality','privacy','watermark'];
 for(const dimension of dimensions){
  await page.waitForFunction(name=>!document.querySelector(`[data-approve="${name}"]`).disabled,dimension);
  const card=page.locator(`[data-dimension="${dimension}"]`);
  const checks=card.locator('input[type=checkbox]');
  for(let index=0;index<await checks.count();index++)await checks.nth(index).check();
  const response=page.waitForResponse(r=>r.url().endsWith('/reviews')&&r.request().method()==='POST');
  await card.locator('[data-approve]').click();
  assert.equal((await response).status(),200);
  await page.waitForFunction(name=>document.querySelector(`[data-dimension="${name}"]`)?.textContent.includes('已批准'),dimension);
  if(dimension!==dimensions.at(-1))assert.equal(await page.locator('#download').isVisible(),false);
 }
 await page.waitForFunction(()=>!document.getElementById('download').hidden);
 const href=await page.locator('#download').getAttribute('href');
 const download=await context.request.get(c.url+href);
 assert.equal(download.status(),200);
 assert.equal(createHash('sha256').update(await download.body()).digest('hex'),c.outputSha);
 if(c.kind!=='audio')assert.notEqual(c.outputSha,c.sourceSha);
 const revokedDimension=c.kind==='image'?'watermark':'quality';
 const revoke=page.waitForResponse(r=>r.url().endsWith('/reviews')&&r.request().method()==='POST');
 await page.locator(`[data-revoke="${revokedDimension}"]`).click();assert.equal((await revoke).status(),200);
 await page.waitForFunction(name=>document.querySelector(`[data-dimension="${name}"]`)?.textContent.includes('已撤销'),revokedDimension);
 assert.equal(await page.locator('#download').isVisible(),false);
 assert.equal((await context.request.get(c.url+href)).status(),409);
 await context.request.delete(c.url+'/ui/session',{headers:{Origin:c.url}});
 await page.locator('#refresh').click();
 await page.waitForFunction(()=>document.getElementById('notice').textContent.includes('会话已失效'));
 assert.equal(await page.locator('#source-media img,#source-media video,#source-media audio,#output-media img,#output-media video,#output-media audio').count(),0);
 assert.equal(await page.locator('#download').isVisible(),false);
 assert.deepEqual(errors,[]);
 await writeFile(c.result,JSON.stringify({state:'passed',actual_chromium:true,source_and_result_rendered:true,
  separate_applicable_review_decisions:true,audio_face_na_without_voice_approval:c.kind==='audio',stable_download_hash_verified:true,revocation_blocks_bytes:true,
  session_expiry_clears_media:true,xss_text_only:true,automatic_confirmation:false,business_acceptance:false}));
}finally{await browser.close();}

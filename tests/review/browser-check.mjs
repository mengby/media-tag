// Isolated D06 personnel workflow against a real local server and browser; no production approvals.
import {readFile,writeFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import path from 'node:path';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
const context=JSON.parse(await readFile(process.argv[2],'utf8'));
const {chromium}=createRequire(import.meta.url)(context.playwright);
const browser=await chromium.launch({executablePath:context.chrome,headless:true});
try{
  const session=await browser.newContext({viewport:{width:1440,height:1000}}),page=await session.newPage(),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(context.url+'/ui/');await page.waitForFunction(()=>document.documentElement.dataset.ready==='true');
  await page.locator('#credential').fill(context.token);await page.locator('#login-form').evaluate(n=>n.requestSubmit());
  await page.locator('#workspace').waitFor({state:'visible'});assert.ok(await page.locator('#credential').evaluate(n=>n.value===''));
  await page.locator('#upload-library').click();await page.locator('#upload-files').setInputFiles(context.source);
  await page.locator('#upload-form').evaluate(n=>n.requestSubmit());await page.waitForFunction(()=>document.getElementById('upload-state').textContent.includes('已登记'));
  await page.locator('#close-upload').click();await page.locator('#review-nav').click();await page.locator('#review-queue button').first().click();
  await page.waitForFunction(()=>document.querySelector('#review-media img')?.naturalWidth===96);
  const assetId=await page.locator('#review-binding').evaluate(n=>n.textContent.split(' · ')[0]);
  await page.locator('#observation-note').fill('工程验证：实际查看合成图的绿色矩形与半透明红色背景；不是医学素材结论。');
  await page.locator('#observation-viewed').check();await page.locator('#record-observation').click();
  await page.waitForFunction(()=>document.querySelectorAll('#observation-list input:checked').length===1);
  await page.locator('#review-field').selectOption('画面主体');await page.locator('#field-text').fill('绿色矩形；半透明红色背景');
  await page.locator('#field-note').fill('工程合成图逐字段记录');await page.locator('#save-field').click();
  await page.waitForFunction(()=>document.getElementById('review-binding').textContent.includes('revision 2'));
  await page.locator('#review-field').selectOption('医学准确性备注');await page.locator('#field-text').fill('=1\n合成工程输入：只验证 CSV 公式防护，不作医学陈述');
  await page.locator('#field-note').fill('工程公式单元格表示测试，原文保持不变');await page.locator('#save-field').click();
  await page.waitForFunction(()=>document.getElementById('review-binding').textContent.includes('revision 3'));
  await page.locator('#review-field').selectOption('科室');
  assert.ok(await page.locator('#save-field').isDisabled());assert.ok(await page.locator('#field-options').isDisabled());
  assert.equal(await page.locator('#field-options option').count(),0);
  assert.ok(await page.locator('#confirm-index').isDisabled());
  await page.locator('[data-page="library"]').click();await page.locator('#upload-library').click();await page.locator('#upload-files').setInputFiles(context.source_video);
  await page.locator('#upload-form').evaluate(n=>n.requestSubmit());await page.waitForFunction(()=>document.getElementById('upload-state').textContent.includes('已登记'));
  await page.locator('#close-upload').click();await page.locator('#review-nav').click();await page.getByRole('button',{name:/synthetic-review-video/}).click();
  await page.waitForFunction(()=>document.querySelector('#review-media video')?.readyState>=3);
  await page.locator('#review-media video').evaluate((n,origin)=>{n.currentTime=.25+origin;n.muted=true;return n.play();},context.video_origin||0);
  await page.waitForFunction(origin=>document.querySelector('#review-media video').currentTime>.75+origin,context.video_origin||0);await page.locator('#review-media video').evaluate(n=>n.pause());
  const videoAsset=await page.locator('#review-binding').evaluate(n=>n.textContent.split(' · ')[0]);
  await page.locator('#observation-start').fill('0.25');await page.locator('#observation-end').fill('0.75');
  await page.locator('#observation-note').fill('工程验证：实际播放当前合成视频 0.25～0.75 秒的图案运动，不作医学判断。');await page.locator('#observation-viewed').check();await page.locator('#record-observation').click();
  await page.waitForFunction(()=>document.querySelectorAll('#observation-list input:checked').length===1);
  await page.getByRole('button',{name:'播放此观察区间'}).click();await page.waitForFunction(origin=>{const v=document.querySelector('#review-media video');return v.paused&&v.currentTime>=.75+origin;},context.video_origin||0);assert.ok(await page.locator('#review-media video').evaluate((v,origin)=>Math.abs(v.currentTime-(.75+origin))<.35,context.video_origin||0));
  await page.locator('#review-field').selectOption('可用片段');await page.locator('#field-text').fill('0.25-0.75 合成图案运动');await page.locator('#field-note').fill('工程片段定位与连续播放验证');await page.locator('#save-field').click();await page.waitForFunction(()=>document.getElementById('review-binding').textContent.includes('revision 2'));
  await page.locator('.gap-panel summary').click();
  const gapDescription='工程合成测试：补充蓝色矩形示意图';
  for(let i=0;i<2;i++){
    await page.locator('[data-gap-field="需要的画面"]').fill(gapDescription);
    await page.locator('[data-gap-field="来源任务"]').fill(context.batch_id);
    await page.locator('#gap-note').fill('本次工程流程第 '+(i+1)+' 次提出同一缺口；未声称已检索或找到素材。');
    await page.locator('#gap-form').evaluate(n=>n.requestSubmit());
    await page.waitForFunction(count=>document.getElementById('gap-list').textContent.includes(count+' 次'),i+1);
  }
  assert.equal(await page.locator('#gap-list .review-queue-row').count(),1);
  for(const check of await page.locator('#review-queue input[type=checkbox]').all())await check.check();await page.locator('#gap-list input[type=checkbox]').first().check();
  await page.locator('#create-export').click();await page.waitForFunction(()=>document.querySelectorAll('#export-links a').length===4);
  const downloads={};
  for(const name of ['assets.csv','gaps.csv','quality.json','manifest.json']){
    const url=await page.locator(`#export-links a[href$="/${name}"]`).getAttribute('href');
    const response=await session.request.get(context.url+url);assert.equal(response.status(),200);
    const raw=await response.body();await writeFile(path.join(context.output,name),raw);
    downloads[name]={sha256:createHash('sha256').update(raw).digest('hex'),size_bytes:raw.length};
  }
  const manifest=JSON.parse(await readFile(path.join(context.output,'manifest.json'),'utf8'));
  const quality=JSON.parse(await readFile(path.join(context.output,'quality.json'),'utf8'));
  assert.equal(manifest.asset_columns,35);assert.equal(manifest.gap_columns,14);
  assert.ok(quality.issues.some(i=>i.code==='formula_escaped'));
  assert.ok(quality.issues.some(i=>i.code==='medical_unconfirmed'));
  await page.getByRole('button',{name:/^synthetic-review.png/}).click();await page.waitForFunction(()=>document.querySelector('#review-media img')?.naturalWidth===96);await page.locator('#review-field').selectOption('画面主体');await page.evaluate(()=>document.getElementById('notice').hidden=true);
  await page.screenshot({path:path.join(context.output,'review-desktop.png'),fullPage:true});
  await page.setViewportSize({width:390,height:844});await page.screenshot({path:path.join(context.output,'review-mobile.png'),fullPage:true});
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'mobile layout overflow');
  await page.setViewportSize({width:1440,height:1000});
  const metadata=await session.request.get(context.url+'/review/assets/'+assetId);const data=await metadata.json();
  assert.equal(data.index_status,'ai_draft');assert.equal(data.effective_values.length,2);assert.ok(data.effective_values.find(v=>v.field==='医学准确性备注').value.values[0].startsWith('=1\n'));
  const gapResponse=await session.request.get(context.url+'/review/gaps');const gap=(await gapResponse.json()).items[0];
  assert.equal(gap.occurrences,2);
  await page.locator('#logout').click();await page.locator('#login').waitFor({state:'visible'});
  assert.equal((await session.request.get(context.url+'/review/assets/'+assetId)).status(),401);
  assert.equal(await page.locator('#review-media').evaluate(n=>n.children.length),0);
  assert.deepEqual(errors,[]);
  await writeFile(path.join(context.output,'browser-result.json'),JSON.stringify({asset_id:assetId,revision:data.revision,gap_id:gap.gap_id,gap_occurrences:2,
    export_id:manifest.export_id,video_asset_id:videoAsset,continuous_interval_playback:true,video_origin:context.video_origin||0,downloads,browser_runtime_errors:errors,medical_options_blocked:true,medical_confirmation_created:false,
    mobile_no_overflow:true,original_text_preserved:true},null,2));
  console.log(JSON.stringify({passed:true,asset_revision:data.revision,columns:[35,14],gap_occurrences:2,medical_options_blocked:true}));
}finally{await browser.close();}

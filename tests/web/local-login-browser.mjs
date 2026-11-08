// Real Chromium QA in an isolated profile. No long-lived bearer is supplied to the browser.
import {readFile,writeFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import assert from 'node:assert/strict';
const config=JSON.parse(await readFile(process.argv[2],'utf8'));
const {chromium}=createRequire(import.meta.url)(config.playwright);
const browser=await chromium.launch({headless:true,executablePath:config.chrome,args:['--disable-gpu']});
try{
  const context=await browser.newContext({viewport:{width:1400,height:980}});
  const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(config.url+'/ui/');await page.waitForFunction(()=>document.documentElement.dataset.ready==='true');
  assert.equal(await page.locator('#login-form').isVisible(),false);
  assert.equal(await page.locator('#workspace').isVisible(),false);
  await page.goto('about:blank');await page.goto(config.url+'/ui/#local-login='+config.ownerTicket);
  await page.waitForFunction(()=>!document.getElementById('workspace').hidden);
  assert.equal(new URL(page.url()).hash,'');assert.equal(await page.locator('#credential').inputValue(),'');
  const session=await page.evaluate(()=>fetch('/ui/session').then(r=>r.json()));
  assert.equal(session.role,'editor');assert.equal(session.medical_review,'not_assigned');
  await page.reload();await page.waitForFunction(()=>!document.getElementById('workspace').hidden);
  const cookie=(await context.cookies()).find(c=>c.name==='media_tag_session');assert(cookie.httpOnly&&cookie.sameSite==='Strict');
  assert(await page.evaluate(()=>Object.entries(localStorage).every(([k,v])=>k.startsWith('media-tag-view:')&&['gallery','list'].includes(v))));
  await page.screenshot({path:config.screenshot,fullPage:true});
  await page.locator('#logout').click();await page.waitForFunction(()=>document.getElementById('workspace').hidden);
  const loggedOut=await page.evaluate(()=>fetch('/ui/session').then(r=>r.status));assert.equal(loggedOut,401);
  await page.goto('about:blank');await page.goto(config.url+'/ui/#local-login='+config.ownerTicket);
  await page.waitForFunction(()=>document.documentElement.dataset.ready==='true');assert.equal(await page.locator('#workspace').isVisible(),false);
  await page.goto('about:blank');await page.goto(config.url+'/ui/#local-login='+config.readerTicket);
  await page.waitForFunction(()=>!document.getElementById('workspace').hidden);
  const reader=await page.evaluate(async()=>({session:await fetch('/ui/session').then(r=>r.json()),write:await fetch('/uploads',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request_id:'reader-denied',filename:'shape.png',size_bytes:10,sha256:'0'.repeat(64)})}).then(r=>r.status)}));
  assert.equal(reader.session.role,'reader');assert.equal(reader.write,403);
  assert.equal(new URL(page.url()).hash,'');assert.equal(await page.locator('#login-form').isVisible(),false);
  assert.deepEqual(errors,[]);
  await writeFile(config.result,JSON.stringify({state:'passed',actual_chromium:true,owner_role:session.role,medical_review:session.medical_review,
    reader_role:reader.session.role,reader_write_status:reader.write,manual_token_input:false,fragment_cleared:true,reload:true,logout:true,replay_rejected:true,http_only_cookie:true}));
}finally{await browser.close();}

// Real local UI verification; reads the private credential file, never logs it.
import {readFile,writeFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import assert from 'node:assert/strict';
const config=JSON.parse(await readFile(process.argv[2],'utf8'));
const token=(await readFile(config.token_file,'utf8')).trim();
const {chromium}=createRequire(import.meta.url)(config.playwright);
const browser=await chromium.launch({headless:true,executablePath:config.chrome,args:['--disable-gpu']});
try{
  const context=await browser.newContext({baseURL:config.base_url,viewport:{width:1440,height:1080}});
  const session=await context.request.post('/ui/session',{headers:{Authorization:'Bearer '+token}});
  assert.equal(session.status(),200);
  const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto('/ui/');await page.locator('#workspace').waitFor({state:'visible'});
  assert(await page.locator('#search-drafts').isChecked());
  assert(!(await page.locator('#search-visual-drafts').isChecked()));
  const response=page.waitForResponse(r=>r.url().endsWith('/search/query')&&r.request().method()==='POST');
  await page.locator('#search').fill(config.query);assert.equal((await response).status(),200);
  const card=page.locator(`[data-asset-id="${config.asset_id}"]`);await card.waitFor({state:'visible'});await card.click();
  await page.getByRole('heading',{name:'AI 标签初稿（未确认）',exact:true}).waitFor({state:'visible'});
  assert((await page.locator('#details').innerText()).includes(config.query));
  await page.screenshot({path:config.output_dir+'/ai-draft-detail.png',fullPage:true});
  const visualResponse=page.waitForResponse(r=>r.url().endsWith('/search/query')&&r.request().postDataJSON()?.include_visual_drafts===true);
  await page.locator('#search-visual-drafts').check();const visual=await visualResponse;
  assert.equal(visual.status(),200);assert.equal((await visual.json()).visual_drafts.state,'ready_uncalibrated');
  assert.equal(await page.locator('input[type=password]').inputValue(),'');
  assert(!await page.locator('body').innerText().then(text=>text.includes(token)));
  assert.deepEqual(errors,[]);
  await writeFile(config.output_dir+'/browser-validation.json',JSON.stringify({state:'passed',default_drafts:true,explicit_visual:true,draft_detail_visible:true,ocr_hit_visible:true,no_page_errors:true,credential_not_in_dom:true}));
  console.log('AI draft browser checks passed');
}finally{await browser.close();}

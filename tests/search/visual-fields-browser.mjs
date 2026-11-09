// Read-only production UI checks; the bulk action is tested separately on synthetic fixtures.
import {readFile,writeFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import assert from 'node:assert/strict';
const config=JSON.parse(await readFile(process.argv[2],'utf8'));
const token=(await readFile(config.token_file,'utf8')).trim();
const {chromium}=createRequire(import.meta.url)(config.playwright);
const browser=await chromium.launch({headless:true,executablePath:config.chrome,args:['--disable-gpu']});
try{
  const context=await browser.newContext({baseURL:config.base_url,viewport:{width:1440,height:1080}});
  assert.equal((await context.request.post('/ui/session',{headers:{Authorization:'Bearer '+token}})).status(),200);
  const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto('/ui/');await page.locator('#workspace').waitFor({state:'visible'});
  assert(await page.locator('#search-drafts').isChecked());
  await page.locator('#auto-tag-start').waitFor({state:'visible'});
  const started=Date.now();
  const request=page.waitForRequest(r=>r.url().endsWith('/search/query')&&r.method()==='POST');
  const response=page.waitForResponse(r=>r.url().endsWith('/search/query')&&r.request().method()==='POST',{timeout:120000});
  await page.locator('#search').fill(config.query);await request;assert.equal((await response).status(),200);
  const search_seconds=(Date.now()-started)/1000;
  const card=page.locator(`[data-asset-id="${config.asset_id}"]`);await card.waitFor({state:'visible'});await card.click();
  await page.getByRole('heading',{name:'AI 标签初稿（未确认）',exact:true}).waitFor({state:'visible'});
  const text=await page.locator('#details').innerText();assert(text.includes(config.query));
  for(const field of ['画面主体','表现形式','中文检索词','英文检索词'])assert(text.includes(field+'：'));
  const draftResponse=await context.request.get('/search/assets/'+config.asset_id+'/ai-draft');
  assert.equal(draftResponse.status(),200);const draft=await draftResponse.json();
  assert.equal(draft.stages.visual_fields,'ready');
  assert.deepEqual(Object.keys(draft.field_states).sort(),['画面主体','表现形式','中文检索词','英文检索词'].sort());
  assert.equal(await page.locator('input[type=password]').inputValue(),'');
  assert(!(await page.locator('body').innerText()).includes(token));assert.deepEqual(errors,[]);
  await page.screenshot({path:config.output_dir+'/visual-fields-detail.png',fullPage:true});
  await writeFile(config.output_dir+'/visual-fields-browser.json',JSON.stringify({state:'passed',real_local_results:true,
    four_field_states_visible:true,field_query_hit_visible:true,bulk_entry_visible:true,no_page_errors:true,credential_not_in_dom:true,search_seconds}));
  console.log('visual-fields production browser checks passed');
}finally{await browser.close();}

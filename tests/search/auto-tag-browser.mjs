// Real browser -> isolated real API/queue/export; source and worker results are synthetic fixtures.
import {readFile,writeFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import assert from 'node:assert/strict';
const config=JSON.parse(await readFile(process.argv[2],'utf8'));
const {chromium}=createRequire(import.meta.url)(config.playwright);
const browser=await chromium.launch({headless:true,executablePath:config.chrome,args:['--disable-gpu']});
try{
  const context=await browser.newContext({baseURL:config.base_url,viewport:{width:1440,height:1080}});
  assert.equal((await context.request.post('/ui/session',{headers:{Authorization:'Bearer owner'}})).status(),200);
  const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto('/ui/');await page.locator('#workspace').waitFor({state:'visible'});
  const button=page.locator('#auto-tag-start');await button.waitFor({state:'visible'});
  await button.click();
  const download=page.locator('#auto-tag-state a');await download.waitFor({state:'visible',timeout:45000});
  assert((await page.locator('#auto-tag-state').innerText()).includes('1/1'));
  const url=await download.getAttribute('href'),response=await context.request.get(url);assert.equal(response.status(),200);
  const csv=await response.text();assert(csv.includes('AI初稿')&&csv.includes('抽象几何图')&&csv.includes('2D插图'));
  await button.click();await page.locator('#auto-tag-state a').waitFor({state:'visible'});
  assert.equal(await page.locator('#auto-tag-state a').getAttribute('href'),url);
  assert.deepEqual(errors,[]);
  await page.screenshot({path:config.output_dir+'/auto-tag-ui.png',fullPage:true});
  await writeFile(config.output_dir+'/auto-tag-browser.json',JSON.stringify({state:'passed',single_action_to_index_export:true,repeat_reused:true,no_page_errors:true,synthetic_worker_protocol:true}));
  console.log('auto-tag real browser/API/queue/export passed');
}finally{await browser.close();}

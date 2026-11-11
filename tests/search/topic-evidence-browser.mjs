// Actual local Chromium DOM regression; only synthetic API replies, no catalogue or models.
import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import {createRequire} from 'node:module';

const require=createRequire(import.meta.url);
const {chromium}=require(process.env.MEDIA_TAG_TEST_PLAYWRIGHT||'C:/Users/EDY/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

test('topic detail renders current content evidence, legacy frames, and absent metadata in actual DOM',async t=>{
  const server=createServer(async(req,res)=>{
    const name=({'/search.js':'search.js','/terms.js':'terms.js'})[req.url];
    if(!name){res.writeHead(404);res.end();return;}
    res.setHeader('Content-Type','text/javascript');
    res.end(await readFile(new URL('../../src/media_tag/web/'+name,import.meta.url)));
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  let browser;
  try{
    browser=await chromium.launch({headless:true,executablePath:process.env.MEDIA_TAG_TEST_CHROME||'C:/Program Files/Google/Chrome/Application/chrome.exe',args:['--disable-gpu']});
    const page=await browser.newPage(),errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    const origin='http://127.0.0.1:'+server.address().port;
    await page.goto(origin+'/search.js');
    await page.setContent('<nav aria-label="主导航"></nav><main id="workspace"></main><div id="library"><div class="toolbar"></div></div><div id="review-editor"></div><label><input id="export-ai"></label><input id="search"><section id="details"></section>');
    const labels=[
      {text:'静态十字段',field:'画面主体',evidence_kind:'image',content_evidence_sha256:'a'.repeat(64),frame_sha256:'f'.repeat(64),model_revision:'model-image',evidence_id:'image-evidence',confidence:null},
      {text:'音频八字段',field:'情绪/语气',evidence_kind:'audio_segment',content_evidence_sha256:'b'.repeat(64),model_revision:'model-audio',evidence_id:'audio-evidence',confidence:.7},
      {text:'完整内容',evidence_kind:'other',content_evidence_sha256:'c'.repeat(64),model_revision:'model-content',evidence_id:'content-evidence'},
      {text:'旧帧标签',frame_sha256:'d'.repeat(64),model_revision:'model-legacy',evidence_id:'frame-evidence',confidence:0},
      {text:'缺元数据 <img src=x onerror=alert(1)>'},
    ];
    await page.evaluate(async({origin,labels})=>{
      const {installSearchUI}=await import(origin+'/search.js');
      window.bulkFixture=null;window.bulkCalls=[];
      const ui=installSearchUI({api:async path=>path.endsWith('/concepts')?{links:[],can_edit:false}:{stages:{local_topics:'ready',audio_topics:'ready',precision:'not_requested'},labels,field_states:{表现形式:{state:'not_applicable'}},uncertainties:[]},
        post:async path=>{
          if(!window.bulkFixture)throw Error('unexpected mutation');
          window.bulkCalls.push(path);
          if(path==='/search/auto-tag')return {errors:[],total:1};
          if(path==='/search/auto-tag/status')return window.bulkFixture;
          if(path==='/search/auto-tag/export')return {assets_url:'/synthetic-assets.csv'};
          throw Error('unexpected endpoint');
        },notify:message=>{throw Error(message);},getPerson:()=>({role:'editor',features:{search:true}}),refreshLibrary:async()=>{},showPage:()=>{}});
      await ui.assetSummary({asset_id:'synthetic',file_access:'authorized'},document.getElementById('details'),{isCurrent:()=>true});
      document.getElementById('search-tools').hidden=false;
    },{origin,labels});
    const cards=page.locator('#details details.evidence-card');
    assert.equal(await cards.count(),5);
    await t.test('new hashes take precedence and actual evidence modality is named',async()=>{
      const image=await cards.nth(0).innerText(),audio=await cards.nth(1).innerText(),content=await cards.nth(2).innerText();
      // Closed details still have actual paragraph text in the DOM.
      const paragraphs=await cards.locator('p').allTextContents();
      assert(paragraphs[0].includes('图像 SHA256 '+ 'a'.repeat(12)));
      assert(!paragraphs[0].includes('f'.repeat(12)));
      assert(paragraphs[1].includes('音频 SHA256 '+ 'b'.repeat(12))&&!paragraphs[1].includes('帧'));
      assert(paragraphs.some(value=>value.includes('内容 SHA256 '+ 'c'.repeat(12))));
      assert(image.includes('静态十字段')&&audio.includes('音频八字段')&&content.includes('完整内容'));
    });
    await t.test('old frames and real zero confidence remain compatible',async()=>{
      assert((await cards.nth(3).textContent()).includes('帧 SHA256 '+ 'd'.repeat(12)));
      assert((await cards.nth(3).textContent()).includes('模型分数 0.000'));
    });
    await t.test('missing metadata is explicit and synthetic content remains plain text',async()=>{
      const missing=await cards.nth(4).textContent();
      assert(missing.includes('模型 未提供 · 证据 未提供 · 内容 SHA256 未提供'));
      assert(!missing.includes('undefined')&&!missing.includes('NaN'));
      assert.equal(await page.locator('#details img').count(),0);
      const text=await page.locator('#details').innerText();
      assert(text.includes('AI 标签初稿（未确认）')&&text.includes('该字段不适用'));
      assert(text.includes('本地十字段初稿')&&text.includes('本地音频初稿')&&text.includes('精细打标初稿：未请求'));
      assert.deepEqual(errors,[]);
    });
    await t.test('terminated execution with missing model stays pending and exports only a draft',async()=>{
      await page.evaluate(()=>{window.bulkFixture={state:'ready',total:1,completed:1,running:0,failed:0,execution_complete:true,semantic_complete:false,waiting:1,pending_capability:1,pending_fields:8};});
      await page.locator('#auto-tag-start').click();
      await page.locator('#auto-tag-state a').waitFor({state:'visible'});
      const text=await page.locator('#auto-tag-state').innerText();
      assert(text.includes('1/1 份已结束处理'));
      assert(text.includes('1 份待模型配置')&&text.includes('8 个字段待补充'));
      assert(text.includes('执行已结束，仍有待模型/复核字段')&&text.includes('AI 初稿'));
      assert(text.includes('医学确认、许可和隐私复核分别保留'));
      assert.equal(await page.locator('#auto-tag-start').isDisabled(),false);
    });
    await t.test('old ready response still exports and terminal failure stops without export',async()=>{
      await page.evaluate(()=>{window.bulkFixture={state:'ready',total:1,completed:1,running:0,failed:0};window.bulkCalls=[];});
      await page.locator('#auto-tag-start').click();
      await page.waitForFunction(()=>window.bulkCalls.includes('/search/auto-tag/export'));
      assert((await page.locator('#auto-tag-state').innerText()).includes('AI 初稿已入索引'));
      await page.evaluate(()=>{window.bulkFixture={state:'partial',total:1,completed:0,running:0,failed:1,execution_complete:true,semantic_complete:false,waiting:0,pending_fields:10};window.bulkCalls=[];});
      await page.locator('#auto-tag-start').click();
      await page.waitForFunction(()=>document.getElementById('auto-tag-state').textContent.includes('续跑失败阶段'));
      assert.equal(await page.locator('#auto-tag-state a').count(),0);
      assert.equal(await page.locator('#auto-tag-start').isDisabled(),false);
      assert(!(await page.evaluate(()=>window.bulkCalls)).includes('/search/auto-tag/export'));
      assert.deepEqual(errors,[]);
    });
  }finally{
    if(browser)await browser.close();
    await new Promise(resolve=>server.close(resolve));
  }
});

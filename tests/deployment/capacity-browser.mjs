// Real UI searches in five isolated Chromium sessions. No page receives a bearer token.
import fs from 'node:fs/promises';
import {createRequire} from 'node:module';
import assert from 'node:assert/strict';
const c=JSON.parse(await fs.readFile(process.argv[2],'utf8'));
const {chromium}=createRequire(import.meta.url)(c.playwright);
const browser=await chromium.launch({headless:true,executablePath:c.chrome,args:['--disable-gpu']});
const rows=[],errors=[],cold=[],holds=[];
try{
 const sessions=await Promise.all(Array.from({length:5},async(_,i)=>{
  const context=await browser.newContext({viewport:{width:1280,height:900}});
  if(c.token_file){
   const token=(await fs.readFile(c.token_file,'utf8')).trim();
   const response=await context.request.post(c.url+'/ui/session',{headers:{Authorization:'Bearer '+token}});
   assert.equal(response.status(),200);
  }
  await context.addInitScript(()=>{
   const native=window.fetch.bind(window);window.d07={pending:null,done:null};
   window.fetch=async(...args)=>{
    const url=String(args[0]);if(!url.endsWith('/search/query'))return native(...args);
    const item={start:performance.now(),epoch_start_ms:Date.now()};window.d07.pending=item;window.d07.done=null;
    const response=await native(...args),body=await response.clone().json();
    item.ok=response.ok;item.status=response.status;item.resultState=body.state;
    item.expected=(body.items??[]).map(g=>g.versions.at(-1).asset_id).sort();return response;
   };
   document.addEventListener('DOMContentLoaded',()=>{
    const target=document.getElementById('collection');
    new MutationObserver(()=>{
     const item=window.d07.pending;if(!item||!item.expected)return;
     requestAnimationFrame(()=>requestAnimationFrame(()=>{
      if(window.d07.pending!==item)return;
      const ids=[...target.querySelectorAll('[data-asset-id]')].map(n=>n.dataset.assetId).sort();
      if(JSON.stringify(ids)!==JSON.stringify(item.expected))return;
      const visible=target.getBoundingClientRect().height>0;
      window.d07.done={...item,elapsed_ms:performance.now()-item.start,epoch_end_ms:Date.now(),visible,
       actionable:[...target.querySelectorAll('button')].every(n=>!n.disabled)};
     }));
    }).observe(target,{childList:true,subtree:true});
   });
  });
  const page=await context.newPage();page.setDefaultTimeout(12000);page.on('pageerror',()=>errors.push({session:i,kind:'pageerror'}));
  const start=performance.now();
  await page.goto(c.url+'/ui/'+(c.tickets?'#local-login='+c.tickets[i]:''));
  await page.waitForFunction(()=>!document.getElementById('workspace').hidden&&document.getElementById('collection').children.length>0);
  cold.push({session:i,elapsed_ms:performance.now()-start,ok:true});
  const check=page.waitForResponse(r=>r.url().endsWith('/search/query'));
  // Checkbox triggers no search while the query is empty.
  await page.locator('#search-literal').check();
  async function search(query,measured){
   const started=performance.now(),epoch=Date.now();
   try{
    await page.evaluate(q=>{document.getElementById('search').value=q;window.d07.done=null;},query);
    await page.locator('#refresh-library').click();
    await page.waitForFunction(()=>window.d07.done!==null);
    const value=await page.evaluate(()=>window.d07.done);
    assert(value.ok&&value.visible&&value.actionable);if(measured)rows.push({...value,session:i,ok:true});return {...value,session:i,ok:true};
   }catch(e){if(measured)rows.push({session:i,ok:false,timeout:e.name==='TimeoutError',elapsed_ms:performance.now()-started,epoch_start_ms:epoch,epoch_end_ms:Date.now()});else throw e;}
  }
  const first=await search(c.query,false);await check;
  cold.push({...first,kind:'first_search'});
  for(let n=0;n<4;n++)await search(c.query,false);
  return {context,page,search};
 }));
 await Promise.all(sessions.map(async s=>{for(let n=0;n<c.per_session;n++)await s.search(n%4===3?'D07-no-such-material':c.query,true);}));
 const save=()=>fs.writeFile(c.result,JSON.stringify({state:errors.length||rows.some(r=>!r.ok)?'failed':'passed',
  sessions:5,actual_chromium:true,measurement:'browser fetch start to matching actionable DOM after two animation frames; excludes typing debounce; includes response-clone instrumentation',
  rows,cold,page_errors:errors,holds}));
 await save();
 if(c.hold_until){
  while(true){
   try{await fs.access(c.hold_until);break;}catch{}
   await Promise.all(sessions.map(async({page},i)=>{
    const status=await page.evaluate(()=>fetch('/ui/session').then(r=>r.status)).catch(()=>0);
    holds.push({session:i,at_ms:Date.now(),status});
   }));
   await save();await new Promise(resolve=>setTimeout(resolve,5000));
  }
 }
 assert.equal(errors.length,0);assert(rows.every(r=>r.ok));assert(holds.every(h=>h.status===200));await save();
}catch(e){
 await fs.writeFile(c.result,JSON.stringify({state:'failed',error_type:e.name,rows,cold,page_errors:errors,holds}));
 console.error('D07 browser validation failed ('+e.name+')');process.exitCode=1;
}finally{await browser.close();}

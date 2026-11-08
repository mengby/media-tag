// Explicit terminology administration. No draft, click, or trial changes online labels.
export function installTermsUI({api,post,notify,getPerson,showPage,refreshVocabulary}){
  const $=id=>document.getElementById(id),el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
  const guard=fn=>async event=>{try{await fn(event);}catch(error){notify(error.message);}};
  const button=(text,fn)=>{const n=el('button',text);n.type='button';n.addEventListener('click',guard(fn));return n;};
  const field=(text,node)=>{const n=el('label',text);node.setAttribute('aria-label',text);n.append(node);return n;};
  const text=(id='',value='',area=false)=>{const n=el(area?'textarea':'input');if(id)n.id=id;n.value=value;n.maxLength=2000;return n;};
  const select=(options,value='')=>{const n=el('select');for(const [key,label]of options){const o=el('option',label);o.value=key;n.append(o);}n.value=value;return n;};
  const check=label=>{const n=el('input');n.type='checkbox';return [field(label,n),n];};
  const relationships=[['equivalent','等价 / 同义'],['contextual','有条件对应'],['broader','上位'],['narrower','下位'],['related','相关'],['metaphor','比喻']];
  const state=s=>({draft:'草稿',review:'待审核',approved:'已审核待发布',published:'已发布快照',rejected:'已拒绝',pending:'待审核',needs_info:'需补充',draft_created:'已进入草稿',withdrawn:'已撤回'}[s]||s);
  let epoch=0,data=null,candidate=null,selection=new Set(),editEntry=null,imported=null,importTable=null,importPreview=null;
  const nav=button('术语词表',async()=>{showPage('terms');await refresh();});nav.id='terms-nav';nav.dataset.page='terms';nav.hidden=true;document.querySelector('nav[aria-label="主导航"]').append(nav);
  const page=el('section',undefined,'page');page.id='terms';page.hidden=true;
  const heading=el('div',undefined,'page-heading');const title=el('div');title.append(el('span','TERMINOLOGY','eyebrow'),el('h1','术语词表'),el('p','候选先审核，发布后再切换查询版本。素材医学复核仍独立进行。'));
  heading.append(title,button('刷新',refresh));const status=el('p',undefined,'muted');status.id='terms-status';
  const layout=el('div',undefined,'term-layout'),sidebar=el('aside',undefined,'panel'),editor=el('div',undefined,'panel');editor.id='term-editor';
  const list=el('div');list.id='term-candidates';const creates=el('div',undefined,'actions');
  creates.append(button('新建分类草稿',()=>createCandidate('taxonomy')),button('新建术语草稿',()=>createCandidate('lexicon')));
  sidebar.append(el('h2','候选与修订'),creates,list);layout.append(sidebar,editor);
  const feedback=el('section',undefined,'panel');feedback.id='term-feedback';const history=el('section',undefined,'panel');history.id='term-history';
  page.append(heading,status,layout,feedback,history);$('workspace').append(page);
  function noteValue(){const value=$('term-note')?.value.trim();if(!value)throw Error('请填写本次操作说明');return value;}
  async function refresh(){
    const current=++epoch;const value=await api('/search/management');if(current!==epoch)return;data=value;
    const releaseName=id=>data.releases.find(r=>r.release_id===id)?.version||'尚无';
    status.textContent=`发布：${releaseName(data.publication.lexicon_release_id)}；查询：${releaseName(data.serving.serving.lexicon_release_id)}${data.serving.pending_publication?' · 有待启用版本':''}`;
    for(const b of creates.querySelectorAll('button'))b.disabled=!data.capabilities.manage;
    list.replaceChildren();for(const item of data.candidates){const b=button(`${item.kind==='taxonomy'?'分类':'术语'} · ${state(item.state)} · 修订 ${item.revision}`,()=>open(item.candidate_id));b.className='term-candidate';list.append(b);}
    renderFeedback();renderHistory();
    if(candidate&&data.candidates.some(c=>c.candidate_id===candidate.candidate_id))await open(candidate.candidate_id);
    else editor.replaceChildren(el('p','选择候选，或基于已发布版本新建草稿。'));
  }
  async function createCandidate(kind){
    if(!data?.capabilities.manage)throw Error('需要词表管理权限');
    const head=data.publication;let payload;
    if(kind==='taxonomy')payload=head.taxonomy_release_id?(await api('/terminology/releases/'+head.taxonomy_release_id)).payload:{schema_version:'d13-taxonomy-v1',template_fingerprint:data.template_fingerprint,options:[]};
    else{
      if(!head.taxonomy_release_id)throw Error('请先审核并发布分类词表');
      const previous=head.lexicon_release_id||data.releases.find(r=>r.kind==='lexicon')?.release_id;
      payload=previous?(await api('/terminology/releases/'+previous)).payload:{schema_version:'d13-lexicon-v1',template_fingerprint:data.template_fingerprint,concepts:[],aliases:[],relations:[]};
      payload.taxonomy_release_id=head.taxonomy_release_id;
    }
    const created=await post('/terminology/candidates',{request_id:crypto.randomUUID(),kind,base_release_id:head[kind+'_release_id'],payload,note:'人工从术语管理页基于已发布记录新建草稿'});
    candidate={candidate_id:created.candidate_id};await refresh();
  }
  async function save(payload,note){
    const id=candidate.candidate_id;
    await post(`/terminology/candidates/${id}/revision`,{request_id:crypto.randomUUID(),expected_revision:candidate.revision,payload,note});
    await refresh();
  }
  function entries(){return candidate.kind==='taxonomy'?candidate.payload.options.map(e=>['options',e]):['concepts','aliases','relations'].flatMap(group=>candidate.payload[group].map(e=>[group,e]));}
  async function open(id){
    const generation=++epoch;const value=await api('/terminology/candidates/'+id);if(generation!==epoch)return;candidate=value;selection.clear();editEntry=null;
    imported=null;importTable=null;importPreview=null;
    editor.replaceChildren(el('h2',`${candidate.kind==='taxonomy'?'分类词表':'专业术语与常用说法'} · ${state(candidate.state)}`));
    const actionNote=text('term-note');editor.append(field('本次操作说明',actionNote));
    const errors=el('div');errors.id='term-issues';for(const issue of candidate.issues)errors.append(el('p',`${issue.code}${issue.entry_id?' · '+issue.entry_id:''}`,'muted'));editor.append(errors);
    const rows=el('div');rows.id='term-entries';const names=new Map((candidate.payload.concepts||[]).map(c=>[c.entry_id,c.name]));
    const keyword=text('term-filter'),kindFilter=select([['','全部类型'],['options','分类选项'],['concepts','概念'],['aliases','常用说法'],['relations','概念关系']]),reviewFilter=select([['','全部审核状态'],['pending','待审核'],['approved','已审核']]);
    const filtering=el('div',undefined,'edit-fields');filtering.append(field('词条搜索',keyword),field('类型',kindFilter),field('状态',reviewFilter));editor.append(filtering);
    const selectedNote=el('p','已勾选 0 条','muted');editor.append(selectedNote);
    function filter(){for(const row of rows.children)row.hidden=!!(keyword.value&&!row.textContent.toLocaleLowerCase().includes(keyword.value.toLocaleLowerCase())||kindFilter.value&&row.dataset.kind!==kindFilter.value||reviewFilter.value&&row.dataset.review!==reviewFilter.value);}
    keyword.addEventListener('input',filter);kindFilter.addEventListener('change',filter);reviewFilter.addEventListener('change',filter);
    for(const [group,entry]of entries()){
      const row=el('div',undefined,'evidence-card');const [label,checkbox]=check(group==='aliases'?`${entry.expression} → ${names.get(entry.target_id)||'不确定'}`:group==='relations'?`${names.get(entry.source_id)||entry.source_id} → ${names.get(entry.target_id)||entry.target_id}`:entry.name||`${entry.field} · ${entry.value}`);
      row.dataset.kind=group;row.dataset.review=candidate.approvals[entry.entry_id]?'approved':'pending';checkbox.addEventListener('change',()=>{checkbox.checked?selection.add(entry.entry_id):selection.delete(entry.entry_id);selectedNote.textContent=`已勾选 ${selection.size} 条`;});row.append(label,el('p',`${candidate.approvals[entry.entry_id]?'已逐条审核':'待逐条审核'}${entry.status==='retired'?' · 已停用':''}`,'muted'));
      const sources=el('details');sources.append(el('summary','依据与适用范围'));for(const source of entry.sources)sources.append(el('p',`${source.locator}\n原句：${source.quote}\n范围：${source.usage}`));row.append(sources);
      if(data.capabilities.manage&&candidate.state!=='published'){
        row.append(button('修改',()=>edit(group,entry)));
        if(group==='aliases'||group==='relations')row.append(button('从此草稿停用',async()=>{const payload=structuredClone(candidate.payload);payload[group]=payload[group].filter(e=>e.entry_id!==entry.entry_id);await save(payload,noteValue());}));
        else if(entry.status!=='retired')row.append(button('标记停用',async()=>{const payload=structuredClone(candidate.payload);payload[group]=payload[group].map(e=>e.entry_id===entry.entry_id?{...e,status:'retired'}:e);await save(payload,noteValue());}));
      }rows.append(row);
    }editor.append(rows);
    const actions=el('div',undefined,'actions');
    if(data.capabilities.manage&&candidate.state!=='published'){
      for(const [group,label]of candidate.kind==='taxonomy'?[['options','新增分类选项']]:[['concepts','新增概念候选'],['aliases','新增对应'],['relations','新增概念关系']])actions.append(button(label,()=>edit(group,null)));
      actions.append(button('提交审核',async()=>{await post(`/terminology/candidates/${id}/review`,{request_id:crypto.randomUUID(),expected_revision:candidate.revision,action:'submit',note:noteValue()});await refresh();}),
        button('审核所勾选条目',async()=>{if(!selection.size)throw Error('逐条查看依据后勾选要审核的条目');await post(`/terminology/candidates/${id}/review`,{request_id:crypto.randomUUID(),expected_revision:candidate.revision,action:'approve',entry_ids:[...selection],note:noteValue()});await refresh();}),
        button('退回草稿补充',()=>save(structuredClone(candidate.payload),noteValue())));
    }editor.append(actions);
    const editHost=el('div');editHost.id='term-entry-editor';editor.append(editHost);
    if(candidate.kind==='lexicon'&&data.capabilities.manage){
      const trial=el('div',undefined,'term-trial');trial.id='term-trial';const query=text('term-trial-query');const output=el('div');output.id='term-trial-output';
      trial.append(el('h3','草稿试搜'),field('试搜原话',query),button('对比当前发布与草稿',async()=>{
        const result=await post(`/search/management/candidates/${id}/trial`,{expected_revision:candidate.revision,request:{request:{query:query.value}}});
        output.replaceChildren();for(const [key,label]of [['published','当前查询版本'],['draft','草稿（不会上线）']]){const part=result[key];output.append(el('h4',label),el('p',`${part.resolution.state} · 可见素材 ${part.items.length}`));for(const item of part.items)for(const version of item.versions)output.append(el('p',version.name));}
      }),output);editor.append(trial);if(data.capabilities.manage&&candidate.state!=='published')renderImport(editor);
    }
    const publishing=el('div');const version=text('term-version');version.placeholder='由发布者指定唯一版本号';const diff=el('pre');diff.id='term-publish-diff';
    publishing.append(el('h3','发布差异'),field('发布版本号',version),button('检查本次差异',async()=>{const result=await api(`/search/management/candidates/${id}/diff`);diff.textContent=JSON.stringify(result,null,2);}),diff);
    if(data.capabilities.publish){const publishButton=button(candidate.kind==='lexicon'?'发布并启用查询':'发布分类快照',async()=>{
      const revision=candidate.revision,releaseVersion=version.value.trim(),operationNote=noteValue();
      const checked=await api(`/search/management/candidates/${id}/diff`);if(!releaseVersion)throw Error('请指定唯一版本号');
      const published=await post(`/terminology/candidates/${id}/publish`,{request_id:crypto.randomUUID(),expected_revision:revision,expected_generation:checked.generation,version:releaseVersion,note:operationNote});
      await refresh();if(published.kind==='lexicon')await activate();notify('已保存发布记录；当前查询版本以页头状态为准');
    });publishButton.disabled=candidate.state!=='approved';publishing.append(publishButton);}editor.append(publishing);
  }
  function sourceFields(host,entry){
    const previous=entry?.sources?.[0];const kind=select([['human','人工来源'],['authorized_record','授权记录'],['external_reference','外部参考'],['synthetic','工程合成（仅验证）']],previous?.kind||'human');
    const locator=text('term-source-locator',previous?.locator||''),quote=text('term-source-quote',previous?.quote||'',true),usage=text('term-source-usage',previous?.usage||'',true),reference=text('term-source-reference',previous?.reference||'');
    host.append(field('来源类型',kind),field('具体来源 / 定位',locator),field('原句 / 引用位置',quote),field('实际允许使用范围',usage),field('来源页面（可留空）',reference));
    return ()=>{
      if(!locator.value.trim()||!quote.value.trim()||!usage.value.trim())throw Error('请填写来源、原句和实际使用范围');
      const source={kind:kind.value,locator:locator.value,quote:quote.value,usage:usage.value,reference:reference.value||null};
      const sources=[...(entry?.sources||[])];if(!sources.some(s=>JSON.stringify(s)===JSON.stringify(source)))sources.push(source);return sources;
    };
  }
  async function edit(group,entry){
    const host=$('term-entry-editor');host.replaceChildren(el('h3',entry?'修改这条候选':'新增候选'));
    const form=el('form');form.id='term-entry-form';form.className='term-workbench';const left=el('div'),right=el('div');form.append(left,right);const controls={};let taxOptions=[];
    if(candidate.kind==='lexicon'){const taxonomy=await api('/terminology/releases/'+candidate.payload.taxonomy_release_id);taxOptions=taxonomy.payload.options.filter(o=>o.status==='active');}
    const value=(name,label,initial='',area=false)=>{const node=text('term-'+name,initial,area);controls[name]=node;left.append(field(label,node));return node;};
    const choice=(name,label,options,initial='')=>{const node=select(options,initial);node.id='term-'+name;controls[name]=node;left.append(field(label,node));return node;};
    if(group==='options'){
      const fields=data.taxonomy_fields.map(f=>[f.name,f.name]);const fieldChoice=choice('field','原契约字段',fields,entry?.field||fields[0]?.[0]);
      const free=value('value','选项 / 过程规范说法',entry?.value||'');const locked=choice('locked-value','原契约已有选项',[]);
      function options(){const definition=data.taxonomy_fields.find(f=>f.name===fieldChoice.value);locked.parentElement.hidden=definition?.mode!=='closed';free.parentElement.hidden=definition?.mode==='closed';locked.replaceChildren();for(const v of definition?.options||[]){const o=el('option',v);o.value=v;locked.append(o);}if(entry?.value)locked.value=entry.value;}
      fieldChoice.addEventListener('change',options);options();
    }else if(group==='concepts'){
      value('name','专业名称',entry?.name||'');choice('concept_type','概念类型',[['department','科室'],['organ','器官'],['symptom','症状'],['disease','疾病'],['process','过程'],['state','状态'],['other','其他']],entry?.concept_type||'other');
      value('definition','定义',entry?.definition||'',true);value('scope','适用范围',entry?.scope||'',true);value('old_names','旧名称（每行一条）',(entry?.old_names||[]).join('\n'),true);
      const options=choice('option_ids','对应已发布分类（可多选）',taxOptions.map(o=>[o.entry_id,`${o.field} · ${o.value}`]));options.multiple=true;for(const o of options.options)o.selected=(entry?.option_ids||[]).includes(o.value);
    }else{
      const concepts=(candidate.payload.concepts||[]).filter(c=>c.status==='active').map(c=>[c.entry_id,c.name]);
      if(group==='aliases')value('expression','常用说法',entry?.expression||'');else choice('source_id','起点概念',[['','选择概念'],...concepts],entry?.source_id||'');
      choice('target_id','目标专业术语',[['','不确定'],...concepts],entry?.target_id||'');choice('relation','关系',group==='aliases'?relationships:relationships.filter(([k])=>k!=='contextual'),entry?.relation||'equivalent');
      const [expand,box]=check('允许作为同义扩展（仅等价且条件满足时）');box.id='term-default-expand';box.checked=entry?.default_expand||false;controls.default_expand=box;left.append(expand);
      if(group==='aliases'){
        choice('polarity','适用肯定 / 否定',[['unspecified','不限制'],['affirmed','肯定'],['negated','否定']],entry?.context?.polarity||'unspecified');
        choice('direction','适用方向',[['unspecified','不限制'],['static','稳定'],['increasing','加重 / 增加'],['decreasing','减轻 / 减少'],['transition','转换']],entry?.context?.direction||'unspecified');
        value('required_terms','必要上下文词（每行一条）',(entry?.context?.required_terms||[]).join('\n'),true);value('excluded_terms','不能替换的上下文词（每行一条）',(entry?.context?.excluded_terms||[]).join('\n'),true);
        for(const [key,label,fieldName]of [['departments','适用科室','科室'],['organs','适用器官','器官/系统']]){const node=choice(key,label,taxOptions.filter(o=>o.field===fieldName).map(o=>[o.entry_id,o.value]));node.multiple=true;for(const o of node.options)o.selected=(entry?.context?.[key]||[]).includes(o.value);}
        value('ambiguity_group','歧义组（同说法不同目标时填写相同组名）',entry?.ambiguity_group||'');
      }
    }
    if(group==='concepts'||group==='options'){
      choice('status','词条状态',[['active','有效'],['retired','停用']],entry?.status||'active');
      const replacementItems=candidate.payload[group].filter(e=>e.entry_id!==entry?.entry_id&&e.status==='active');
      const replacements=choice('replacements','停用后的替代条目（可多选）',replacementItems.map(e=>[e.entry_id,e.name||`${e.field} · ${e.value}`]));
      replacements.multiple=true;for(const o of replacements.options)o.selected=(entry?.replacements||[]).includes(o.value);
    }
    value('rationale','对应理由',entry?.rationale||'',true);right.append(el('h4','来源与适用范围'));const getSources=sourceFields(right,entry);const trial=$('term-trial');if(trial)right.append(trial);
    const saveButton=el('button','保存为草稿','primary');saveButton.type='submit';saveButton.id='save-term-entry';left.append(saveButton,button('取消编辑',()=>open(candidate.candidate_id)));host.append(form);
    form.addEventListener('submit',guard(async event=>{
      event.preventDefault();const result={...(entry||{}),entry_id:entry?.entry_id||`${group.slice(0,-1)}-${crypto.randomUUID()}`,sources:getSources(),rationale:controls.rationale.value};
      const lines=node=>node.value.split(/\n/).map(t=>t.trim()).filter(Boolean),selected=node=>[...node.selectedOptions].map(o=>o.value);
      if(group==='options'){result.field=controls.field.value;result.value=controls['locked-value'].parentElement.hidden?controls.value.value:controls['locked-value'].value;}
      if(group==='concepts')Object.assign(result,{name:controls.name.value,concept_type:controls.concept_type.value,definition:controls.definition.value,scope:controls.scope.value,option_ids:selected(controls.option_ids),old_names:lines(controls.old_names)});
      if(group==='concepts'||group==='options')Object.assign(result,{status:controls.status.value,replacements:selected(controls.replacements)});
      if(group==='aliases'||group==='relations')Object.assign(result,{target_id:controls.target_id.value||null,relation:controls.relation.value,default_expand:controls.default_expand.checked});
      if(group==='relations')result.source_id=controls.source_id.value;
      if(group==='aliases')Object.assign(result,{expression:controls.expression.value,ambiguity_group:controls.ambiguity_group.value||null,context:{polarity:controls.polarity.value,direction:controls.direction.value,
        departments:selected(controls.departments),organs:selected(controls.organs),required_terms:lines(controls.required_terms),excluded_terms:lines(controls.excluded_terms)}});
      const payload=structuredClone(candidate.payload);const index=payload[group].findIndex(e=>e.entry_id===result.entry_id);if(index<0)payload[group].push(result);else payload[group][index]=result;
      await save(payload,noteValue());
    }));
  }
  async function activate(){
    const fresh=await api('/search/management');const pub=fresh.publication;
    if(!pub.lexicon_release_id)throw Error('先发布绑定当前分类的术语词表');
    await post('/terminology/text-indices/activate',{request_id:crypto.randomUUID(),expected_generation:pub.generation,expected_serving_epoch:fresh.serving.serving.epoch,
      taxonomy_release_id:pub.taxonomy_release_id,lexicon_release_id:pub.lexicon_release_id,note:'人工从管理页构建并启用已经审核发布的查询版本'});await refresh();await refreshVocabulary();
  }
  function renderFeedback(){
    feedback.replaceChildren(el('h2','待审核与补充记录'));
    for(const item of data.feedback){const card=el('div',undefined,'evidence-card');card.append(el('strong',item.payload.expression),el('p',`${state(item.state)} · ${item.payload.quote||'未补充原句'}`));
      if(data.capabilities.manage&&['pending','needs_info'].includes(item.state))for(const [action,label]of [['promote','转为词表草稿'],['needs_info','退回补充'],['reject','拒绝']])card.append(button(label,async()=>{const note=prompt('本次处理说明');if(!note?.trim())return;const result=await post(`/search/feedback/${item.feedback_id}/decision`,{request_id:crypto.randomUUID(),expected_revision:item.revision,action,note});if(result.candidate_id)candidate={candidate_id:result.candidate_id};await refresh();}));feedback.append(card);
    }
  }
  function renderHistory(){
    history.replaceChildren(el('h2','发布与查询版本'));
    if(data.capabilities.publish)history.append(button('构建并启用当前发布版本',activate));
    for(const release of data.releases){const row=el('div',undefined,'evidence-card');row.append(el('strong',`${release.kind==='taxonomy'?'分类':'术语'} · ${release.version}`),el('p',`${release.published_by} · ${release.published_at}`,'muted'));
      if(release.kind==='lexicon'&&data.capabilities.publish)row.append(button('回滚并启用这一组',async()=>{
        const note=prompt('回滚说明');if(!note?.trim())return;await post('/terminology/rollback',{request_id:crypto.randomUUID(),expected_generation:data.publication.generation,taxonomy_release_id:release.taxonomy_release_id,lexicon_release_id:release.release_id,note});await refresh();await activate();notify('已回滚查询整组');
      }));history.append(row);
    }
  }
  function renderImport(host){
    const area=el('details');area.append(el('summary','导入 Excel / CSV 候选'));
    const file=text('term-import-file');file.type='file';file.accept='.csv,.xlsx';const sheet=select([]);sheet.id='term-import-sheet';const mapping=el('div',undefined,'edit-fields');mapping.id='term-import-mapping';const reports=el('div');reports.id='term-import-reports';
    const selectors=new Map();const defaults=text('term-import-usage');defaults.placeholder='本批候选实际允许使用范围（可逐行映射）';
    const defaultRelation=select([['','按逐行映射值'],...relationships]);defaultRelation.id='term-import-relation';
    area.append(field('候选文件',file),button('上传并检查表结构',async()=>{
      if(!file.files[0])throw Error('请选择CSV或XLSX文件');const format=file.files[0].name.toLowerCase().endsWith('.xlsx')?'xlsx':'csv';
      imported=await api('/search/imports?format='+format,{method:'POST',body:file.files[0],headers:{'Content-Type':'application/octet-stream'}});importPreview=null;sheet.replaceChildren();
      const blank=el('option','明确选择工作表');blank.value='';sheet.append(blank);for(const name of imported.sheets){const o=el('option',name);o.value=name;sheet.append(o);}
      sheet.parentElement.hidden=format==='csv';if(format==='csv')await inspect();else{mapping.replaceChildren();reports.replaceChildren(el('p','请选择要导入的工作表。'));}
    }),field('工作表',sheet),mapping,field('统一关系（不映射关系列时使用）',defaultRelation),field('本批来源使用范围（未映射时使用）',defaults));
    async function inspect(){importTable=await api(`/search/imports/${imported.import_id}`+(imported.format==='xlsx'?'?sheet='+encodeURIComponent(sheet.value):''));mapping.replaceChildren();selectors.clear();
      for(const [key,label]of [['expression','常用说法'],['target_id','专业术语ID（可留空）'],['target_name','专业术语名称（可留空）'],['relation','关系代码'],['rationale','对应理由'],['source_locator','来源定位'],['source_quote','来源原句'],['source_usage','实际使用范围'],['source_reference','来源页面'],['context_json','语境JSON（可选）']]){const node=select([['','不映射'],...importTable.headers.map(h=>[h,h])]);selectors.set(key,node);mapping.append(field(label,node));}
    }
    sheet.addEventListener('change',guard(inspect));const selectedRows=new Set();
    function specification(apply){const columns={};for(const [key,node]of selectors)if(node.value)columns[key]=node.value;return {request_id:crypto.randomUUID(),candidate_id:candidate.candidate_id,expected_revision:candidate.revision,sheet:imported.format==='xlsx'?sheet.value:null,columns,
      defaults:{...(defaults.value?{source_usage:defaults.value}:{}),...(defaultRelation.value?{relation:defaultRelation.value}:{})},selected_rows:apply?[...selectedRows]:[],apply,note:noteValue()};}
    area.append(button('预检映射与逐行问题',async()=>{if(!imported||!importTable)throw Error('先选择文件、工作表和列映射');const request=specification(false);importPreview=await post(`/search/imports/${imported.import_id}/preview`,request);reports.replaceChildren();selectedRows.clear();
      for(const row of importPreview.reports){const [label,box]=check(`第 ${row.row} 行 · ${row.status} · ${row.codes.join('、')}`);box.disabled=row.status!=='candidate';box.addEventListener('change',()=>box.checked?selectedRows.add(row.row):selectedRows.delete(row.row));reports.append(label);}
    }),button('仅将勾选行导入草稿',async()=>{if(!importPreview||!selectedRows.size)throw Error('先预检并明确勾选要导入的行');const result=await post(`/search/imports/${imported.import_id}/preview`,specification(true));if(!result.applied)throw Error('所选行存在冲突，未导入；请检查预检和来源。');await refresh();notify('所选候选已进入草稿，仍需逐条审核');}),reports);host.append(area);
  }
  return {session(person){nav.hidden=!person.features.search||(!person.terminology_capabilities?.manage&&!person.terminology_capabilities?.publish);},
    reset(){epoch++;data=null;candidate=null;selection.clear();imported=null;importTable=null;importPreview=null;editor.replaceChildren();list.replaceChildren();feedback.replaceChildren();history.replaceChildren();status.textContent='';nav.hidden=true;}};
}

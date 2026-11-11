import {installTermsUI} from './terms.js';

export function searchMessage(result){
  if(!result)return '';
  const labels={recognized_no_visible_assets:'已识别对应术语，但当前条件下没有你可用的素材。',unrecognized_expression:'暂未识别这个说法，可以补充对应关系。',choose_concept:'这个说法有候选或歧义，请选择想找的术语。',terminology_unavailable:'术语服务当前降级，仍保留原文检索。'};
  return labels[result.empty_reason]||'';
}

export function installSearchUI({api,post,notify,getPerson,refreshLibrary,showPage}){
  const $=id=>document.getElementById(id),el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
  const guard=fn=>async event=>{try{await fn(event);}catch(e){notify(e.message);}};
  const button=(text,fn)=>{const n=el('button',text);n.type='button';n.addEventListener('click',guard(fn));return n;};
  const field=(text,node)=>{const label=el('label',text);node.setAttribute('aria-label',text);label.append(node);return label;};
  const input=(id,type='text')=>{const n=el('input');n.id=id;n.type=type;return n;};
  const select=(id,options)=>{const n=el('select');n.id=id;for(const [value,text]of options){const o=el('option',text);o.value=value;n.append(o);}return n;};
  let epoch=0,reviewEpoch=0,assetEpoch=0,vocabulary=null,result=null,selected=[],selectionVersion=null,lastQuery='',editing=null,feedbackVersion=null,feedbackContext={},reported=null,mode='feedback';
  const wrapper=el('div',undefined,'search-tools');wrapper.id='search-tools';wrapper.hidden=true;
  const actions=el('div',undefined,'actions');
  actions.append(button('补充说法',()=>openFeedback()),button('我的提交',refreshFeedback));
  const tagging=el('div');tagging.id='auto-tag-state';let taggingBusy=false;
  const tagButton=button('我的素材批量自动打标',async()=>{
    if(taggingBusy)return;taggingBusy=true;tagButton.disabled=true;
    const generation=epoch;
    try{
      const submitted=await post('/search/auto-tag',{});
      if(generation!==epoch)return;
      if(submitted.errors.length)throw Error(`有 ${submitted.errors.length} 份素材尚不满足分析条件，请查看我的任务。`);
      tagging.textContent=`已启动 ${submitted.total} 份素材的自动打标，成功阶段会复用。`;
      await refreshTagging();
    }catch(error){if(generation!==epoch)return;taggingBusy=false;tagButton.disabled=getPerson()?.role==='reader';throw error;}
  });
  tagButton.id='auto-tag-start';actions.append(tagButton);
  async function refreshTagging(){
    if(!taggingBusy||!getPerson())return;
    const generation=epoch;
    try{
      const state=await post('/search/auto-tag/status',{});
      if(generation!==epoch)return;
      const modern=typeof state.execution_complete==='boolean';
      tagging.textContent=`自动打标：${state.completed}/${state.total} 份${modern?'已结束处理':'完成'}，${state.running} 份排队或运行，${state.failed} 份需重试。`;
      if(modern&&state.semantic_complete===false)tagging.append(el('p',`仍有 ${state.waiting||0} 份待处理，${state.pending_capability||0} 份待模型配置，${state.pending_fields||0} 个字段待补充。审核、医学确认、许可和隐私复核分别保留。`));
      if(state.state==='ready'){
        const exported=await post('/search/auto-tag/export',{});if(generation!==epoch)return;
        const link=el('a','下载本批 35 列索引表');link.href=exported.assets_url;link.download='assets.csv';
        tagging.append(el('p',modern&&state.semantic_complete===false?'执行已结束，仍有待模型/复核字段；当前合格结果可导出为 AI 初稿。':'AI 初稿已入索引，审核可后续进行。'),link);taggingBusy=false;tagButton.disabled=false;await refreshLibrary();
      }else if(state.state==='partial'||state.execution_complete===true){
        taggingBusy=false;tagButton.disabled=false;tagging.append(el('p','再次点击自动打标可续跑失败阶段；成功结果会复用。'));
      }else setTimeout(()=>refreshTagging(),5000);
    }catch(error){if(generation!==epoch)return;taggingBusy=false;tagButton.disabled=false;notify(error.message);}
  }
  const include=input('search-drafts','checkbox'),literal=input('search-literal','checkbox'),visual=input('search-visual-drafts','checkbox');
  include.checked=true;
  actions.append(field('包含未确认 AI 建议',include),field('视觉候选（未校准）',visual),field('仅按原文检索',literal));
  const filters=el('details');filters.append(el('summary','检索语境'));
  const grid=el('div',undefined,'edit-fields');
  const department=select('search-department',[['','科室不限']]),organ=select('search-organ',[['','器官不限']]);
  const polarity=select('search-polarity',[['unspecified','按原文判断'],['affirmed','肯定 / 存在'],['negated','否定 / 未出现']]);
  const direction=select('search-direction',[['unspecified','方向不限'],['static','保持稳定'],['increasing','增加 / 加重'],['decreasing','减少 / 缓解'],['transition','方向转换']]);
  grid.append(field('科室',department),field('器官 / 系统',organ),field('肯定与否定',polarity),field('变化方向',direction));filters.append(grid);
  const summary=el('div');summary.id='search-summary';summary.setAttribute('aria-live','polite');
  const mySubmissions=el('div');mySubmissions.id='my-feedback';mySubmissions.hidden=true;
  wrapper.append(actions,tagging,filters,summary,mySubmissions);$('library').querySelector('.toolbar').after(wrapper);
  const dialog=el('dialog');dialog.id='feedback-dialog';const form=el('form');const title=el('h2','补充说法');
  const expression=input('feedback-expression');expression.required=true;expression.maxLength=2000;
  const quote=el('textarea');quote.id='feedback-quote';quote.maxLength=2000;
  const target=select('feedback-target',[['','不确定 / 都不是']]);
  const note=el('p','提交后进入待审核；不会立即改变搜索或素材标签。','muted');
  const submit=el('button','提交补充','primary');submit.type='submit';submit.id='submit-feedback';
  form.append(title,field('常用说法 / 搜索原话',expression),field('使用原句 / 想找什么画面',quote),field('你认为对应的专业术语（可留空）',target),note,
    button('取消',()=>dialog.close()),submit);dialog.append(form);document.body.append(dialog);
  const conceptHost=el('section',undefined,'concept-review');conceptHost.id='concept-review';$('review-editor').append(conceptHost);
  const [aliasLabel,aliasCheckbox]=checkExport();$('export-ai').parentElement.after(aliasLabel);
  function checkExport(){const label=el('label',undefined,'check'),box=input('export-aliases','checkbox');label.append(box,document.createTextNode('仅补充已确认、条件满足的同义词（最多 8 条）'));return [label,box];}
  const termsUI=installTermsUI({api,post,notify,getPerson,showPage,refreshVocabulary:()=>loadVocabulary(true)});
  async function loadVocabulary(force=false){
    if(!vocabulary||force){const generation=epoch;const data=await api('/search/vocabulary');if(generation!==epoch)return null;vocabulary=data;}
    return vocabulary;
  }
  function fillOptions(){
    for(const [node,fieldName,label]of [[department,'科室','科室不限'],[organ,'器官/系统','器官不限']]){
      const old=node.value;node.replaceChildren();const first=el('option',label);first.value='';node.append(first);
      for(const o of vocabulary?.options||[])if(o.field===fieldName){const n=el('option',o.value);n.value=o.option_id;node.append(n);}node.value=old;
    }
    const old=target.value;target.replaceChildren();const blank=el('option','不确定 / 都不是');blank.value='';target.append(blank);
    for(const concept of vocabulary?.concepts||[]){const o=el('option',concept.name);o.value=concept.concept_id;target.append(o);}target.value=old;
  }
  async function openFeedback(kind='feedback',candidate=null){
    await loadVocabulary(true);fillOptions();mode=kind;editing=null;
    title.textContent=kind==='gap'?'记录素材需求':'补充说法';submit.textContent=kind==='gap'?'登记素材需求':'提交补充';
    expression.value=$('search').value;quote.value='';target.value='';reported=candidate?{...candidate,lexicon_release_id:result?.resolution?.versions?.lexicon_release_id}:null;feedbackVersion=vocabulary?.versions?.lexicon_release_id||null;target.parentElement.hidden=kind==='gap';
    feedbackContext={departments:department.value?[department.value]:[],organs:organ.value?[organ.value]:[],polarity:polarity.value,direction:direction.value};
    note.textContent=kind==='gap'?'记录需要的画面，不表示库内已有或已获许可。':(candidate?`反馈当前对应：${candidate.name}。你认为正确的术语可留空。`:'')+'提交后进入待审核；不会立即改变搜索或素材标签。';
    if(getPerson()?.role==='reader')throw Error('当前只读账号不能提交补充');dialog.showModal();
  }
  form.addEventListener('submit',guard(async event=>{
    event.preventDefault();const data={request_id:crypto.randomUUID(),expression:expression.value,quote:quote.value,
      target_id:mode==='gap'?null:target.value||null,lexicon_release_id:feedbackVersion,
      reported_target_id:editing?.payload.reported_target_id||reported?.concept_id||null,reported_entry_id:editing?.payload.reported_entry_id||reported?.entry_id||null,
      reported_lexicon_release_id:editing?.payload.reported_lexicon_release_id||reported?.lexicon_release_id||null,
      context:editing?.payload.context||feedbackContext,kind:editing?.payload.kind||(mode==='incorrect'?'incorrect_mapping':'add_expression')};
    if(editing){data.expected_revision=editing.revision;await post('/search/feedback/'+editing.feedback_id+'/revision',data);}
    else {const receipt=await post(mode==='gap'?'/search/gaps':'/search/feedback',data);if(mode!=='gap'&&receipt.state!=='pending')notify('已记录补充，处理状态见“我的提交”');}
    dialog.close();if(mode==='gap')notify('已记录素材需求');else notify('已记录补充，处理状态见“我的提交”');if(!mySubmissions.hidden)await refreshFeedback();
  }));
  async function refreshFeedback(){
    if(getPerson()?.role==='reader')throw Error('只读账号没有提交记录');
    const data=await api('/search/feedback');mySubmissions.hidden=false;mySubmissions.replaceChildren(el('h3','我的提交'));
    const states={pending:'待审核',needs_info:'待补充',draft_created:'已进入草稿',rejected:'已拒绝',withdrawn:'已撤回'};
    for(const item of data.items){const card=el('div',undefined,'evidence-card');card.append(el('strong',item.payload.expression),el('p',`${states[item.state]||item.state} · 累计 ${item.occurrences} 次`));
      if(['pending','needs_info'].includes(item.state)){
        card.append(button('补充资料',async()=>{await openFeedback();editing=item;expression.value=item.payload.expression;quote.value=item.payload.quote;
          if(item.payload.target_id&&![...target.options].some(o=>o.value===item.payload.target_id)){const old=el('option','原版本目标（请核对后保留或重新选择）');old.value=item.payload.target_id;target.append(old);}
          target.value=item.payload.target_id||'';feedbackVersion=item.payload.bound_lexicon_release_id||null;submit.textContent='保存补充';}),
          button('撤回',async()=>{await post('/search/feedback/'+item.feedback_id+'/revision',{...item.payload,request_id:crypto.randomUUID(),expected_revision:item.revision,withdraw:true,
            lexicon_release_id:item.payload.bound_lexicon_release_id||null,bound_lexicon_release_id:undefined});await refreshFeedback();}));
      }mySubmissions.append(card);
    }
  }
  target.addEventListener('change',()=>{feedbackVersion=vocabulary?.versions?.lexicon_release_id||null;});
  function renderResult(data){
    result=data;summary.replaceChildren();if(!data)return;
    const resolution=data.resolution;const state={resolved:'按已发布术语检索',ambiguous:'需要选择或补充语境',no_match:'暂未识别确切对应',degraded:'术语服务已降级'}[resolution.state]||'';
    summary.append(el('p',state+(data.include_drafts?' · 当前包含未确认 AI 建议':''),'muted'));
    if(data.visual_drafts?.state==='ready_uncalibrated')summary.append(el('p','视觉候选按画面相似度排列，尚未校准，不表示医学分类或入片适用。','muted'));
    else if(data.visual_drafts?.state==='unavailable')summary.append(el('p','视觉查询暂不可用，仍显示原文和 OCR 初稿匹配。','muted'));
    const message=searchMessage(data);if(message)summary.append(el('p',message));
    const choices=el('div',undefined,'actions');
    for(const candidate of resolution.candidates||[]){const b=button(`${candidate.name}${candidate.path==='bge_candidate'?' · 模型候选':''}`,async()=>{
      selected=[candidate.concept_id];selectionVersion=resolution.versions?.lexicon_release_id;literal.checked=false;await refreshLibrary();});b.dataset.conceptId=candidate.concept_id;choices.append(b);}
    if(selected.length)choices.append(button('取消术语选择',async()=>{selected=[];selectionVersion=null;await refreshLibrary();}));
    if(resolution.state==='resolved'&&(resolution.candidates||[]).length)choices.append(button('对应不准确',()=>openFeedback('incorrect',resolution.candidates[0])));
    else for(const candidate of resolution.candidates||[])choices.append(button(`反馈“${candidate.name}”的对应`,()=>openFeedback('incorrect',candidate)));
    summary.append(choices);
    if(data.empty_reason==='recognized_no_visible_assets'&&getPerson()?.features.review&&getPerson()?.role!=='reader')summary.append(button('记录素材需求',()=>openFeedback('gap')));
    else if(data.empty_reason&&getPerson()?.role!=='reader')summary.append(button('补充这个说法',()=>openFeedback()));
    if(data.resolution.pending_publication)summary.append(el('p','新的词表仍待查询版本切换，当前结果绑定上一组有效版本。','muted'));
  }
  for(const node of [include,visual,literal,department,organ,polarity,direction])node.addEventListener('change',guard(async()=>{selected=[];selectionVersion=null;await refreshLibrary();}));
  async function search(query,mine,cursor){
    if(lastQuery!==query){selected=[];selectionVersion=null;lastQuery=query;}
    return post('/search/query',{request:{query,context:{departments:department.value?[department.value]:[],organs:organ.value?[organ.value]:[],
      polarity:polarity.value,direction:direction.value}},selected_concept_ids:selected,selection_lexicon_release_id:selectionVersion,
      include_drafts:include.checked,include_visual_drafts:visual.checked,literal_only:literal.checked,mine,limit:30,cursor});
  }
  async function reviewAsset(ctx){
    const generation=++reviewEpoch;conceptHost.replaceChildren();if(!getPerson()?.features.search)return;
    const data=await api('/search/assets/'+ctx.detail.asset_id+'/concepts');const words=await loadVocabulary(true);if(generation!==reviewEpoch||!words)return;
    conceptHost.append(el('hr'),el('h3','概念关联与片段语境'),el('p','使用上方勾选的实际观察记录。医学概念须先逐字段确认分类；方向须依据连续视频。','muted'));
    for(const link of data.links){const row=el('div',undefined,'evidence-card');row.append(el('strong',link.name),el('p',`${link.active?link.current?'当前有效':'词义或医学字段已变化，需重新复核':'已撤回'} · ${link.polarity==='negated'?'否定':'肯定'} · ${link.direction}`));
      if(link.active)row.append(button('撤回关联',async()=>{await post(`/search/assets/${data.asset_id}/concepts/${link.link_id}/revoke`,{request_id:crypto.randomUUID(),expected_revision:data.revision,source_sha256:data.source_sha256,note:'人工从标签复核页撤回此概念关联'});await ctx.refresh();}));conceptHost.append(row);
    }
    const con=select('link-concept',[['','选择已发布术语'],...words.concepts.map(c=>[c.concept_id,c.name])]);
    const pol=select('link-polarity',[['affirmed','肯定 / 存在'],['negated','否定 / 未出现']]);
    const dir=select('link-direction',[['unspecified','未指定方向'],['static','保持稳定'],['increasing','增加 / 加重'],['decreasing','减少 / 缓解'],['transition','转换']]);
    const noteInput=input('link-note');noteInput.maxLength=2000;
    const save=button('确认这条概念关联',async()=>{
      if(!con.value||!noteInput.value.trim())throw Error('请选择术语并填写本次依据说明');
      await post('/search/assets/'+data.asset_id+'/concepts',{request_id:crypto.randomUUID(),expected_revision:data.revision,source_sha256:data.source_sha256,
        lexicon_release_id:data.lexicon_release_id,concept_id:con.value,polarity:pol.value,direction:dir.value,observation_ids:ctx.getObservations(),note:noteInput.value});
      await ctx.refresh();notify('已记录这条人工关联；整行索引仍需单独复核');
    });save.id='save-concept-link';
    const warning=el('p',undefined,'muted');con.addEventListener('change',()=>{const selected=words.concepts.find(c=>c.concept_id===con.value);save.disabled=!!selected?.requires_medical_review&&!data.medical_reviewer;warning.textContent=save.disabled?'此术语需要指定医学复核权限。':'';});
    conceptHost.append(field('专业术语',con),field('肯定与否定',pol),field('变化方向',dir),field('本次关联依据',noteInput),warning,save);
  }
  async function assetSummary(data,panel,ctx){
    const generation=++assetEpoch;if(!getPerson()?.features.search||data.file_access!=='authorized')return;
    const linked=await api('/search/assets/'+data.asset_id+'/concepts');if(generation!==assetEpoch||!ctx.isCurrent())return;
    const host=el('div');host.append(el('h3','概念与命中依据'));
    const record=result?.items.flatMap(g=>g.versions).find(v=>v.asset_id===data.asset_id);
    const labels={literal_name_or_id:'原文名称 / ID',human_text:'人工字段原文',expanded_name:'按术语扩展名称（不是医学证据）',expanded_human_text:'按术语扩展人工文字',human_classification:'已逐字段确认的分类',human_concept:'明确人工概念关联',ai_draft:'未确认 AI 建议',ai_visual_draft:'视觉候选（未校准）'};
    for(const reason of record?.reasons||[])host.append(el('p',`${labels[reason.kind]||reason.kind}${reason.field?' · '+reason.field:''}`));
    for(const link of linked.links.filter(l=>l.active))host.append(el('p',`${link.name} · ${link.current?'当前关联':'需重新复核'} · ${link.polarity==='negated'?'否定':'肯定'}`));
    if(getPerson()?.features.review&&linked.can_edit)host.append(button('在标签复核页维护关联',ctx.openReview));panel.append(host);
    const draft=await api('/search/assets/'+data.asset_id+'/ai-draft');if(generation!==assetEpoch||!ctx.isCurrent())return;
    const draftHost=el('div');draftHost.append(el('h3','AI 标签初稿（未确认）'));
    const stageNames={ocr:'画中文字',face:'人脸抽样观察',clip:'视觉特征',visual_fields:'画面主体 / 表现形式 / 检索词',local_topics:'本地十字段初稿',audio_topics:'本地音频初稿',precision:'精细打标初稿'};
    const stageStates={ready:'已接入',visual_features_ready_classification_missing:'视觉检索可用；分类词表待补',not_requested:'未请求',unavailable:'结果不可用',not_applicable:'不适用（没有画面证据）'};
    for(const [name,state] of Object.entries(draft.stages))draftHost.append(el('p',`${stageNames[name]||name}：${stageStates[state]||state}`,'muted'));
    for(const label of [...draft.labels].sort((a,b)=>Number(b.kind==='field_suggestion')-Number(a.kind==='field_suggestion')).slice(0,100)){
      const short=value=>typeof value==='string'&&value.length?value.slice(0,12):'未提供';
      const contentHash=label.content_evidence_sha256||label.frame_sha256;
      const evidenceName=({image:'图像',audio:'音频',audio_segment:'音频',video:'视频',video_segment:'视频',frame:'帧'})[label.evidence_kind]||(label.frame_sha256&&!label.content_evidence_sha256?'帧':'内容');
      const line=el('details',undefined,'evidence-card');line.append(el('summary',`${label.field?label.field+'：':''}${label.text}`),el('p',`模型 ${short(label.model_revision)} · 证据 ${label.evidence_id||'未提供'} · ${evidenceName} SHA256 ${short(contentHash)}`));
      if(Number.isFinite(label.confidence))line.append(el('p',`模型分数 ${label.confidence.toFixed(3)}（不是医学概率）`));draftHost.append(line);
    }
    if(draft.labels.length>100)draftHost.append(el('p',`共 ${draft.labels.length} 条；页面显示前 100 条，完整记录可从批次初稿台账查看。`));
    for(const [field,value] of Object.entries(draft.field_states||{}))if(value.state!=='observed')draftHost.append(el('p',`${field}：${value.state==='no_match'?'模型候选不在模板选项内':value.state==='not_applicable'?'该字段不适用':'内容证据不足'}，保留空值。`,'muted'));
    for(const note of draft.uncertainties||[])draftHost.append(el('p',`模型记录的不确定性：${note}`,'muted'));
    draftHost.append(el('p','OCR 原文和人脸检测均为模型观察；未检出不证明无人脸，检测到也不表示打码完成。','muted'));panel.append(draftHost);
  }
  function reset(){epoch++;reviewEpoch++;assetEpoch++;taggingBusy=false;tagging.replaceChildren();vocabulary=null;result=null;selected=[];selectionVersion=null;editing=null;lastQuery='';summary.replaceChildren();mySubmissions.replaceChildren();mySubmissions.hidden=true;
    conceptHost.replaceChildren();expression.value='';quote.value='';target.replaceChildren();department.replaceChildren();organ.replaceChildren();polarity.value='unspecified';direction.value='unspecified';include.checked=true;visual.checked=false;literal.checked=false;aliasCheckbox.checked=false;dialog.close();wrapper.hidden=true;termsUI.reset();}
  return {search,results:renderResult,emptyMessage:()=>searchMessage(result),reviewAsset,assetSummary,reset,
    session(person){wrapper.hidden=!person.features.search;$('search').placeholder=person.features.search?'按常用说法、专业术语或原文查找':'按名称或素材 ID 查找';
      tagButton.hidden=!person.features.semantic_fields;
      aliasLabel.hidden=!person.features.search;aliasCheckbox.disabled=!person.features.search;
      for(const b of actions.querySelectorAll('button'))b.disabled=person.role==='reader';termsUI.session(person);if(person.features.search)loadVocabulary(true).then(fillOptions).catch(e=>notify(e.message));}};
}

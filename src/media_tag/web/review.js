// D06 management surface. Every write is a single explicit, hash-bound human action.
export function playbackWindow(start,end,origin){
  if(!Number.isFinite(start)||!Number.isFinite(end)||!Number.isFinite(origin)||start<0||end<=start)throw Error('缺少已校验的片段时间基准');
  return {start:start+origin,end:end+origin};
}
export function installReviewUI({api,post,notify,getPerson,onAsset}) {
  const $=id=>document.getElementById(id);
  const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
  const button=(text,action)=>{const n=el('button',text);n.type='button';n.addEventListener('click',guard(action));return n;};
  const guard=action=>async event=>{try{await action(event);}catch(error){notify(error.message);}};
  let epoch=0,detail=null,queue=[],selectedAssets=new Set(),selectedGaps=new Set(),observations=new Set(),gapData=null,editingGap=null,clipEnd=null;
  const id=()=>crypto.randomUUID();
  const status=s=>({ai_draft:'AI 初稿',needs_review:'需重新复核',confirmed:'已确认'}[s]||s);
  const valuesText=value=>value?.state==='observed'?value.values.join(';'):value?`${value.state} · ${value.reason}`:'尚无有效值';

  async function refresh(){
    if(!getPerson()?.features.review)return;
    const result=await api('/review/queue');queue=result.items;
    $('review-queue').replaceChildren();
    for(const item of queue){
      const row=el('div',undefined,'review-queue-row'),check=el('input');check.type='checkbox';check.checked=selectedAssets.has(item.asset_id);
      check.setAttribute('aria-label','选入导出 '+item.name);check.addEventListener('change',()=>check.checked?selectedAssets.add(item.asset_id):selectedAssets.delete(item.asset_id));
      row.append(check,button(`${item.name} · ${status(item.index_status)}`,()=>open(item.asset_id)));$('review-queue').append(row);
    }
    if(!queue.length)$('review-queue').append(el('p','暂无可编辑素材。共享只读权限不能修改标签。'));
    await refreshGaps();
  }

  async function open(assetId){
    const generation=++epoch;const result=await api('/review/assets/'+assetId);if(generation!==epoch)return;
    const changed=detail?.asset_id!==assetId;detail=result;clipEnd=null;if(changed)observations.clear();
    $('review-empty').hidden=true;$('review-editor').hidden=false;$('review-name').textContent=result.name;
    $('review-binding').textContent=`${assetId} · revision ${result.revision}\nSHA256 ${result.sha256}`;
    $('review-index-status').textContent=`索引：${status(result.index_status)}；源文件访问：${result.media.file_access}`;
    const container=$('review-media');container.replaceChildren();
    if(result.media.file_access==='authorized'){
      const kind=result.technical.kind,node=el(['image','gif','animation'].includes(kind)?'img':kind==='audio'?'audio':'video');
      node.src=`/ui/media/${assetId}?sha256=${result.sha256}`;
      if(node.tagName!=='IMG'){node.controls=true;node.preload='metadata';node.addEventListener('timeupdate',()=>{if(clipEnd!==null&&node.currentTime>=clipEnd){node.pause();clipEnd=null;}});}
      else node.alt='当前源文件';
      container.append(node);
    }else container.append(el('p','当前输出受隐私或生产状态限制，不能借标签复核预览绕过。'));
    if(['video','audio'].includes(result.technical.kind))container.append(el('p',`区间使用当前文件归一化秒；登记的共同起点为 ${result.technical.time_basis.normalization_origin_seconds??'未知'} 秒，定位时加回播放器源时间。`,'muted'));
    const provenance=el('details');provenance.append(el('summary','已登记来源记录（尚未核实）'));
    for(const record of result.source_records){const item=el('div',undefined,'evidence-card');item.append(el('p',`来源：${record.label||'未提供'}；原始页面：${record.reference||'未提供'}；署名：${record.attribution||'未提供'}；许可：未核实`));item.append(button('用作人工记录草稿',()=>{$('observation-kind').value='human_record';$('observation-reference').value=record.reference||'';$('observation-note').value=`依据已登记来源记录 ${record.source_id}，需人工核对后保存。`;}));provenance.append(item);}container.append(provenance);
    const oldField=$('review-field').value;$('review-field').replaceChildren();
    for(const field of result.fields.filter(f=>f.editable)){
      const option=el('option',field.name+(field.medical?' · 医学逐项确认':''));option.value=field.name;$('review-field').append(option);
    }
    if(result.fields.some(f=>f.name===oldField&&f.editable))$('review-field').value=oldField;
    $('observation-kind').value=['video','gif','animation'].includes(result.technical.kind)?'video_segment':result.technical.kind==='audio'?'audio_segment':'image';
    $('observation-end').value=String(result.technical.time_basis.duration_seconds??result.technical.duration_seconds??'');
    $('index-viewed').checked=false;$('confirm-index').disabled=!result.medical_reviewer;$('revoke-index').disabled=!result.medical_reviewer;
    renderObservations();renderField();
    if(onAsset)await onAsset({detail:result,getObservations:()=>[...observations],refresh:async()=>{await refresh();await open(assetId);}});
  }

  function renderObservations(){
    $('observation-list').replaceChildren(el('h4','本字段使用的观察依据（明确选择）'));
    for(const record of detail.observations){const label=el('label',undefined,'check'),check=el('input');check.type='checkbox';check.value=record.observation_id;check.checked=observations.has(record.observation_id);
      check.addEventListener('change',()=>check.checked?observations.add(record.observation_id):observations.delete(record.observation_id));
      label.append(check,document.createTextNode(`${record.kind} · ${record.note} · ${record.actor_id}`));$('observation-list').append(label);if(record.kind.endsWith('segment'))$('observation-list').append(button('播放此观察区间',()=>playInterval(record.start_seconds,record.end_seconds)));}
  }

  function playInterval(start,end){
    const player=$('review-media').querySelector('video,audio');if(!player)throw Error('当前格式没有连续媒体播放器');
    const range=playbackWindow(start,end,detail.technical.time_basis.normalization_origin_seconds);
    player.currentTime=range.start;clipEnd=range.end;player.play().catch(e=>notify(e.message));
  }

  async function recordObservation(){
    if(!detail)throw Error('先选择素材');const pinned=detail,kind=$('observation-kind').value;
    const result=await post(`/review/assets/${pinned.asset_id}/observations`,{request_id:id(),expected_revision:pinned.revision,source_sha256:pinned.sha256,
      kind,note:$('observation-note').value,viewed:$('observation-viewed').checked,reference:$('observation-reference').value||null,
      start_seconds:kind.endsWith('segment')?Number($('observation-start').value):null,end_seconds:kind.endsWith('segment')?Number($('observation-end').value):null});
    observations.add(result.observation_id);await open(pinned.asset_id);$('observation-viewed').checked=false;notify('已记录依据；尚未修改任何字段。');
  }

  function renderField(){
    if(!detail)return;const field=detail.fields.find(f=>f.name===$('review-field').value);if(!field)return;
    $('field-note').value='';$('field-policy').textContent=field.medical?'医学字段：需指定复核人逐项确认。'+(!field.options_available?'正式词表未发布；选项保持空白，可记录无匹配候选。':''):'保留建议与有效值的区别；修改不会批准文件或许可。';
    const effective=detail.effective_values.find(v=>v.field===field.name);
    $('effective-preview').replaceChildren(el('p','当前有效值：'+valuesText(effective?.value)));
    $('field-state').value=effective?.value.state||'observed';$('field-reason').value=effective?.value.reason||'';
    $('field-text').value=effective?.value.values?.join(';')||'';$('field-options').replaceChildren();
    const selection=['单选','多选'].includes(field.type);$('field-options').hidden=!selection;$('field-text').hidden=selection;
    $('field-options').multiple=field.type==='多选';
    if(selection){if(field.type==='单选'){const placeholder=el('option','请选择已发布选项');placeholder.value='';$('field-options').append(placeholder);}for(const item of field.options||[]){const option=el('option',item);option.value=item;option.selected=effective?.value.values?.includes(item)||false;$('field-options').append(option);}}
    $('field-options').disabled=selection&&!field.options_available;
    $('save-field').disabled=field.medical&&!detail.medical_reviewer;
    $('field-dynamic').checked=field.name==='过程/状态';
    const suggestions=$('field-suggestions');suggestions.replaceChildren(el('h4','AI / 工具建议（未写入有效值）'));
    for(const suggestion of detail.suggestions.filter(s=>s.field===field.name)){
      const card=el('div',undefined,'evidence-card');card.append(el('p',valuesText(suggestion.value)),el('p',`${suggestion.origin} · ${suggestion.producer_version} · ${suggestion.available?'来源当前有效':'来源已失效'}`,'hash'));
      for(const evidence of suggestion.evidence){
        card.append(el('p',`${evidence.kind} · ${evidence.start_seconds??''}～${evidence.end_seconds??''} 秒`,'muted'));
        if(evidence.kind==='frame'||evidence.kind==='image'){const image=el('img');image.src=evidence.content;image.alt='该建议的哈希校验证据';card.append(image);}
        if(evidence.start_seconds!==null)card.append(button('在源文件定位此证据',()=>playInterval(evidence.start_seconds,evidence.end_seconds??Math.min(detail.technical.time_basis.duration_seconds,evidence.start_seconds+.5))));
      }
      const accept=button('仅采用这条建议',()=>saveField('accept',suggestion)),reject=button('拒绝这条建议',()=>saveField('reject',suggestion));
      accept.disabled=!suggestion.available||field.medical&&!detail.medical_reviewer;reject.disabled=!suggestion.available||field.medical&&!detail.medical_reviewer;
      card.append(accept,reject);if(suggestion.decisions.length)card.append(el('p',`已记录 ${suggestion.decisions.length} 次人工决定`));suggestions.append(card);
    }
    const history=el('details');history.append(el('summary','此字段的人工决定历史'));for(const record of detail.decisions.filter(d=>d.field===field.name))history.append(el('p',`${record.created_at} · ${record.actor_id} · revision ${record.asset_revision} · ${record.decision} · ${valuesText(record.data.effective?.value)} · ${record.data.note}`,'hash'));suggestions.append(history);
  }

  async function saveField(decision='manual',suggestion=null){
    if(!detail)return;const pinned=detail,field=detail.fields.find(f=>f.name===$('review-field').value),state=$('field-state').value;
    let value=null;
    if(decision==='manual'){
      const values=['单选','多选'].includes(field.type)?Array.from($('field-options').selectedOptions,o=>o.value).filter(Boolean):[$('field-text').value];
      value=state==='observed'?{state,values}:{state,reason:$('field-reason').value};
    }
    await post(`/review/assets/${pinned.asset_id}/fields`,{request_id:id(),expected_revision:pinned.revision,source_sha256:pinned.sha256,
      field:field.name,decision,value,observation_ids:Array.from(observations),run_id:suggestion?.run_id||null,suggestion_id:suggestion?.suggestion_id||null,
      note:$('field-note').value,dynamic:$('field-dynamic').checked});
    await open(pinned.asset_id);await refresh();notify('已保存单个字段决定；如有版本冲突，请刷新并重新核对。');
  }

  async function confirm(decision){
    if(!detail)return;const pinned=detail;
    await post(`/review/assets/${pinned.asset_id}/confirmation`,{request_id:id(),expected_revision:pinned.revision,source_sha256:pinned.sha256,
      decision,full_source_viewed:$('index-viewed').checked,note:$('index-note').value});await open(pinned.asset_id);await refresh();notify('已记录索引决定；未改变隐私、许可或剪辑用途审核。');
  }

  function renderGapFields(values={}){
    $('gap-fields').replaceChildren();
    for(const field of gapData.fields.filter(f=>!f.controlled)){
      const label=el('label',field.name),selection=['单选','多选'].includes(field.type);const input=el(selection?'select':'textarea');input.dataset.gapField=field.name;input.dataset.type=field.type;
      if(selection){input.multiple=field.type==='多选';if(!input.multiple){const option=el('option','留空 / 未填写');option.value='';input.append(option);}for(const text of field.options||[]){const option=el('option',text);option.value=text;option.selected=values[field.name]?.values?.includes(text)||false;input.append(option);}input.disabled=field.options===null;}
      else input.value=values[field.name]?.values?.join(';')||'';
      if(['科室','器官/系统','过程/状态'].includes(field.name)&&getPerson()?.medical_review!=='available'){input.disabled=true;label.append(el('p','此分类字段需指定复核人；需求可先写在“需要的画面”。','muted'));}if(selection&&field.options===null)label.append(el('p','正式选项尚未发布，保留空白。','muted'));label.append(input);$('gap-fields').append(label);
    }
  }

  async function refreshGaps(){
    const initial=gapData===null;gapData=await api('/review/gaps');if(initial)renderGapFields();$('gap-list').replaceChildren();
    for(const gap of gapData.items){const row=el('div',undefined,'review-queue-row'),check=el('input');check.type='checkbox';check.checked=selectedGaps.has(gap.gap_id);check.setAttribute('aria-label','选入导出 '+gap.gap_id);check.addEventListener('change',()=>check.checked?selectedGaps.add(gap.gap_id):selectedGaps.delete(gap.gap_id));
      row.append(check,el('p',`${gap.gap_id} · ${gap.values['需要的画面']?.values?.join(';')||''} · ${gap.occurrences} 次`),button('编辑',()=>{editingGap=gap;renderGapFields(gap.values);$('save-gap').textContent='保存此缺口修订';}));$('gap-list').append(row);}
  }

  async function saveGap(event){
    event.preventDefault();const values={};
    for(const input of $('gap-fields').querySelectorAll('[data-gap-field]')){
      let selected=input.tagName==='SELECT'?Array.from(input.selectedOptions,o=>o.value).filter(Boolean):input.dataset.type==='关联'?input.value.split(';').map(v=>v.trim()).filter(Boolean):input.value?[input.value]:[];
      if(selected.length)values[input.dataset.gapField]={state:'observed',values:selected};
    }
    const spec={request_id:id(),values,note:$('gap-note').value};
    if(editingGap){spec.expected_revision=editingGap.revision;await post('/review/gaps/'+editingGap.gap_id,spec);}else await post('/review/gaps',spec);
    editingGap=null;$('save-gap').textContent='登记 / 累计缺口';await refreshGaps();renderGapFields();notify('缺口已记录；同一请求重试不会重复累计次数。');
  }

  function exportLinks(result){
    $('export-links').replaceChildren(el('p','快照：'+result.export_id,'hash'));
    for(const [file,title] of [['assets.csv','素材表 CSV'],['gaps.csv','缺口表 CSV'],['quality.json','质量报告'],['manifest.json','快照哈希清单']]){
      const link=el('a',title,'button');link.href=`/review/exports/${result.export_id}/content/${file}`;$('export-links').append(link);
    }
  }

  async function createExport(){
    const assets=queue.filter(r=>selectedAssets.has(r.asset_id)).map(r=>({asset_id:r.asset_id,expected_revision:r.revision,source_sha256:r.sha256}));
    const gaps=gapData.items.filter(r=>selectedGaps.has(r.gap_id)).map(r=>({gap_id:r.gap_id,expected_revision:r.revision}));
    if(!assets.length&&!gaps.length)throw Error('请明确勾选要导出的素材或缺口');
    const result=await post('/review/exports',{request_id:id(),assets,gaps,include_ai_drafts:$('export-ai').checked,include_confirmed_aliases:$('export-aliases')?.checked||false});exportLinks(result);notify('两张 CSV 和质量报告已完整校验并发布。');
  }

  function reset(){epoch++;detail=null;queue=[];selectedAssets.clear();selectedGaps.clear();observations.clear();editingGap=null;gapData=null;clipEnd=null;
    for(const name of ['review-media','review-queue','field-suggestions','effective-preview','observation-list','export-links','gap-fields','gap-list'])$(name).replaceChildren();
    for(const input of $('review').querySelectorAll('input,textarea')){if(input.type==='checkbox')input.checked=false;else input.value='';}
    $('review-editor').hidden=true;$('review-empty').hidden=false;$('review-nav').hidden=true;
  }
  $('review-nav').addEventListener('click',guard(refresh));$('refresh-review').addEventListener('click',guard(refresh));$('review-field').addEventListener('change',renderField);
  $('record-observation').addEventListener('click',guard(recordObservation));$('save-field').addEventListener('click',guard(()=>saveField()));
  $('confirm-index').addEventListener('click',guard(()=>confirm('confirm')));$('revoke-index').addEventListener('click',guard(()=>confirm('revoke')));
  $('gap-form').addEventListener('submit',guard(saveGap));$('new-gap').addEventListener('click',()=>{editingGap=null;renderGapFields();$('save-gap').textContent='登记 / 累计缺口';});
  $('create-export').addEventListener('click',guard(createExport));$('latest-export').addEventListener('click',guard(async()=>exportLinks(await api('/review/exports/latest'))));
  return {open,session(person){$('review-nav').hidden=!person.features.review||person.role==='reader';},reset};
}

import {estimateFeedback,setEstimateButton} from './estimate-ui.js';
import {api,post,esc,money,toast} from './base.js';
import {duration} from './usage.js';

import {estimateGate} from './estimate-policy.js';

export function bindCreationFlow(form,{getAssets,onCreate}){
 const settings=form.querySelector('#task-settings'),confirmation=form.querySelector('#task-confirmation'),box=form.querySelector('#usage-estimate');
 const estimateButton=form.querySelector('#estimate-usage'),createButton=form.querySelector('#create-task'),back=form.querySelector('#back-settings'),error=form.querySelector('#estimate-error');
 const assetPanel=document.querySelector('#new-assets');
 let generation=0,busy=false,submitting=false,confirmed=null;
 const ids=()=>[...new Set(getAssets().filter(a=>a.status==='ready').map(a=>a.asset_id))];
 const ready=()=>getAssets().length>0&&getAssets().every(a=>a.status==='ready'&&a.asset_id);
 const budget=()=>Math.round(+form.budget.value*1e6);
 const fingerprint=()=>JSON.stringify({assets:getAssets().map(a=>[a.asset_id,a.uid,a.status]),fields:['title','pref','requirement','budget','hold'].map(k=>form.elements[k].value),best:form.best.checked});
 const current=()=>confirmed&&confirmed.key===fingerprint();
 function focusWithoutJump(node){
  node.focus({preventScroll:true});
  const rect=node.getBoundingClientRect();
  if(rect.top<16||rect.bottom>innerHeight-16)node.scrollIntoView({block:'nearest',behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth'});
 }
 function sync(){
  const confirm=!!confirmed;
  // Preserve page length before hiding the taller settings panel. Otherwise
  // the browser clamps scrollY immediately when its document becomes shorter.
  if(settings.hidden!==confirm)form.style.minHeight=form.getBoundingClientRect().height+'px';
  settings.hidden=confirm;confirmation.hidden=!confirm;
  estimateButton.disabled=busy||!ready()||!form.checkValidity();
  setEstimateButton(estimateButton,busy,busy?'预估中…':'查看预估');
  back.disabled=submitting;
  createButton.disabled=submitting||!current()||!ready()||estimateGate(confirmed?.result,budget())!=='ready';
  createButton.textContent=submitting?'正在创建…':'创建任务';
  assetPanel.inert=submitting;settings.inert=submitting;
 }
 function invalidate(message=''){
  generation++;confirmed=null;busy=false;error.hidden=!message;error.textContent=message;sync();
 }
 async function estimate(){
  if(busy||!form.reportValidity())return;
  if(!ready()){toast('请先完成素材上传，或移除失败素材');return}
  const n=++generation,key=fingerprint();busy=true;error.hidden=true;sync();
  try{
   const response=await estimateFeedback(async()=>{
    const setup=await api('/api/v1/workspace/setup');
    if(n!==generation||!form.isConnected||key!==fingerprint())return null;
    if(!setup.model_config_ref||!setup.real_ready)throw Error('真实分析尚未准备好，请先在管理设置中检查模型配置。');
    const result=await post('/api/v1/workspace/estimate',{asset_ids:ids(),model_config_ref:setup.model_config_ref,budget_minor:budget()});
    return {setup,result};
   });
   if(!response||n!==generation||!form.isConnected||key!==fingerprint())return;
   const {setup,result}=response;
   const gate=estimateGate(result,budget());
   confirmed={key,result,config:result.model_config_ref,model:setup.model_name};
   const message=gate==='ready'?'当前预算覆盖预计费用。达到预算会暂停新请求，保留已完成内容。':gate==='insufficient'?'预算不足，请返回调整预算或减少素材，再重新预估。':'暂时无法可靠预估，请返回检查素材与配置，或联系管理员。';
   box.innerHTML=`<div class="creation-overview"><span>${getAssets().length}个素材</span><span>${esc(form.pref.selectedOptions[0]?.textContent||'默认偏好')}</span></div><div class="estimate-grid"><div><small>预计费用范围</small><strong>${result.cost_low_minor!=null&&result.cost_high_minor!=null?money(result.cost_low_minor)+' — '+money(result.cost_high_minor):'暂无可靠估算'}</strong></div><div><small>预计处理时间</small><strong>${result.seconds_low!=null&&result.seconds_high!=null?duration(result.seconds_low)+' — '+duration(result.seconds_high):'暂无可靠估算'}</strong></div></div><div class="creation-budget"><span>本次预算上限</span><strong>${money(budget())}</strong></div><div class="notice ${gate==='ready'?'':'warn'}" role="status">${message}</div><p class="creation-basis">${result.sample_count?'参考同配置的 '+esc(result.sample_count)+' 个已完成任务，实际费用和时间可能变化。':'当前配置没有足够的历史记录，暂时无法可靠预估。'}</p><details class="estimate-details"><summary>预估依据</summary><p>${esc(result.basis)}</p>${setup.model_name?`<p>使用模型：${esc(setup.model_name)}</p>`:''}<p>以上为参考范围，不是报价或完成保证；预估不调用模型，也不占用预算。</p></details>`;
  }catch(e){if(n===generation&&form.isConnected){error.textContent='预估未完成：'+e.message;error.hidden=false}}
  finally{if(n===generation){busy=false;sync();if(confirmed)focusWithoutJump(confirmation.querySelector('h2'))}}
 }
 estimateButton.onclick=estimate;
 back.onclick=()=>{const insufficient=estimateGate(confirmed?.result,budget())==='insufficient';invalidate();focusWithoutJump(insufficient?form.budget:form.title)};
 form.addEventListener('input',()=>invalidate());
 form.addEventListener('change',()=>invalidate());
 form.onsubmit=async e=>{
  e.preventDefault();if(submitting)return;
  if(!confirmed){await estimate();return}
  if(!current()||!ready()){invalidate('素材或设置已变化，请重新查看预估。');return}
  if(estimateGate(confirmed.result,budget())!=='ready')return;
  submitting=true;sync();
  try{
   const latest=await api('/api/v1/workspace/setup');
   if(!form.isConnected)return;
   if(JSON.stringify(latest.model_config_ref)!==JSON.stringify(confirmed.config)||!latest.real_ready){invalidate('模型配置已变化，请重新查看预估。');return}
   if(!current()||!ready()){invalidate('素材或设置已变化，请重新查看预估。');return}
   await onCreate(confirmed.config);
  }catch(e){toast(e.message)}finally{submitting=false;if(form.isConnected)sync();else if(assetPanel)assetPanel.inert=false}
 };
 sync();return {invalidate};
}

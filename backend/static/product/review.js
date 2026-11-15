import {estimateFeedback,estimateLabel,setEstimateButton,estimateLoadingHTML} from './estimate-ui.js';
import {settingsHTML,bindSettings} from './task-settings.js';
import {estimateGate} from './estimate-policy.js';
import {processingHTML,estimateHTML} from './usage.js';
import {galleryItems} from './selection.js';
import {$,esc,money,seconds,date,label,badge,api,post,btn,imageURL,empty,modal,closeModal,toast,actions} from './base.js';
let state=null,timer,read=0,estimateDialogEpoch=0;
export function stopReview(){clearTimeout(timer);document.querySelector('#main')?.classList.remove('has-review-bar');state=null;read++}
const originalURL=c=>'/api/v1/workspace/tasks/'+state.id+'/assets/'+c.asset_id+'/original';
function all(){return [...new Map([...(state.view.photos||[]),...(state.view.video_candidates||state.view.video_segments||[]),...(state.view.video_fragments||[])].map(c=>[c.candidate_id,c])).values()]}
function find(id){const c=all().find(c=>c.candidate_id===id);if(!c)throw Error('候选已经变化，请重新加载任务');return c}
function pendingRecommendations(){const candidates=new Map(all().map(c=>[c.candidate_id,c]));return [...new Map([...(state.view.recommended_photos||[]),...(state.view.video_segments||[])].map(c=>[c.candidate_id,c])).values()].filter(c=>!(candidates.get(c.candidate_id)||c).review_decision).length}
function kept(){return [...(state.view.final_recommended_photos||[]).map(c=>({...c,kind:'photo'})),...(state.view.final_video_segments||[]).map(c=>({...c,kind:'video'}))]}
const decision=c=>({keep:'已保留',reject:'已淘汰',trim:'待修剪'}[c.review_decision]||'尚未审核');
const rawReason=c=>c.source_type==='mock'?'模拟示例，仅用于体验操作流程，不代表选片效果。':c.photo_check?.reason||c.segment_eval||c.reason||c.caption||c.boundary_note||'暂未提供具体选择理由，请查看作品后确认。';
function reason(c){const value=rawReason(c);return typeof value==='string'&&/^(连续好看较密集|连续好看密集)[。！]?$/.test(value.trim())?'根据抽样画面推荐，请播放确认。':typeof value==='string'?value:'暂未提供具体选择理由，请播放确认。'}
function body(c,kind,payload={}){return {asset_id:c.asset_id,run_id:c.run_id||null,kind,payload:{candidate_id:c.candidate_id,...payload}}}
export async function openReview(id,section,epoch,alive){if(!id)throw Error('请从精选空间选择任务');const [progress,view,setup,options]=await Promise.all([api('/api/v1/workspace/tasks/'+id+'/progress'),api('/api/v1/tasks/'+id+'/results'),api('/api/v1/workspace/setup'),api('/api/v1/workspace/tasks/'+id+'/settings')]);if(!alive())return;state={id,progress,view,setup,options,alive,tab:['photos','videos','deliver'].includes(section)?section:'photos',filter:progress.status==='completed'?'keep':'all',alternatives:false,compare:new Set(),busy:new Set(),saveStatus:'saved',meta:new Map(),page:0};draw();watch();if(freshTask(progress))loadStartEstimate();}
async function refresh({candidateId=null}={}){
 const s=state,n=++read;if(!s)return;
 const [progress,view,options]=await Promise.all([api('/api/v1/workspace/tasks/'+s.id+'/progress'),api('/api/v1/tasks/'+s.id+'/results'),api('/api/v1/workspace/tasks/'+s.id+'/settings')]);
 if(state!==s||n!==read)return;
 const previousStatus=s.progress.status;
 const budgetChanged=s.progress.budget.limit!==progress.budget.limit;
 const sameSettings=s.options?.revision===options.revision;
 const budgetOnly=budgetChanged&&sameSettings&&freshTask(s.progress)&&progress.title===s.progress.title&&!s.editingSettings;
 s.progress=progress;s.view=view;s.options=options;
 if(budgetChanged){s.estimateError=null}
 if(budgetOnly&&freshTask(progress)&&$('#task-progress .task-ready')){
  $('#task-progress .ready-budget strong').textContent=progress.budget.limit==null?'尚未设置':money(progress.budget.limit);
  const settingBudget=$('#task-budget-value');if(settingBudget)settingBudget.textContent=progress.budget.limit==null?'尚未设置':money(progress.budget.limit);
  loadStartEstimate();return;
 }
 if(candidateId&&sameSettings&&previousStatus===progress.status&&s.tab!=='deliver'){
  const candidates=list();
  const page=Math.min(s.page,Math.max(0,Math.ceil(candidates.length/12)-1));
  const visible=candidates.slice(page*12,page*12+12);
  const cards=[...document.querySelectorAll('#works [data-candidate]')];
  if(page===s.page&&cards.map(el=>el.dataset.candidate).join('|')===visible.map(c=>c.candidate_id).join('|')){
   const card=cards.find(el=>el.dataset.candidate===candidateId),candidate=visible.find(c=>c.candidate_id===candidateId);
   if(card&&candidate){const template=document.createElement('template');template.innerHTML=work(candidate,s.tab==='photos');card.replaceWith(template.content.firstElementChild);bindComparisons();alignCardReasons()}
  }else drawGallery();
  paintWorkCounts();
  return;
 }
 if(!sameSettings){s.startEstimate=null}
 draw();if(freshTask(progress)&&(!s.startEstimate||budgetChanged))loadStartEstimate();
}
function draw(){if(!state)return;const s=state,p=s.progress,locked=p.status==='completed',n=kept().length,preparing=freshTask(p),hideWorks=preparing&&!all().length&&!n;$('#main').innerHTML=`${taskHeadingHTML(p)}<div class="stepper"><span class="${preparing?'current':''}">1 素材就绪</span><span class="${!preparing&&['analyzing','assets_locked','failed','pending_reconciliation'].includes(p.status)?'current':''}">2 处理与精选</span><span class="${s.tab!=='deliver'&&['ready_for_review','completed'].includes(p.status)?'current':''}">3 挑选作品</span><span class="${s.tab==='deliver'?'current':''}">4 查看成果</span></div>${s.tab!=='deliver'?`<div id="task-progress">${progressHTML()}</div>${taskOptionsHTML(p)}`:''}${hideWorks?'':`${s.tab!=='deliver'?`<div class="tabs" aria-label="作品类型"><button data-act="tab" data-id="photos" aria-pressed="${s.tab==='photos'}">照片精选 <small> ${list('photos').length}</small></button><button data-act="tab" data-id="videos" aria-pressed="${s.tab==='videos'}">视频精选 <small> ${list('videos').length}</small></button></div>`:''}${locked&&s.tab!=='deliver'?'<div class="notice">审核已确认。需改变决定时请先重新打开；历史交付版本会保留。 '+btn('修改选择','reopen','','small')+'</div>':''}<div id="works"></div>${s.tab!=='deliver'?reviewBarHTML():''}`}`;
 $('#main').classList.toggle('has-review-bar',!hideWorks&&s.tab!=='deliver');
 bindTaskTitle();bindSettings(s,{draw,refresh});if(hideWorks)return;
 if(s.tab==='deliver')renderDelivery().catch(e=>{if($('#works'))$('#works').innerHTML=empty('成果暂时无法加载',e.message,btn('重试','refresh'))});else drawGallery();}
function freshTask(p){return state?.options?.can_edit!==false&&p.status==='assets_locked'&&!p.job&&!p.source_hold&&!p.error&&!p.budget.settled&&!p.budget.reserved&&!p.budget.unknown}
function taskHeadingHTML(p){return `<div class="page-heading task-heading"><div class="task-title-row">${state.renaming?`<label class="sr-only" for="task-title-input">任务名称</label><input id="task-title-input" maxlength="100" value="${esc(state.renameDraft)}"><button data-act="save-title" class="primary small">保存</button><button data-act="cancel-title" class="quiet small">取消</button>`:`<h1><button class="task-title-name" data-act="rename-task" title="修改任务名称">${esc(p.title)}</button></h1><button class="quiet small task-title-edit" data-act="rename-task" aria-label="修改任务名称" title="修改任务名称"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="m15 5 4 4M4 20l4-1 12-12a2.8 2.8 0 0 0-4-4L4 15Z"/></svg></button>`}</div>${state.renameError?`<p class="notice error" role="alert">${esc(state.renameError)}</p>`:''}<p class="muted">${p.total_assets}个素材 · ${date(p.created_at)} ${p.job?.mode==='mock'?'<span class="badge warn">模拟体验 · 不代表自动选片效果</span>':''} ${p.experimental_quality?'<span class="badge warn">实验质检 · 真实效果未通过</span>':''}</p></div>`}
function bindTaskTitle(){const input=$('#task-title-input');if(!input)return;input.oninput=()=>{state.renameDraft=input.value};input.onkeydown=e=>{if(e.isComposing)return;if(e.key==='Escape'){e.preventDefault();actions['cancel-title']()}if(e.key==='Enter'){e.preventDefault();document.querySelector('[data-act="save-title"]').click()}}}
async function loadStartEstimate({notify=false}={}){
 const s=state;if(!s||s.estimateLoading)return;
 const budget=s.progress.budget.limit,revision=s.options?.revision;
 s.estimateLoading=true;s.estimateError=null;paintProgress();
 try{
  const r=await estimateFeedback(()=>api('/api/v1/workspace/tasks/'+s.id+'/estimate'));
  if(state===s&&s.progress.budget.limit===budget&&s.options?.revision===revision){s.startEstimate=r;if(notify)toast('预估已更新',2000)}
 }catch(e){if(state===s&&s.progress.budget.limit===budget&&s.options?.revision===revision)s.estimateError=e.message}
 finally{if(state===s){s.estimateLoading=false;if(freshTask(s.progress)&&(s.progress.budget.limit!==budget||s.options?.revision!==revision)){loadStartEstimate();return}paintProgress()}}
}
function canStartEstimate(s){return !s.editingSettings&&s.startEstimate&&estimateGate(s.startEstimate,s.progress.budget.limit)==='ready'&&s.progress.real_ready&&!s.estimateLoading&&!s.estimateError}
function readyEstimateHTML(s){
 const e=s.startEstimate,gate=estimateGate(e,s.progress.budget.limit);
 return `${e?estimateHTML(e,{runtimeNote:'处理期间可离开页面，稍后从精选空间回来查看；请保持电脑唤醒与服务运行。'}):s.estimateLoading?estimateLoadingHTML():'<p class="hint">尚未取得预估，请重新预估后开始。</p>'}${s.estimateError?`<div class="notice error" role="alert">预估未完成：${esc(s.estimateError.replace(/[。.!?！？]+$/,''))}。${e?'上方保留的是上次结果，请重新预估成功后开始处理。':'请重新预估后开始。'}</div>`:''}${e&&gate==='insufficient'?`<p class="notice warn">预算不足以覆盖预计费用上限，请调整预算或在下方“修改设置”中减少素材。 ${btn('调整预算','budget','','quiet small')}</p>`:''}${e&&gate==='unknown'?'<p class="notice warn">暂不能可靠预估，当前不开始付费处理，请管理员检查配置后重新预估。</p>':''}${!s.progress.real_ready?'<p class="notice warn">真实分析尚未配置，请在管理设置中检查。</p>':''}`;
}
function paintProgress(){
 const host=$('#task-progress');if(!host)return;
 const s=state,ready=host.querySelector('.task-ready');
 if(!ready||!freshTask(s.progress)){const open=[...host.querySelectorAll('details')].map(el=>el.open);host.innerHTML=progressHTML();host.querySelectorAll('details').forEach((el,i)=>{el.open=!!open[i]});return}
 const result=host.querySelector('#ready-estimate-content');
 if(!s.estimateLoading||!s.startEstimate){
  const open=[...result.querySelectorAll('details')].map(el=>el.open);
  result.innerHTML=readyEstimateHTML(s);
  result.querySelectorAll('details').forEach((el,i)=>{el.open=!!open[i]});
 }
 const refresh=host.querySelector('[data-act="refresh-start-estimate"]');
 setEstimateButton(refresh,s.estimateLoading,s.estimateLoading?'预估中…':'重新预估');
 refresh.disabled=!!s.estimateLoading||!!s.editingSettings;
 const start=host.querySelector('[data-act="analyze"][data-id="real"]');
 if(s.estimateLoading&&!start.disabled)start.dataset.estimateGuard='true';
 if(!s.estimateLoading)delete start.dataset.estimateGuard;
 start.disabled=!canStartEstimate(s);

}
actions['refresh-start-estimate']=()=>{if(state.editingSettings){toast('请先保存或取消设置修改，再按已保存内容预估');return}return loadStartEstimate({notify:true})};
function startHTML(){
 const s=state,p=s.progress;
 return `<section class="card task-ready"><h2>准备开始</h2><p class="muted">素材已保存。确认费用与时间后，即可开始处理。</p><div class="row between ready-budget"><span>本次预算上限 <strong>${p.budget.limit==null?'尚未设置':money(p.budget.limit)}</strong></span></div><div id="ready-estimate-content">${readyEstimateHTML(s)}</div><div class="row ready-actions">${btn('开始处理','analyze','real','primary estimate-start').replace('<button ',canStartEstimate(s)?'<button ':'<button disabled ')}<button class="quiet small estimate-trigger" data-act="refresh-start-estimate" aria-busy="${!!s.estimateLoading}" ${s.estimateLoading||s.editingSettings?'disabled':''}>${estimateLabel(s.estimateLoading?'预估中…':'重新预估',!!s.estimateLoading)}</button></div><details class="ready-advanced"><summary>高级选项</summary><div class="ready-advanced-content">${btn('模拟体验（不付费）','analyze','mock','small')}<p class="hint">模拟仅用于体验操作，不代表真实 AI 处理效果。</p></div></details></section>`;
}
function taskOptionsHTML(p){return settingsHTML(state)}
function progressHTML(){if(freshTask(state.progress))return startHTML();const p=state.progress,b=p.budget,working=['analyzing','selecting','exporting'].includes(p.status),needs=!!p.source_hold||p.status==='pending_reconciliation'||b.unknown>0;
 const content=`<section class="card processing-record"><div class="row progress-heading"><h2>${label(needs?'pending_reconciliation':p.status==='ready_for_review'||p.status==='completed'?'complete':p.status)}</h2>${p.outcome&&p.outcome!=='complete'?badge(p.outcome,p.outcome==='complete'?'处理完成':undefined):''}</div>${working?processingHTML(p):''}${needs?`<div class="notice warn">${esc(p.source_hold||'有请求结果或费用尚不确定，已暂停后续请求。')} 已完成成果仍保留，可继续审核与导出。请管理员先核对供应商账单。</div>`:''}${p.outcome==='incomplete_budget'?'<div class="notice warn">预算不足，未处理范围如下。可增加本任务预算后从断点继续，不会清除已完成成果。</div>':''}${p.error?`<div class="notice error">${esc(p.error.message||p.error.error||p.error.code||'处理未完成，请检查原因后继续')}</div>`:''}${p.outcome==='none_qualified'?'<div class="notice warn">当前标准下没有找到合格成果，不会把相对最好冒充宣传合格。可查看备选、调整偏好后新建重选任务。</div>':''}${p.outcome==='partial'?'<div class="notice warn">本次只产出部分类型的合格内容。已有照片或视频可继续审核，不代表全部处理失败。</div>':''}${p.outcome==='incomplete_quality'?'<div class="notice error">部分质量检查没有完成；现有分析不能当作完整精选。请查看处理范围并修复后继续。</div>':''}<h3 class="record-section-title">费用情况</h3><div class="costs"><span>已用费用<strong>${money(b.settled)}</strong></span><span>在途预留<strong>${money(b.reserved)}</strong></span><span>未知费用<strong>${money(b.unknown)}</strong></span><span>预算上限<strong>${b.limit==null?'尚未设置':money(b.limit)}</strong></span></div>${p.job?.mode==='mock'?'<div class="hint">模拟费用记录仅用于验证预算流程，不是实际付款。</div>':''}<section class="coverage-group"><h3 class="record-section-title">素材处理情况</h3>${p.assets.map(a=>`<div class="asset-coverage"><div class="asset-status"><span class="filename">${esc(a.name)}</span>${badge(a.status)}</div>${a.coverage_plan?.coarse?.planned_batches?`<p class="hint">画面初筛：已完成 ${a.coverage_plan.coarse.batches?.length||0}/${a.coverage_plan.coarse.planned_batches}批。</p>`:''}${a.coverage_plan?`<p class="hint">细查与抽查计划 ${a.coverage_plan.checked?.length||0}/${a.coverage_plan.planned?.length||0}个窗口已完成。</p>`:''}</div>`).join('')}<p class="hint coverage-note">采样检查不代表逐帧检查，最终请结合实际作品挑选。</p></section><div class="row">${p.status==='assets_locked'||p.status==='failed'?(!needs?`${p.real_ready?btn(p.status==='failed'?'从断点继续真实处理':'开始真实挑选','analyze','real','primary'):'<span class="hint">真实分析尚未配置，管理员准备后可开始。</span>'}${btn('预估费用与时间','estimate-task','','small')}${btn('模拟体验（不付费）','analyze','mock','small')}`:''):''}${working?btn('取消后续处理','cancel-task','','danger'):''}${p.outcome==='incomplete_budget'&&!needs?btn('调整预算','budget','','small'):''}</div></section>`;
 if(working||needs||p.outcome&&p.outcome!=='complete'||['assets_locked','failed'].includes(p.status))return content;
 return `<details class="card progress-summary"><summary><strong>处理记录</strong> · 已完成${p.completed_assets}/${p.total_assets}个素材 · 已用${money(b.settled)}</summary>${content}</details>`;
}
async function watch(){clearTimeout(timer);const s=state;if(!s)return;const working=['analyzing','selecting','exporting','pending_reconciliation'].includes(s.progress.status);if(!working)return;timer=setTimeout(async()=>{if(state!==s)return;try{const p=await api('/api/v1/workspace/tasks/'+s.id+'/progress');if(state!==s)return;const changed=p.status!==s.progress.status||p.completed_assets!==s.progress.completed_assets||p.outcome!==s.progress.outcome;s.progress=p;if(changed)await refresh();else paintProgress()}catch(e){if(state===s){$('#task-progress .progress-read-error')?.remove();$('#task-progress').insertAdjacentHTML('afterbegin',`<div class="notice error progress-read-error">${esc(e.message)} ${btn('重试连接','refresh','','small')}</div>`)}}finally{if(state===s)watch()}},2500)}
function list(kind=state.tab){return galleryItems(state.view,kind,state.alternatives,state.filter)}
function paintWorkCounts(){for(const kind of ['photos','videos','deliver']){const count=document.querySelector('.tabs [data-id="'+kind+'"] small');if(count)count.textContent=' '+(kind==='deliver'?kept().length:list(kind).length)}}
function drawGallery(){const s=state,photos=s.tab==='photos';const matched=list();paintWorkCounts();s.page=Math.min(s.page,Math.max(0,Math.ceil(matched.length/12)-1));const items=matched.slice(s.page*12,s.page*12+12);$('#works').innerHTML=`<div class="toolbar review-toolbar"><div class="review-visibility-group"><label class="check review-visibility"><input id="show-alternatives" type="checkbox" ${s.alternatives?'checked':''}><span>显示备选${photos?'照片':'片段'}</span></label><span class="inline-help"><button type="button" class="help-trigger" aria-label="了解备选展示" aria-describedby="alternatives-help"><svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" aria-hidden="true" focusable="false"><circle cx="8" cy="8" r="6.25"/><path d="M6.4 6.1a1.7 1.7 0 0 1 3.3.5c0 1.2-1.7 1.3-1.7 2.5"/><circle cx="8" cy="11.3" r=".55" fill="currentColor" stroke="none"/></svg></button><span id="alternatives-help" class="help-tooltip" role="tooltip">推荐优先，备选按需展开。</span></span></div><div class="review-tools"><select id="decision-filter" aria-label="筛选审核决定">${[['all','全部状态'],['unreviewed','尚未审核'],['keep','已保留'],['reject','已淘汰'],...(!photos?[["trim","待修剪"]]:[])] .map(([v,n])=>`<option value="${v}" ${s.filter===v?'selected':''}>${n}</option>`).join('')}</select>${photos?`<span class="compare-control" title="${s.compare.size===2?'并排查看所选两张照片':'请先勾选两张照片'}">${btn('对比照片','compare','','small').replace('<button ',s.compare.size===2?'<button ':'<button disabled ')}</span>`:''}</div></div>${matched.length>12?`<p class="hint">共${matched.length}项，当前显示第${s.page*12+1}–${Math.min((s.page+1)*12,matched.length)}项。可逐页挑选，已保存的选择不会丢失。</p>`:''}${items.length?`<div class="gallery">${items.map(c=>work(c,photos)).join('')}</div>${matched.length>12?`<nav class="gallery-pages" aria-label="作品翻页">${btn('上一页','review-page',s.page-1,'small').replace('<button ',s.page?'<button ':'<button disabled ')}<span aria-live="polite">第${s.page+1}/${Math.ceil(matched.length/12)}页</span>${btn('下一页','review-page',s.page+1,'small').replace('<button ',(s.page+1)*12<matched.length?'<button ':'<button disabled ')}</nav>`:''}`:empty(photos?'这里还没有照片':'这里还没有视频',s.filter!=='all'?'当前筛选条件下没有作品，切换状态查看其他内容。':s.progress.status==='assets_locked'?'开始处理后，照片和视频会分别出现在这里。':['analyzing','selecting','exporting'].includes(s.progress.status)?'正在处理这一批素材，已完成检查的照片和视频会自动显示。可在上方查看已处理范围与费用。':'当前没有可展示的推荐；可展开备选或查看上方处理范围。')}`;$('#show-alternatives').onchange=e=>{s.alternatives=e.target.checked;s.page=0;drawGallery()};$('#decision-filter').onchange=e=>{s.filter=e.target.value;s.page=0;drawGallery()};bindComparisons();alignCardReasons();}
function bindComparisons(){const s=state;document.querySelectorAll('[data-compare]').forEach(el=>el.onchange=()=>{if(el.checked&&s.compare.size>=2){el.checked=false;toast('一次比较两张照片，请先取消一张');return}el.checked?s.compare.add(el.dataset.compare):s.compare.delete(el.dataset.compare);const b=document.querySelector('[data-act=compare]');b.textContent='对比照片';b.disabled=s.compare.size!==2;b.parentElement.title=s.compare.size===2?'并排查看所选两张照片':'请先勾选两张照片'});}
// Match only the actual text height of neighbours in the same visual row.
function alignCardReasons(){
 const cards=[...document.querySelectorAll('#works .work')];
 cards.forEach(card=>{const el=card.querySelector('.reason');if(el)el.style.height='auto'});
 const rows=new Map();
 cards.forEach(card=>{const top=Math.round(card.getBoundingClientRect().top),el=card.querySelector('.reason');if(!el)return;const row=rows.get(top)||[];row.push(el);rows.set(top,row)});
 rows.forEach(row=>{const height=Math.max(...row.map(el=>el.getBoundingClientRect().height));row.forEach(el=>el.style.height=height+'px')});
}
let reasonResize;
window.addEventListener('resize',()=>{clearTimeout(reasonResize);reasonResize=setTimeout(alignCardReasons,100)});

function reviewBarHTML(){
 const s=state,status=s.saveStatus||'saved',text={saved:'选择已保存',saving:'正在保存…',error:'保存未成功，请重试',uncertain:'保存结果待确认，请重新读取'}[status];
 return `<div class="sticky-actions review-bottom-bar" aria-label="审核操作"><div class="review-save-summary"><span>已保留 <strong>${kept().length}</strong> 项</span><span class="save-state ${status}" role="status">${text}</span>${status==='error'||status==='uncertain'?btn('重新读取','reload-decisions','','small'):''}</div><button class="primary" data-act="tab" data-id="deliver" aria-disabled="${status==='saving'||status==='uncertain'}" ${status==='uncertain'?'disabled':''}>查看成果</button></div>`;
}
function paintReviewBar(){
 const bar=$('.review-bottom-bar');if(!bar)return;
 const template=document.createElement('template');template.innerHTML=reviewBarHTML();
 const updated=template.content.firstElementChild;
 bar.querySelector('.review-save-summary').innerHTML=updated.querySelector('.review-save-summary').innerHTML;
 const button=bar.querySelector('[data-id="deliver"]');
 button.disabled=state.saveStatus==='uncertain';
 button.setAttribute('aria-disabled',String(['saving','uncertain'].includes(state.saveStatus)));
}
function work(c,photo){
 const id=c.candidate_id,locked=state.progress.status==='completed',name=state.progress.assets.find(a=>a.asset_id===c.asset_id)?.name||'素材';
 const start=c.review_start_ms??c.start_ms,end=c.review_end_ms??c.end_ms,time=state.progress.assets.find(a=>a.asset_id===c.asset_id)?.media_type==='image'?'照片原图':seconds(c.t_ms);
 const tier=photo?((state.view.recommended_photos||[]).some(p=>p.candidate_id===id)?'精选推荐':'备选照片'):((state.view.video_segments||[]).some(v=>v.candidate_id===id)?'精选推荐':'备选片段');
 const stateName=c.review_decision||'unreviewed',symbol={keep:'✓',reject:'−',trim:'◷',unreviewed:'○'}[stateName];
 return `<article class="work ${c.review_decision||''}" data-candidate="${esc(id)}"><div class="work-media">${photo?`<img loading="lazy" tabindex="0" role="button" aria-label="查看照片大图" src="${imageURL(c,state.id)}" alt="${esc(name)} ${time}" data-act="large" data-id="${esc(id)}">`:`<img loading="lazy" src="${imageURL({...c,t_ms:c.frames?.[0]?.t_ms??c.start_ms},state.id)}" alt="视频预览"><button type="button" class="video-cover-play" data-act="play" data-id="${esc(id)}" aria-label="播放视频片段"><svg viewBox="0 0 24 24" width="26" height="26" aria-hidden="true"><path d="M8 5v14l11-7z" fill="currentColor"/></svg></button>`}</div><div class="work-meta">
 <div class="work-labels"><span class="badge recommendation-label">${tier}</span><span class="badge decision-label ${esc(stateName)}"><span aria-hidden="true">${symbol}</span> ${decision(c)}</span></div>
 <small class="work-time">${photo?time:seconds(start)+' — '+seconds(end)+' · '+seconds(end-start)}</small>
 ${c.source_type!=='real'?`<span class="badge warn">${({mock:'模拟示例',mixed:'混合来源 · 需核查',experiment:'实验结果'})[c.source_type]||'来源待核对'}</span>`:''}
 ${c.judgment_status&&c.judgment_status!=='complete'?'<span class="badge warn">连续判断未完成 · 需人工核查</span>':''}
 <p class="reason">${esc(String(reason(c)).replace(/^mock:/,'模拟示例：'))}</p><small class="filename">${esc(name)}</small>
 <div class="work-actions">${locked?'':`${btn('保留','keep',id,c.review_decision==='keep'?'choice-active':'')}${btn('淘汰','reject',id,c.review_decision==='reject'?'choice-active':'')}`}</div>
 ${!locked?`<div class="clear-choice-slot"><button class="text-action" data-act="clear" data-id="${esc(id)}" ${c.review_decision?'':'disabled'} title="撤回此项保留、淘汰或待修剪决定；不会删除原片">撤回决定</button></div>`:''}
 ${photo?`<label class="comparison-choice"><input type="checkbox" data-compare="${esc(id)}" ${state.compare.has(id)?'checked':''}>加入比较</label>`:''}
 ${btn('查看详情','work-detail',id,'text-action work-detail-link')}</div></article>`;
}
actions['work-detail']=id=>{
 const c=find(id),photo=(state.view.photos||[]).some(p=>p.candidate_id===id),name=state.progress.assets.find(a=>a.asset_id===c.asset_id)?.name||'素材';
 if(!photo)return player(id);
 const media=photo?`<div class="detail-image-stage" data-state="loading" aria-busy="true"><img data-detail-image data-src="${imageURL(c,state.id,true)}" alt="${esc(name)}"><div class="detail-image-message" role="status"><span class="detail-loading">正在加载清晰图片…</span><span class="detail-image-error" hidden>图片暂未加载成功${btn('重新加载','detail-image-retry','','small')}</span></div></div>`:`<div class="detail-video-stage">${btn('播放片段','play',id,'primary')}<span class="hint">播放当前片段，查看连续画面</span></div>`;
 modal('作品详情',`<div class="work-detail-view">${media}<div class="work-detail-copy"><section><h3>选择理由</h3><p>${esc(String(reason(c)))}</p></section><section><h3>原片（上传素材）</h3><p class="filename">${esc(name)}</p><p>${photo?(state.progress.assets.find(a=>a.asset_id===c.asset_id)?.media_type==='image'?'照片原图':seconds(c.t_ms)):seconds(c.review_start_ms??c.start_ms)+' — '+seconds(c.review_end_ms??c.end_ms)}</p></section><section>${evidenceHTML(c)}<p class="hint">系统检查不代表保证好看，最终由你选择保留。</p></section></div></div>`,'','work-detail');
 if(photo)loadDetailImage();
};
function loadDetailImage(){
 document.querySelectorAll('[data-detail-image]').forEach(img=>{
 const stage=img.parentElement,loading=stage.querySelector('.detail-loading'),error=stage.querySelector('.detail-image-error');
 stage.dataset.state='loading';stage.setAttribute('aria-busy','true');loading.hidden=false;error.hidden=true;delete img.dataset.failed;img.style.opacity='';
 img.onload=async()=>{try{await img.decode()}catch{}if(!img.isConnected)return;stage.dataset.state='ready';stage.setAttribute('aria-busy','false')};
 img.onerror=()=>{if(!img.isConnected)return;stage.dataset.state='error';stage.setAttribute('aria-busy','false');loading.hidden=true;error.hidden=false};
 img.src=img.dataset.src;
 });
}
actions['detail-image-retry']=loadDetailImage;

function evidenceHTML(c){
 let html='';
 if(c.photo_check){html+=`<p class="hint">系统画面检查：${c.photo_check.status==='pass'?'通过':c.photo_check.status==='fail'?'未通过':'未完成'}</p>`;if(c.photo_check.visible_evidence)html+=`<p class="hint">${Object.entries(c.photo_check.visible_evidence).map(([k,v])=>esc(({face:'脸部',eyes:'眼睛',paws:'爪子'}[k]||k)+ '：'+v)).join('；')}</p>`}
 if(c.boundary_evidence)html+=`<p class="hint">开始依据：${esc(c.boundary_evidence.start)}<br>结束依据：${esc(c.boundary_evidence.end)}</p>`;
 if(c.continuous_evidence?.length)html+=`<details><summary>逐时点观看依据（采样观察）</summary>${c.continuous_evidence.map(e=>`<p class="hint">${seconds(e.t_ms)} · ${esc(({good:'好画面',transition:'自然过渡',defect:'坏画面',uncertain:'尚未看准'}[e.viewing]))} · ${esc(e.reason)}</p>`).join('')}</details>`;
 return html;
}
async function decide(id,kind){
 const scroll=window.scrollY,s=state;if(s.busy.size||s.saveStatus==='uncertain')return;
 s.busy.add(id);s.saveStatus='saving';paintReviewBar();let acknowledged=false;
 try{
  await post('/api/v1/tasks/'+s.id+'/feedback',body(find(id),kind));acknowledged=true;
  if(state===s){await refresh({candidateId:id});s.saveStatus='saved';paintReviewBar();window.scrollTo(0,scroll);toast(kind==='clear_decision'?'已撤回这项决定，作品仍在列表中':'选择已保存')}
 }catch(e){
  if(state===s){s.saveStatus=!acknowledged&&[400,401,403,404,409,413,422,429].includes(e.status)?'error':'uncertain';paintReviewBar()}
  throw e;
 }finally{s.busy.delete(id)}
}
actions['reload-decisions']=async()=>{const s=state;await refresh();if(state===s){s.saveStatus='saved';paintReviewBar();toast('已重新读取保存状态，请核对当前选择')}};

for(const k of ['keep','reject'])actions[k]=id=>decide(id,k);actions.clear=id=>decide(id,'clear_decision');
actions['review-page']=page=>{state.page=+page;drawGallery();document.querySelector('.toolbar')?.scrollIntoView({block:'start'})};
const pickerPositions=new Map();
function pickerPositionKey(s){return 'furcut-picker:'+s.id+':'+(s.view.pipeline_version??2)}
function rememberPicker(s){
 const card=[...document.querySelectorAll('#works [data-candidate]')].find(el=>el.getBoundingClientRect().bottom>100);
 const saved={tab:s.tab,filter:s.filter,alternatives:s.alternatives,page:s.page,compare:[...s.compare],scrollY:window.scrollY,anchorId:card?.dataset.candidate,anchorTop:card?.getBoundingClientRect().top,details:[...document.querySelectorAll('#main details')].map(el=>el.open)};
 const key=pickerPositionKey(s);pickerPositions.set(key,saved);try{sessionStorage.setItem(key,JSON.stringify(saved))}catch{}
}
function previousPicker(s){
 const key=pickerPositionKey(s);let saved=pickerPositions.get(key);
 if(!saved){try{saved=JSON.parse(sessionStorage.getItem(key)||'null')}catch{}}
 return saved&&['photos','videos'].includes(saved.tab)?saved:null;
}
function changeReviewTab(tab,saved=null){
 const s=state;if(!s||!['photos','videos','deliver'].includes(tab))return;
 if(['saving','uncertain'].includes(s.saveStatus)){toast('请先确认选择已保存');return}
 if(tab===s.tab)return;
 if(tab==='deliver')rememberPicker(s);
 s.tab=tab;s.filter=saved?.filter||(s.progress.status==='completed'?'keep':'all');s.page=Number.isInteger(saved?.page)&&saved.page>=0?saved.page:0;
 if(saved){s.alternatives=!!saved.alternatives;s.compare=new Set(saved.compare||[])}
 const u=new URL(location.href);u.hash='/task/'+s.id+(tab==='photos'?'':'/'+tab);history.replaceState(null,'',u);draw();
 if(saved){
  document.querySelectorAll('#main details').forEach((el,i)=>el.open=!!saved.details?.[i]);
  requestAnimationFrame(()=>requestAnimationFrame(()=>{if(state!==s||s.tab!==tab)return;const anchor=[...document.querySelectorAll('#works [data-candidate]')].find(el=>el.dataset.candidate===saved.anchorId);const top=anchor&&Number.isFinite(saved.anchorTop)?window.scrollY+anchor.getBoundingClientRect().top-saved.anchorTop:saved.scrollY||0;window.scrollTo({top,behavior:'instant'})}));
 }else if(tab==='deliver')requestAnimationFrame(()=>{if(state===s&&s.tab==='deliver')$('#works')?.scrollIntoView({block:'start'})});
}
actions.tab=tab=>changeReviewTab(tab);
actions['return-picking']=()=>{const saved=previousPicker(state);changeReviewTab(saved?.tab||'photos',saved)};
actions.refresh=async()=>{await refresh();watch()};
actions['rename-task']=()=>{state.renaming=true;state.renameDraft=state.progress.title;state.renameError='';draw();$('#task-title-input').focus();$('#task-title-input').select()};
actions['cancel-title']=()=>{state.renaming=false;state.renameError='';draw()};
actions['save-title']=async()=>{const s=state,title=(s.renameDraft||'').trim();if(!title){s.renameError='请输入任务名称';draw();return}try{await api('/api/v1/workspace/tasks/'+s.id+'/title',{method:'PATCH',body:JSON.stringify({title})});if(state!==s)return;s.renaming=false;await refresh();toast('名称已保存')}catch(e){if(state===s){s.renameError='名称保存未确认：'+e.message;s.renaming=true;draw()}throw e}};
actions['estimate-task']=async()=>{const s=state,n=++estimateDialogEpoch;modal('处理前预估',estimateLoadingHTML());try{const r=await estimateFeedback(()=>api('/api/v1/workspace/tasks/'+s.id+'/estimate'));if(state===s&&n===estimateDialogEpoch&&$('#modal').open&&$('#modal-title')?.textContent==='处理前预估')modal('处理前预估',estimateHTML(r),btn('知道了','close'))}catch(e){if(state===s&&n===estimateDialogEpoch&&$('#modal').open&&$('#modal-title')?.textContent==='处理前预估')modal('预估暂不可用','<p>'+esc(e.message)+'</p>',btn('返回','close'))}};
actions.analyze=async mode=>{
 if(state.editingSettings){toast('请先保存或取消设置修改');return}
 if(mode!=='real'){await actions['dispatch-analysis'](mode);return}
 const s=state,n=++estimateDialogEpoch;modal('开始前确认',estimateLoadingHTML());
 try{const r=await estimateFeedback(()=>api('/api/v1/workspace/tasks/'+s.id+'/estimate'));if(state!==s||n!==estimateDialogEpoch||!$('#modal').open||$('#modal-title')?.textContent!=='开始前确认')return;
 const blocked=freshTask(s.progress)?estimateGate(r,s.progress.budget.limit)!=='ready':r.budget_status==='insufficient'&&!r.already_spent_minor;
 modal('开始前确认',estimateHTML(r,{runtimeNote:'开始后按实际模型用量计费。处理期间可以离开页面；请保持电脑唤醒与服务运行。'}),btn('先不处理','close')+(blocked?'':btn('确认处理','confirm-analysis','','primary')));
 }catch(e){if(state===s&&n===estimateDialogEpoch&&$('#modal').open&&$('#modal-title')?.textContent==='开始前确认')modal('预估未完成','<p>'+esc(e.message)+'</p><p>本次未开始处理，请修复后重新预估。</p>',btn('返回','close'))}
};
actions['confirm-analysis']=async()=>{closeModal();await actions['dispatch-analysis']('real')};
actions['dispatch-analysis']=async mode=>{const s=state;await post('/api/v1/tasks/'+s.id+'/analyze',{mode});await refresh();watch()};
actions['cancel-task']=()=>modal('停止后续处理？','<p>已完成成果和已发生费用会保留。在途请求仍需结算或核账，取消不能撤销已经发出的请求。</p>',btn('返回','close')+btn('确认取消','confirm-cancel','','danger'));actions['confirm-cancel']=async()=>{await post('/api/v1/tasks/'+state.id+'/cancel');closeModal();await refresh();watch()};
actions.budget=()=>modal('调整本次预算',`<label class="field"><span>总预算上限（元）</span><input id="new-budget" type="number" min=".01" step=".01" value="${state.progress.budget.limit/1e6}"></label><p class="hint">已发生、在途和未知费用继续占用预算。增加预算不会自动发送请求。</p>`,btn('保存预算','save-budget','','primary'));actions['save-budget']=async()=>{await post('/api/v1/tasks/'+state.id+'/budget',{limit_minor:Math.round(+$('#new-budget').value*1e6)});closeModal();await refresh()};
actions.reopen=async()=>{await post('/api/v1/tasks/'+state.id+'/reopen-review');await refresh()};actions['confirm-review']=()=>modal('确认本次挑选？','<p>只有已保留的作品会进入下载。未保留、已淘汰和待修剪的内容不会自动导出。</p><p>之后仍可重新打开审核，修改选择；已生成的历史成果会保留。</p>',btn('继续挑选','close')+btn('确认选择','finish-review','','primary'));actions['finish-review']=async()=>{await post('/api/v1/tasks/'+state.id+'/confirm-review');closeModal();await refresh();toast('选择已确认，可以下载；以后仍可重新修改')};
actions.large=id=>{const c=find(id);modal('查看清晰原帧',`<img class="large-photo" src="${imageURL(c,state.id,true)}" alt="源素材原始清晰帧"><p class="hint">${seconds(c.t_ms)} · 不进行生成式修图</p>`,state.progress.status==='completed'?'':btn('淘汰这张','modal-reject',id)+btn('保留这张','modal-keep',id,'primary'));};actions['modal-keep']=async id=>{await decide(id,'keep');closeModal()};actions['modal-reject']=async id=>{await decide(id,'reject');closeModal()};
actions.compare=()=>{const selected=[...state.compare].map(find);if(selected.length!==2){toast('请先勾选两张照片');return}modal('比较相似照片',`<div class="compare">${selected.map(c=>`<div><div class="detail-image-stage" data-state="loading" aria-busy="true"><img data-detail-image data-src="${imageURL(c,state.id,true)}" alt="比较照片"><div class="detail-image-message" role="status"><span class="detail-loading">正在加载清晰图片…</span><span class="detail-image-error" hidden>图片暂未加载成功${btn('重新加载','detail-image-retry','','small')}</span></div></div><p>${state.progress.assets.find(a=>a.asset_id===c.asset_id)?.media_type==='image'?'照片原图':seconds(c.t_ms)} · ${esc(String(reason(c)).slice(0,80))}</p>${state.progress.status==='completed'?'':btn('记录偏好','prefer-photo',c.candidate_id,'primary')}</div>`).join('')}</div><p class="hint comparison-note">${state.progress.status==='completed'?'审核已确认，当前仅比较查看；继续修改审核后可以保存偏好。':'仅记录本次比较，不改变保留结果，也不会自动用于下次选片。'}</p>`);loadDetailImage()};
actions['prefer-photo']=async id=>{const c=find(id),other=[...state.compare].map(find).find(c=>c.candidate_id!==id);await post('/api/v1/tasks/'+state.id+'/feedback',body(c,'candidate_comparison',{over_candidate_id:other.candidate_id,note:'web pair comparison'}));toast('已记录本次比较偏好');closeModal()};
async function meta(c,s){if(!s.meta.has(c.asset_id))s.meta.set(c.asset_id,await api('/api/v1/assets/'+c.asset_id+'/meta'));return s.meta.get(c.asset_id)}
let trimState,playerEpoch=0;
function playbackStatus(text,error=false){const el=$('#play-status');if(el){el.textContent=text;el.className='hint'+(error?' playback-error':'');el.hidden=!text||!error}}
function pauseModalVideos(){document.querySelectorAll('#modal video').forEach(v=>v.pause())}
async function loadClip(v,c,s,start,end,epoch){
 if(trimState)trimState.clipStart=start;v.removeAttribute('src');v.load();playbackStatus('正在准备片段…');
 try{const r=await post('/api/v1/preview/video',{task_id:s.id,candidate_id:c.candidate_id,asset_id:c.asset_id,start_ms:start,end_ms:end});if(state===s&&v.isConnected&&epoch===playerEpoch)v.src=r.url}
 catch(e){if(v.isConnected&&epoch===playerEpoch){playbackStatus('片段未能加载：'+e.message,true);$('#play-retry').hidden=false}}
}
// Preserved trim implementation is retained below; current product opens view-only details.
async function player(id){
 const c=find(id),s=state,epoch=++playerEpoch;pauseModalVideos();trimState=null;
 modal('视频详情','<div class="video-loading" aria-label="正在加载视频"></div>','','work-detail');
 let m;try{m=await meta(c,s)}catch(e){if(epoch===playerEpoch&&$('#modal').open){$('.modal-content').innerHTML=`<p class="notice error">视频信息未能加载：${esc(e.message)}</p>${btn('重新加载','retry-video-view',id)}`}return}
 if(state!==s||epoch!==playerEpoch||!$('#modal').open)return;
 const start=c.review_start_ms??c.start_ms,end=c.review_end_ms??c.end_ms,name=s.progress.assets.find(a=>a.asset_id===c.asset_id)?.name||'素材';
 if(!Number.isFinite(start)||!Number.isFinite(end)||start<0||end<=start||end>m.duration_ms){$('.modal-content').innerHTML='<p class="notice error">视频范围不完整，请重新加载任务后检查。</p>';return}
 modal('视频详情',`<div class="work-detail-view video-readonly-view"><div class="detail-video-stage detail-video-player"><video id="source-player" class="player" controls playsinline preload="metadata" poster="${imageURL({...c,t_ms:start},s.id)}"></video><div class="playback-feedback"><p id="play-status" class="hint" role="status" hidden></p><button id="play-retry" data-act="retry-video-view" data-id="${esc(id)}" class="small" hidden>重新加载</button></div></div><div class="work-detail-copy"><section><h3>选择理由</h3><p>${esc(String(reason(c)))}</p></section><section><h3>原片（上传素材）</h3><p class="filename">${esc(name)}</p><p>当前视频范围（原片时间）<br>${seconds(start)} — ${seconds(end)}</p></section>${evidenceHTML(c)?`<section>${evidenceHTML(c)}</section>`:''}</div></div>`,'','work-detail');
 const v=$('#source-player');let autoplay=true;
 v.oncanplay=()=>{if(!v.isConnected||!$('#modal').open||epoch!==playerEpoch)return;playbackStatus('');if(autoplay){autoplay=false;v.play().catch(()=>{})}};
 v.onerror=()=>{if(v.isConnected){playbackStatus('视频暂时无法播放，请重新加载。',true);$('#play-retry').hidden=false}};
 await loadClip(v,c,s,start,end,epoch);
}
actions['retry-video-view']=id=>player(id);
async function archivedTrimPlayer(id,editing=false,original=false){

 const c=find(id),s=state,epoch=++playerEpoch;pauseModalVideos();trimState=null;
 modal('视频详情','<div class="video-loading" aria-label="正在加载视频"></div>','','video-review');
 let m;try{m=await meta(c,s)}catch(e){if(epoch===playerEpoch&&$('#modal').open){$('.modal-content').innerHTML=`<p class="notice error">视频信息未能加载：${esc(e.message)}</p>${btn('重新加载','play',id)}`}return}
 if(state!==s||epoch!==playerEpoch||!$('#modal').open)return;
 const start=c.review_start_ms??c.start_ms,end=c.review_end_ms??c.end_ms,duration=m.duration_ms;
 if(!Number.isFinite(duration)||duration<=0||!Number.isFinite(start)||!Number.isFinite(end)||start<0||end<=start||end>duration){$('.modal-content').innerHTML='<p class="notice error">视频时长或片段边界不完整，请重新加载任务后检查。</p>';return}
 const locked=s.progress.status==='completed';
 const t={id,c,start,end,savedStart:start,savedEnd:end,hasSavedBoundary:c.review_start_ms!=null&&c.review_end_ms!=null,duration,frameStep:1000/(m.fps||30),dirty:false,saving:false,editing:!locked,mode:'clip',previewing:false};trimState=t;
 const name=s.progress.assets.find(a=>a.asset_id===c.asset_id)?.name||'素材';
 modal('视频详情',`<div class="video-viewer-layout"><div class="video-viewer-main"><div class="video-stage"><video id="source-player" class="player" controls playsinline preload="metadata" poster="${imageURL({...c,t_ms:start},s.id)}"></video></div><div class="video-playback-bar"><div class="playback-feedback"><p id="play-status" class="hint" role="status" hidden></p><button id="play-retry" data-act="retry-trim" class="small" hidden>重新加载</button></div></div><div id="trim-editor" ${t.editing?'':'hidden'}><div class="timeline"><div class="trim-track" style="--trim-start:${start/duration*100}%;--trim-end:${end/duration*100}%"><div class="trim-track-line"></div><input aria-label="片段起点" type="range" id="trim-start" min="0" max="${duration}" step="1" value="${start}"><input aria-label="片段终点" type="range" id="trim-end" min="0" max="${duration}" step="1" value="${end}"></div><div class="range-row">${['start','end'].map((key,i)=>`<div class="trim-boundary"><label for="trim-${key}">${i?'终点':'起点'} <output id="${key}-label" class="time-output">${seconds(i?end:start)}</output></label></div>`).join('')}</div><div class="trim-preview-action">${btn('裁剪预览','trim-preview','','small')}${btn('撤销调整','trim-reset','','small')}</div></div></div></div><details class="video-viewer-copy"><summary>选择理由与原片信息</summary><section><h3>选择理由</h3><p>${esc(String(reason(c)))}</p></section><section><h3>原片（上传素材）</h3><p class="filename">${esc(name)}</p><p>当前选取范围（原片时间）<br><span id="video-range">${seconds(start)} — ${seconds(end)}</span></p></section>${evidenceHTML(c)?`<section>${evidenceHTML(c)}</section>`:''}</details></div>`,`<p id="trim-status" class="hint" role="status">${t.editing?trimBaseline(t):'观看不会改变已保存的选择。'}</p><div class="video-footer-actions" id="trim-footer" ${t.editing?'':'hidden'}>${btn('保存边界','trim-save','','primary')}</div>`,'video-review');
 const v=$('#source-player');
 v.onloadedmetadata=()=>{if(v.isConnected)v.currentTime=t.mode==='original'?Math.min((t.seekMs??t.start)/1000,Math.max(0,v.duration-.001)):0};
 v.oncanplay=()=>{if(!v.isConnected)return;playbackStatus(t.mode==='original'?'当前查看原片（上传素材）。':t.previewing?'当前播放修剪预览'+(t.dirty?'，尚未保存。':'。'):t.hasSavedBoundary?'当前播放已保存的修剪片段。':'当前播放推荐片段。');if(t.autoplay){t.autoplay=false;v.play().catch(()=>playbackStatus('视频已就绪，点击播放即可观看。'))}};
 v.onerror=()=>{if(v.isConnected){playbackStatus('视频暂时无法播放，请重新加载；原片仍保留。',true);$('#play-retry').hidden=false}};
 $('#trim-start').oninput=e=>setRange('start',+e.target.value);$('#trim-end').oninput=e=>setRange('end',+e.target.value);
 updateTrimControls(t);await switchVideoMode(editing||original?'original':'clip',!editing);
}
async function switchVideoMode(mode,autoplay=false,seekMs){
 const t=trimState,v=$('#source-player'),s=state;if(!t||!v||t.saving||t.uncertain)return;
 const epoch=++playerEpoch;t.mode=mode;t.seekMs=seekMs??t.start;t.autoplay=autoplay;v.pause();$('#play-retry').hidden=true;
 const context=$('[data-act="view-context"]');if(context)context.textContent=mode==='clip'?'查看上传原片':'返回当前片段';
 if(mode==='original'){playbackStatus('正在加载原片…');v.src=originalURL(t.c)}else await loadClip(v,t.c,s,t.start,t.end,epoch);
}
actions['video-mode']=mode=>switchVideoMode(mode,true);
$('#modal').addEventListener('close',()=>{if($('#modal').open)return;pauseModalVideos();playerEpoch++;trimState=null});
actions.play=id=>player(id);actions.original=id=>player(id,false,true);actions.trim=id=>player(id,true,true);
actions['retry-trim']=()=>{const t=trimState;if(t)return switchVideoMode(t.mode,false)};
function requestClose(){if(trimState?.uncertain){toast('保存结果待确认，请先点击核对保存');return}if(trimState?.saving){toast('正在保存边界，请稍等');return}if(trimState?.dirty&&$('#trim-status')){$('#trim-status').className='notice warn';$('#trim-status').innerHTML='边界尚未保存。 '+btn('放弃修改','discard-trim','','small');return}closeModal()}
actions.close=requestClose;actions['discard-trim']=()=>{if(trimState?.saving||trimState?.uncertain)return;if(trimState)trimState.dirty=false;closeModal()};
$('#modal').addEventListener('cancel',e=>{pauseModalVideos();if(trimState?.dirty||trimState?.saving||trimState?.uncertain){e.preventDefault();requestClose()}});
function invalidateTrimPreview(t,seekMs){t.previewGeneration=(t.previewGeneration||0)+1;if(t.mode==='clip'){playerEpoch++;$('#source-player')?.pause();switchVideoMode('original',false,seekMs)}}
function seekTrimFrame(t,ms){
 const v=$('#source-player');if(trimState!==t||!v||t.mode!=='original')return;
 t.seekMs=ms;t.autoplay=false;v.pause();
 if(t.scrubFrame)return;
 t.scrubFrame=requestAnimationFrame(()=>{t.scrubFrame=null;if(trimState!==t||!v.isConnected||t.mode!=='original'||v.readyState<1)return;v.currentTime=Math.max(0,Math.min(t.seekMs/1000,Math.max(0,v.duration-.001)))})
}
function trimBaseline(t){return t.hasSavedBoundary?'人工边界已保存；可继续调整。':'当前为推荐片段范围；调整后请保存。'}
function updateTrimControls(t){
 if(trimState!==t||!$('#trim-status'))return;
 $('#trim-start').value=t.start;$('#trim-end').value=t.end;
 $('#start-label').textContent=seconds(t.start);$('#end-label').textContent=seconds(t.end);
 $('#video-range').textContent=seconds(t.start)+' — '+seconds(t.end);
 const track=$('.trim-track');track.style.setProperty('--trim-start',t.start/t.duration*100+'%');track.style.setProperty('--trim-end',t.end/t.duration*100+'%');
 document.querySelectorAll('.timeline input,.timeline button,.video-footer-actions button,[data-act="trim-preview"],[data-act="trim-reset"],.video-mode-bar button').forEach(el=>el.disabled=t.saving||!!t.uncertain);
 const reset=$('[data-act="trim-reset"]');if(reset)reset.disabled=t.saving||!!t.uncertain||!t.dirty;
 const save=$('[data-act="trim-save"]');save.disabled=t.saving||(!t.dirty&&!t.uncertain);save.textContent=t.saving?'保存中…':t.uncertain?'核对保存':'保存边界';
}
function setRange(key,value){const t=trimState;if(!t||t.saving||t.uncertain)return;const beforeStart=t.start,beforeEnd=t.end;if(key==='start')t.start=Math.max(0,Math.min(Math.round(value),t.end-1));else t.end=Math.min(t.duration,Math.max(Math.round(value),t.start+1));if(beforeStart!==t.start||beforeEnd!==t.end){invalidateTrimPreview(t,t[key]);seekTrimFrame(t,t[key])}t.dirty=t.start!==t.savedStart||t.end!==t.savedEnd;updateTrimControls(t);$('#trim-status').className='hint'+(t.dirty?' trim-unsaved':'');$('#trim-status').textContent=t.dirty?'边界有修改，尚未保存。':trimBaseline(t)}
for(const key of ['start','end'])actions['nudge-'+key]=n=>{setRange(key,trimState[key]+(+n)*trimState.frameStep);return switchVideoMode('original',false,trimState[key])};
actions['trim-save']=async()=>{
 const t=trimState,s=state;if(!t||t.saving||(!t.dirty&&!t.uncertain))return;t.saving=true;updateTrimControls(t);
 try{
  if(t.uncertain){const r=await api('/api/v1/tasks/'+s.id+'/results');const c=[...(r.video_candidates||r.video_segments||[]),...(r.video_fragments||[])].find(c=>c.candidate_id===t.id);if(!c)throw Error('候选已变化，请关闭后重新打开');t.start=t.savedStart=c.review_start_ms??c.start_ms;t.end=t.savedEnd=c.review_end_ms??c.end_ms;t.hasSavedBoundary=c.review_start_ms!=null&&c.review_end_ms!=null;t.uncertain=false;t.dirty=false;t.c=c;s.view=r;await refresh({candidateId:t.id});invalidateTrimPreview(t);$('#trim-status').textContent='已读取服务器边界，请核对后继续。';return}
  $('#trim-status').className='hint';$('#trim-status').textContent='正在保存边界…';
  const response=await post('/api/v1/tasks/'+s.id+'/feedback',body(t.c,'boundary_adjust',{start_ms:t.start,end_ms:t.end}));
  if(!Number.isFinite(response.start_ms)||!Number.isFinite(response.end_ms))throw Error('保存回执不完整');
  t.savedStart=response.start_ms;t.savedEnd=response.end_ms;t.start=t.savedStart;t.end=t.savedEnd;t.hasSavedBoundary=true;t.dirty=false;t.c.review_start_ms=t.start;t.c.review_end_ms=t.end;if(t.mode==='clip')playbackStatus('当前播放已保存的修剪片段。');
  $('#trim-status').textContent='边界已保存，确认保留后进入成果。';
  try{await refresh({candidateId:t.id});if(trimState===t)$('#trim-status').textContent=kept().some(c=>c.candidate_id===t.id)?'边界已保存，成果将使用新边界。':'边界已保存，确认保留后进入成果。'}catch{$('#trim-status').textContent='边界已保存，列表暂未刷新，请稍后重新进入查看。'}
 }catch(e){if([400,401,403,404,409,413,422,429].includes(e.status)){t.start=t.savedStart;t.end=t.savedEnd;t.dirty=false;invalidateTrimPreview(t);$('#trim-status').className='notice error';$('#trim-status').textContent='保存失败，已恢复原边界：'+e.message}else{t.uncertain=true;$('#trim-status').className='notice warn';$('#trim-status').textContent='保存结果待确认。请点击核对保存，先读取状态，不会重复提交。'}toast(e.message)}
 finally{t.saving=false;updateTrimControls(t);requestAnimationFrame(()=>updateTrimControls(t))}
};
actions['mark-trim']=async id=>{if(trimState?.saving)return;if(trimState?.dirty||trimState?.uncertain){toast('请先保存或核对边界');return}await decide(id,'trim');closeModal()};
actions['trim-reset']=async()=>{const t=trimState;if(!t||t.saving||t.uncertain||!t.dirty)return;t.start=t.savedStart;t.end=t.savedEnd;t.dirty=false;t.previewing=false;updateTrimControls(t);$('#trim-status').className='hint';$('#trim-status').textContent=t.hasSavedBoundary?'已恢复上次保存的范围。':'已恢复推荐片段范围。';await switchVideoMode('clip',false);requestAnimationFrame(()=>updateTrimControls(t))};
actions['trim-preview']=()=>{if(trimState)trimState.previewing=true;return switchVideoMode('clip',true)};
actions['view-context']=()=>switchVideoMode(trimState?.mode==='clip'?'original':'clip',false);
function deliveryStamp(s,c){return JSON.stringify([s.view.pipeline_version,s.options,c])}
function deliveryItemState(s,c){const value=s.deliveryDownloads?.get(c.candidate_id);return value?.stamp===deliveryStamp(s,c)?value:null}
function deliveryRow(c,index,s){
 const id=c.candidate_id,photo=c.kind==='photo',asset=s.progress.assets.find(a=>a.asset_id===c.asset_id),d=deliveryItemState(s,c);
 const thumb=photo?c:{...c,t_ms:c.frames?.[0]?.t_ms??c.start_ms};
 const timing=photo?(asset?.media_type==='image'?'照片原图':'来源位置 '+seconds(c.t_ms)):seconds(c.start_ms)+' — '+seconds(c.end_ms)+' · '+seconds(c.end_ms-c.start_ms);
 const blocked=s.view.pipeline_version===3;
 const control=blocked?'<span class="hint">暂不可用</span>':d?.loading?'<button class="small" disabled aria-busy="true">准备中</button>':d?.url?`<a class="button small" href="${esc(d.url)}" download>单独下载</a>`:btn(d?.error?'重试下载':'单独下载','single-export',id,'small');
 return `<article class="delivery-item" data-delivery-id="${esc(id)}"><button class="delivery-thumb" data-act="work-detail" data-id="${esc(id)}" aria-label="${photo?'查看照片详情':'播放视频'}"><img loading="lazy" src="${imageURL(thumb,s.id)}" alt="${photo?'照片':'视频'}预览">${photo?'':'<span class="delivery-play" aria-hidden="true">▶</span>'}</button><div class="delivery-item-copy"><strong>${photo?'照片':'视频'} ${String(index+1).padStart(2,'0')}</strong><p>${esc(timing)}</p><p class="filename">${esc(asset?.name||'素材')}</p><p class="delivery-item-status ${d?.error?'delivery-error':''}" role="status">${esc(d?.loading?'正在准备文件…':d?.error|| (d?.url?'文件已准备，可下载。':''))}</p></div><div class="delivery-item-actions">${control}${(d?.variants||[]).map(v=>`<a class="button small" href="${esc(v.url)}" download>最佳帧版</a>`).join('')}</div></article>`;
}
function paintDeliveryRow(s,id){if(state!==s||s.tab!=='deliver')return;const items=kept(),index=items.findIndex(c=>c.candidate_id===id),node=[...document.querySelectorAll('[data-delivery-id]')].find(el=>el.dataset.deliveryId===id);if(index>=0&&node)node.outerHTML=deliveryRow(items[index],index,s)}
function deliveryAlerts(s){
 if(s.view.pipeline_version===3)return '<div class="notice warn">这是实验选片任务，暂不提供正式下载。日常交付请使用标准任务。</div>';
 return [s.progress.source_hold?`<div class="notice warn">${esc(s.progress.source_hold)}</div>`:'',s.progress.budget.unknown>0||s.progress.status==='pending_reconciliation'?'<div class="notice warn">有费用待核对，新的分析已暂停。已保留作品的下载仍会核对素材是否可用。</div>':''].join('');
}
function deliveryHistoryHTML(packages,jobs){
 const records=packages.map(p=>`<div class="delivery-record"><div><strong>${p.status==='verified'?p.count+'项作品':'文件异常'}</strong><p class="hint">${p.created_at?date(p.created_at):esc(p.message)}</p></div><div class="delivery-record-actions">${p.url?`<a class="button small" href="${esc(p.url)}" target="_blank" rel="noopener">查看</a><a class="button small" href="${esc(p.download_url)}" download>下载</a>`:''}</div></div>`).join('');
 const exports=jobs.map(j=>`<details class="delivery-old-export"><summary>${date(j.created_at)} · ${j.total_items}项作品 · ${j.status==='completed'?'已准备':j.status==='running'?'准备中':'需核对'}</summary>${j.snapshot_matches_review===false||j.render_matches_current===false?'<p class="hint">这是之前的下载版本，当前作品请使用“全部下载”。</p>':''}${j.error?`<p class="hint delivery-error">${esc(j.error.message||j.error.error||'部分文件未准备完成。')}</p>`:''}${(j.items||[]).filter(i=>i.status==='done').map(i=>`<div class="delivery-record"><div class="filename">${esc(i.source)} · ${i.kind==='photo'?seconds(i.t_ms):seconds(i.start_ms)+' — '+seconds(i.end_ms)}</div><div class="delivery-record-actions"><a class="button small" href="/api/v1/export/file/${encodeURIComponent(i.file_name)}" download>下载</a>${(i.variant_files||[]).map(v=>`<a class="button small" href="/api/v1/export/file/${encodeURIComponent(v.file)}" download>最佳帧版</a>`).join('')}</div></div>`).join('')}${j.manifest_file?`<a class="button small" href="/api/v1/export/file/${encodeURIComponent(j.manifest_file)}" download>下载清单</a>`:''}</details>`).join('');
 return records||exports?`<details class="card delivery-history"><summary>下载记录 <span class="count">${packages.length+jobs.length}</span></summary><div>${records}${exports}</div></details>`:'';
}
function deliveryAllStamp(s){return JSON.stringify([s.options,s.progress.title,kept()])}
function paintDeliveryAll(s){
 if(state!==s||s.tab!=='deliver')return;const b=$('[data-act="download-all"]'),d=s.deliveryAll?.loading||s.deliveryAll?.stamp===deliveryAllStamp(s)?s.deliveryAll:null;
 if(b){b.textContent=d?.loading?'准备中':d?.error?'重试':'全部下载';b.disabled=!!d?.loading||!kept().length||s.view.pipeline_version===3;b.setAttribute('aria-busy',String(!!d?.loading))}
 const message=$('#delivery-all-status');if(message){message.className='delivery-all-status'+(d?.error?' delivery-error':'');message.innerHTML=d?.loading?'正在准备压缩包，请稍候…':d?.error?esc(d.error):d?.url?`压缩包已准备。<a href="${esc(d.url)}" download>重新下载</a>`:'';message.hidden=!message.textContent}
}
async function renderDelivery(){
 const s=state;if(!s)return;const items=kept(),blocked=s.view.pipeline_version===3;
 const [d,h]=await Promise.all([api('/api/v1/workspace/tasks/'+s.id+'/deliveries'),api('/api/v1/workspace/tasks/'+s.id+'/export-history')]);if(state!==s||s.tab!=='deliver')return;
 s.deliveryDownloads??=new Map();
 $('#works').innerHTML=`${deliveryAlerts(s)}<section class="card delivery-card"><div class="delivery-heading"><div><h2>已保留作品 <span class="count">${items.length}项</span></h2></div><div class="delivery-toolbar">${btn('全部下载','download-all','','primary small').replace('<button ',!items.length||blocked?'<button disabled ':'<button ')}${btn('返回挑选','return-picking','','small')}</div></div><p id="delivery-all-status" class="delivery-all-status" role="status" hidden></p><div class="delivery-items">${items.length?items.map((c,i)=>deliveryRow(c,i,s)).join(''):empty('还没有保留作品','返回挑选，保留喜欢的照片或视频后即可下载。')}</div></section><div id="delivery-history">${deliveryHistoryHTML(d.deliveries,h.jobs||[])}</div>`;
 paintDeliveryAll(s);
}
function requestFileDownload(url){const a=document.createElement('a');a.href=url;a.download='';document.body.append(a);a.click();a.remove()}
async function updateDeliveryHistory(s){const[d,h]=await Promise.all([api('/api/v1/workspace/tasks/'+s.id+'/deliveries'),api('/api/v1/workspace/tasks/'+s.id+'/export-history')]);if(state===s&&s.tab==='deliver'&&$('#delivery-history'))$('#delivery-history').innerHTML=deliveryHistoryHTML(d.deliveries,h.jobs||[])}
actions['download-all']=async()=>{
 const s=state;if(!s||s.view.pipeline_version===3||s.deliveryAll?.loading||!kept().length)return;
 const stamp=deliveryAllStamp(s);s.deliveryAll={loading:true,stamp};paintDeliveryAll(s);
 try{
  const r=await post('/api/v1/tasks/'+s.id+'/package');if(!r.package)throw Error('未取得下载文件，请重试。');
  const url='/api/v1/workspace/tasks/'+encodeURIComponent(s.id)+'/deliveries/'+encodeURIComponent(r.package)+'/download';
  s.deliveryAll={url,stamp};
  try{const doc=await api('/api/v1/packages/'+encodeURIComponent(r.package)+'/manifest.json');if(state===s&&deliveryAllStamp(s)===stamp){for(const c of kept()){const entry=doc.items.find(i=>i.candidate_id===c.candidate_id);if(entry){const base='/api/v1/packages/'+encodeURIComponent(r.package)+'/';s.deliveryDownloads.set(c.candidate_id,{stamp:deliveryStamp(s,c),url:base+encodeURIComponent(entry.file),variants:(entry.variant_files||[]).map(v=>({url:base+encodeURIComponent(v.file)}))});paintDeliveryRow(s,c.candidate_id)}}}}catch{/* The verified package can still be downloaded without individual links. */}
  if(state===s&&s.tab==='deliver'){paintDeliveryAll(s);if(deliveryAllStamp(s)===stamp)requestFileDownload(url);else toast('保留作品已变化，请重新下载');try{await updateDeliveryHistory(s)}catch{toast('压缩包已准备，下载记录暂未刷新')}}
 }catch(e){s.deliveryAll={error:e.message,stamp};if(state===s&&s.tab==='deliver')paintDeliveryAll(s)}
 finally{if(state===s&&s.tab==='deliver')requestAnimationFrame(()=>paintDeliveryAll(s))}
};
function exportHTML(j){if(!j)return '';const done=j.status==='completed',reviewChanged=j.snapshot_matches_review===false,renderChanged=j.render_matches_current===false,historical=reviewChanged||renderChanged;return `<div class="notice ${['failed','partial','needs_repair'].includes(j.status)?'error':historical?'warn':''}">${historical?'历史导出版本 · ':''}${done?'全部导出完成':j.status==='running'?'正在导出':j.status==='needs_repair'?'文件需恢复':'导出部分失败'} · ${j.done_items}/${j.total_items}项 ${reviewChanged?'<p>当前保留集合已变化，请重新批量导出当前成果。</p>':renderChanged?'<p>导出方式已更新，请重新批量导出当前版本。原文件与历史成果包仍保留。</p>':''}${j.error?`<p>${esc(j.error.message||j.error.error||'导出未完成')}</p>`:''}<progress value="${j.done_items}" max="${j.total_items||1}"></progress>${!historical&&['failed','partial','needs_repair'].includes(j.status)?btn(j.status==='needs_repair'?'恢复缺失文件':'重试未完成项','batch-export','','small'):''}${(j.items||[]).filter(i=>i.status==='failed').map(i=>`<p>${esc(i.source)}：${esc(i.error)}</p>`).join('')}${j.manifest_file?`<a class="button small" href="/api/v1/export/file/${encodeURIComponent(j.manifest_file)}">下载来源清单</a>`:''}<div>${(j.items||[]).filter(i=>i.status==='done').map(i=>`<div class="download-row"><span class="filename">${esc(i.source)} · ${i.kind==='photo'?seconds(i.t_ms):seconds(i.start_ms)+' — '+seconds(i.end_ms)}</span><a class="button small" href="/api/v1/export/file/${encodeURIComponent(i.file_name)}">${i.kind==='photo'?'照片':'连续版'}</a>${(i.variant_files||[]).map(v=>`<a class="button small" href="/api/v1/export/file/${encodeURIComponent(v.file)}">最佳帧开头版</a>`).join('')}</div>`).join('')}</div></div>`}
async function pollExport(id){clearTimeout(timer);const s=state;timer=setTimeout(async()=>{if(state!==s||s.tab!=='deliver')return;try{const j=await api('/api/v1/export/jobs/'+id);if(state!==s)return;$('#export-state').innerHTML=exportHTML(j);if(j.status==='running')pollExport(id)}catch(e){toast(e.message)}},1800)}
actions['batch-export']=async()=>{const s=state,r=await post('/api/v1/export/batch',{task_id:s.id,items:kept()});await renderDelivery();pollExport(r.export_job_id)};
actions.package=actions['download-all'];
actions.manifest=async()=>{const s=state;if(!s||!kept().length||s.view.pipeline_version===3)return;const r=await post('/api/v1/export/manifest',{task_id:s.id,items:kept()});if(state===s&&s.tab==='deliver'){requestFileDownload('/api/v1/export/file/'+encodeURIComponent(r.file));toast('清单已准备，已请求下载')}};
actions['single-export']=async id=>{
 const s=state,c=kept().find(i=>i.candidate_id===id);if(!c||s.view.pipeline_version===3)return;
 s.deliveryDownloads??=new Map();const stamp=deliveryStamp(s,c);if(deliveryItemState(s,c)?.loading)return;
 s.deliveryDownloads.set(id,{stamp,loading:true});paintDeliveryRow(s,id);
 try{const r=await post('/api/v1/export/'+c.kind,{task_id:s.id,candidate_id:id,asset_id:c.asset_id,...(c.kind==='photo'?{t_ms:c.t_ms,source_pts:c.source_pts,time_base:c.time_base}:{start_ms:c.start_ms,end_ms:c.end_ms})});if(!r.file)throw Error('未取得下载文件，请重试。');const url='/api/v1/export/file/'+encodeURIComponent(r.file);s.deliveryDownloads.set(id,{stamp,url,variants:(r.variant_files||[]).map(v=>({url:'/api/v1/export/file/'+encodeURIComponent(v.file)}))});if(state===s&&s.tab==='deliver'&&deliveryStamp(s,kept().find(i=>i.candidate_id===id))===stamp){paintDeliveryRow(s,id);requestFileDownload(url)}}
 catch(e){s.deliveryDownloads.set(id,{stamp,error:e.message});paintDeliveryRow(s,id)}
};

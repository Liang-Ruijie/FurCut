export const $=s=>document.querySelector(s);
export const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export const money=n=>'¥'+((n||0)/1e6).toFixed(4).replace(/0+$/,'').replace(/\.$/,'');
export const seconds=ms=>(Number(ms||0)/1000).toFixed(2)+'s';
export const date=s=>new Date(s).toLocaleString('zh-CN',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
export const statusNames={created:'待准备',assets_locked:'准备就绪',analyzing:'正在挑选',selecting:'正在精选',exporting:'正在导出',ready_for_review:'等待审核',completed:'审核已确认',cancelled:'已取消',failed:'需要处理',pending_reconciliation:'等待费用核对',complete:'已完成',partial:'部分完成',none_qualified:'暂未找到合格内容',incomplete_quality:'质量检查未完成',incomplete_budget:'预算不足',incomplete_error:'处理未完成',paused:'已观察部分，暂停中',quality_incomplete:'质检有缺项',processing:'正在细查与质检',unprocessed:'尚未处理',running:'正在处理',error:'处理出错',pending:'等待处理'};
export const label=s=>statusNames[s]||'等待处理';
export const badge=(s,text)=>`<span class="badge ${['failed','incomplete_error','error'].includes(s)?'error':['pending_reconciliation','partial','incomplete_budget','incomplete_quality'].includes(s)?'warn':''}">${esc(text||label(s))}</span>`;
let toastTimer;
export function toast(text,duration=4500){$('#toast').textContent=text;$('#toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('#toast').hidden=true,duration)}
export async function api(path,opts={}){
 const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),120000);
 try{const r=await fetch(path,{...opts,signal:opts.signal||controller.signal,headers:{...(opts.body?{'Content-Type':'application/json'}:{}),...opts.headers}});let d;try{d=await r.json()}catch{const e=new Error('服务响应不完整，操作结果尚未确认，请检查连接后重新读取。');e.code='invalid_response';if(!r.ok)e.status=r.status;throw e}if(!r.ok){const detail=Array.isArray(d.detail)?d.detail.map(x=>{const field=({limit_minor:'预算上限',budget_minor:'预算上限',coarse_fps:'粗筛每秒帧数',coarse_max_frames:'粗筛最大帧数',coarse_resize_edge:'图像输入长边像素'})[x.loc?.at(-1)]||'填写内容';return field+'不符合要求，请检查数值和必填项。'}).join(' '):typeof d.detail==='string'?d.detail:null;const e=new Error(d.error?.message||detail||'操作未完成，请稍后重试');e.status=r.status;e.code=d.error?.code;throw e}$('#network-alert').hidden=true;return d}
 catch(e){if(e instanceof TypeError || e.name==='AbortError'){e.message=e.name==='AbortError'?'等待服务响应超时，操作结果尚未确认，请重新读取状态。':'与服务的连接中断，请检查网络后重新读取状态。';$('#network-alert').textContent='与服务的连接中断，已保存内容仍在。请检查网络后重试。';$('#network-alert').hidden=false}throw e}
 finally{clearTimeout(timer)}
}
export const post=(path,body={})=>api(path,{method:'POST',body:JSON.stringify(body)});
export const btn=(name,act,id='',cls='')=>`<button class="${cls}" data-act="${act}" data-id="${esc(id)}">${esc(name)}</button>`;
export const imageURL=(c,task='',full=false)=>'/api/v1/workspace/assets/'+encodeURIComponent(c.asset_id)+'/image?'+new URLSearchParams({t_ms:c.t_ms||0,...(task?{task_id:task}:{}),...(c.source_pts!=null?{source_pts:c.source_pts,time_base:c.time_base}:{}),...(full?{full:'true'}:{})});
export const empty=(title,body,action='')=>`<div class="empty"><h2>${esc(title)}</h2><p>${esc(body)}</p>${action}</div>`;
export function modal(title,content,foot='',variant=''){$('#modal').dataset.variant=variant;$('#modal-body').innerHTML=`<div class="modal-head"><h2 id="modal-title">${esc(title)}</h2>${btn('关闭','close','','quiet small')}</div><div class="modal-content">${content}</div>${foot?`<div class="modal-foot">${foot}</div>`:''}`;if(!$('#modal').open)$('#modal').showModal()}
export function closeModal(){document.querySelectorAll('#modal video').forEach(v=>v.pause());$('#modal').close();$('#modal-body').innerHTML=''}
export const actions={close:closeModal};
document.addEventListener('click',async e=>{const b=e.target.closest('[data-act]');if(!b||b.disabled)return;const fn=actions[b.dataset.act];if(!fn)return;b.dataset.actionPending='true';b.disabled=true;try{await fn(b.dataset.id,b)}catch(err){toast(err.message)}finally{if(b.isConnected){b.disabled=false;delete b.dataset.actionPending}}});
document.addEventListener('error',e=>{if(e.target.tagName==='IMG'&&!e.target.hasAttribute('data-detail-image')&&!e.target.dataset.failed){e.target.dataset.failed='true';e.target.alt='预览暂不可用';e.target.style.opacity='.45'}},true);
window.addEventListener('offline',()=>{$('#network-alert').textContent='网络已断开，未保存的修改请保留在当前页面，连接恢复后重试。';$('#network-alert').hidden=false});

document.querySelector('.skip').addEventListener('click',e=>{e.preventDefault();document.querySelector('#main').focus()});

// Scrolling a form must not silently change a focused budget or trim value.
document.addEventListener('wheel',e=>{if(e.target instanceof HTMLInputElement&&e.target.type==='number'&&document.activeElement===e.target)e.target.blur()},{passive:true});

// Photo previews remain available to keyboard users as well as pointer users.
document.addEventListener('keydown',e=>{if(e.target.matches?.('.work-media img[data-act="large"]')&&['Enter',' '].includes(e.key)){e.preventDefault();e.target.click()}});

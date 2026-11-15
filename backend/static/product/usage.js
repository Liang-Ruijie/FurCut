import {esc,money} from './base.js';
export function duration(value){if(value==null)return '尚不能可靠估算';if(value<60)return '约'+Math.ceil(value)+'秒';return '约'+Math.ceil(value/60)+'分钟'}
export function estimateHTML(e,{runtimeNote=''}={}){
 const known=e.cost_low_minor!=null;
 const warning={insufficient:'当前预算明显低于预估，不建议开始整批处理。请减少素材或修改预算，再预估。',at_risk:'当前预算可能无法完成全部处理。中途暂停时，已发生费用不会退回，已完成分析保留。',unknown:'暂不能保证当前预算能完成全部处理，请不要把预算上限当成完成报价。',likely_sufficient:'当前预算覆盖预估范围，但不是完成保证；达到预算会暂停新请求。'}[e.budget_status];
 return `<div class="notice usage-estimate ${e.budget_status==='likely_sufficient'?'':'warn'}"><div class="estimate-grid"><div><small>预计费用范围</small><strong>${known?money(e.cost_low_minor)+' — '+money(e.cost_high_minor):'暂无可靠估算'}</strong></div><div><small>预计处理时间</small><strong>${known?duration(e.seconds_low)+' — '+duration(e.seconds_high):'暂无可靠估算'}</strong></div></div><p class="estimate-status">${esc(warning)}</p><div class="estimate-notes">${runtimeNote?`<p class="estimate-runtime">${esc(runtimeNote)}</p>`:''}<details class="estimate-basis"><summary>预估依据</summary><div><p>${esc(e.basis)}</p><p>${esc(e.notice)}</p></div></details></div>${e.already_spent_minor?`<p>本任务已记录${money(e.already_spent_minor)}；以上为整批参考，不代表剩余费用。已完成分析会按原身份复用。</p>`:''}</div>`;
}
export function processingHTML(p){
 const s=p.processing;if(!s)return `<p>${esc(p.job?.stage||'正在准备处理')}</p><progress></progress>`;
 const count=s.count;
 return `<div class="processing-stages" aria-label="处理阶段">${s.steps.map((name,i)=>`<span class="${i===s.index?'active':i<s.index?'past':''}" ${i===s.index?'aria-current="step"':''}>${esc(name)}</span>`).join('')}</div><p class="processing-description">${esc(s.description)}</p><div class="row between"><small>${s.current_asset?esc(s.current_asset)+' · ':''}已完成${p.completed_assets}/${p.total_assets}个素材</small><small>本任务累计耗时 ${duration(s.elapsed_seconds).replace('约','')}</small></div>${count?`<progress value="${Math.min(count.done,count.total)}" max="${count.total}"></progress><p class="hint">当前阶段已完成 ${count.done}/${count.total}${esc(count.unit)}，不是全任务完成比例。</p>`:'<progress aria-label="当前阶段正在处理"></progress><p class="hint">'+esc(s.detail||'正在处理')+' · 本阶段暂无可靠总量，不显示虚假百分比。</p>'}<p class="hint">${esc(s.note)}</p>`;
}

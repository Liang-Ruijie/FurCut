import {api,post,toast} from './base.js';

// The displayed version is the edit baseline. Reading history never writes preferences.
export function bindPreferenceForm(form,source,reload,{isNew=false}={}){
 const names=['required','priority','bonus','action_bonus'];
 const actions=form.querySelector('.preference-actions'),save=actions.querySelector('[type=submit]'),cancel=actions.querySelector('[type=button]'),message=form.querySelector('#preference-save-status');
 const history=form.querySelector('#pref-history'),best=form.elements.best,hold=form.elements.hold;
 let busy=false,uncertain=false,pending=null,nameOnly=false;
 const standard=v=>JSON.stringify({...v,name:undefined});
 const reread=()=>reload(nameOnly?source.version:undefined);
 const values=()=>({name:form.elements.display_name.value.trim(),rules:Object.fromEntries(names.map(n=>[n,form.elements[n].value.split(/[,，]/).map(s=>s.trim()).filter(Boolean)])),best:best.checked,hold:+hold.value});
 const signature=v=>JSON.stringify(v);
 const fromSaved=p=>({name:p.display_name||'',rules:{required:p.quality_floor.required||[],priority:p.visual_rules.priority||[],bonus:p.visual_rules.bonus||[],action_bonus:p.visual_rules.action_bonus||[]},best:!!p.export_defaults.best_frame_enabled,hold:(p.export_defaults.best_frame_hold_ms??300)/1000});
 let baseline=signature(fromSaved(source));
 function status(text,kind='hint'){message.textContent=text;message.className=kind;message.hidden=!text;}
 function sync(){
  const dirty=isNew||signature(values())!==baseline;
  form.querySelector('#preference-name-count').textContent=Array.from(form.elements.display_name.value).length+'/12';
  form.dataset.unsaved=String(dirty||uncertain);
  actions.hidden=!dirty&&!uncertain&&!busy;
  form.elements.display_name.disabled=busy||uncertain;
  names.forEach(n=>form.elements[n].disabled=busy||uncertain);
  best.disabled=busy||uncertain;history.disabled=busy||uncertain||isNew;form.querySelector('#new-preference').disabled=busy||uncertain;
  hold.disabled=busy||uncertain||!best.checked;
  form.querySelector('.preference-download-settings').dataset.enabled=String(best.checked);
  const valid=form.elements.required.value.split(/[,，]/).some(s=>s.trim())&&(!best.checked||(hold.value!==''&&hold.checkValidity()));
  save.disabled=busy||(!uncertain&&(!dirty||!valid));cancel.disabled=busy;
  save.textContent=busy?'保存中…':uncertain?'确认保存状态':standard(values())===standard(JSON.parse(baseline))&&!isNew?'保存名称':'另存为新版本';
 }
 form.addEventListener('input',e=>{if(e.target===history)return;status('');sync()});
 form.addEventListener('change',e=>{if(e.target===history)return;status('');sync()});
 cancel.onclick=async()=>{
  if(uncertain){
   if(!confirm('上次保存结果尚未确认，将重新读取已保存偏好；这不会撤销服务器已保存的内容。继续吗？'))return;
   busy=true;sync();try{await reread()}catch(e){status('重新读取失败：'+e.message,'notice error')}finally{busy=false;sync()}
   return;
  }
  if(isNew){await reload(source.version);return}form.reset();status('');sync();
 };
 form.onsubmit=async e=>{
  e.preventDefault();if(busy)return;
  if(!uncertain&&((!isNew&&signature(values())===baseline)||!form.reportValidity()))return;
  busy=true;sync();
  try{
   if(uncertain){
    const latest=await api('/api/v1/preferences'+(nameOnly?'?version='+source.version:''));
    if(signature(fromSaved(latest))===signature(pending)){
     baseline=signature(pending);uncertain=false;status('已确认偏好保存成功');toast(nameOnly?'版本名称已保存':'偏好已保存，新任务可复用');
     try{await reread()}catch{status('已确认保存成功；页面暂未重新读取，请刷新。','notice')}
    }else{uncertain=false;status('未读取到本次修改。内容已保留，请核对后再次保存。','notice warn')}
    return;
   }
   pending=values();
   nameOnly=!isNew&&standard(pending)===standard(JSON.parse(baseline));
   // A disabled duration retains the last saved valid value; it cannot block disabling the option.
   if(!pending.best&&(!Number.isFinite(pending.hold)||pending.hold<0||pending.hold>5))pending.hold=fromSaved(source).hold;
   const payload={display_name:pending.name,quality_floor:{...source.quality_floor,required:pending.rules.required},visual_rules:{...source.visual_rules,priority:pending.rules.priority,bonus:pending.rules.bonus,action_bonus:pending.rules.action_bonus},export_defaults:{...source.export_defaults,best_frame_enabled:pending.best,best_frame_hold_ms:Math.round(pending.hold*1000)}};
   status('正在保存…');
   if(nameOnly)await api('/api/v1/preferences/'+source.version+'/name',{method:'PATCH',body:JSON.stringify({display_name:pending.name})});else await post('/api/v1/preferences',payload);
   baseline=signature(pending);form.dataset.unsaved='false';toast(nameOnly?'版本名称已保存':'偏好已保存，新任务可复用');
   try{await reread()}catch{status('偏好已保存；页面暂未重新读取，请刷新。','notice')}
  }catch(error){
   uncertain=![400,401,403,404,409,413,422,429].includes(error.status);
   status((uncertain?'保存结果尚未确认：':'保存失败：')+error.message+(uncertain?'。点击“确认保存状态”会先读取结果，不直接重复保存。':'。修改内容已保留。'),'notice '+(uncertain?'warn':'error'));
  }finally{busy=false;sync()}
 };
 sync();
}

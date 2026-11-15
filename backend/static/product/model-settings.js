import {api,post,toast} from './base.js';

// Each supplier owns a draft. Switching never relabels another supplier's endpoint.
const names={deepseek:'DeepSeek',dashscope:'阿里云百炼'};
export function endpointProblem(ep){
 for(const key of ['model','base_url','api_key_ref'])if(!ep[key]?.trim())return '请填写模型名称、服务地址和服务端密钥引用。';
 let url;try{url=new URL(ep.base_url)}catch{return '请填写完整的服务地址。'}
 if(!['http:','https:'].includes(url.protocol)||url.username||url.password)return '服务地址须为 HTTP/HTTPS，且不能包含密钥或账号密码。';
 const other=ep.provider==='dashscope'?'deepseek':'dashscope';
 if(url.hostname.split('.').includes(other)||ep.provider==='dashscope'&&/^deepseek[-_]/i.test(ep.model)||ep.provider==='deepseek'&&/^qwen/i.test(ep.model)||ep.api_key_ref===(ep.provider==='dashscope'?'DEEPSEEK_API_KEY':'DASHSCOPE_API_KEY'))return '服务商与模型、地址或密钥引用不一致，请检查后再保存。';
 return '';
}
export function bindModelForm(form,cfg,reload){
 const drafts=new Map(), fields={model:'model',base:'base_url',ref:'api_key_ref'};
 let provider=form.provider.value,history={},saving=false,uncertain=false;
 const endpoint=()=>({provider:form.provider.value,...Object.fromEntries(Object.entries(fields).map(([name,key])=>[key,form.elements[name].value.trim()]))});
 const empty=p=>({provider:p,model:'',base_url:'',api_key_ref:''});
 drafts.set(provider,cfg?.vlm?{...cfg.vlm}:empty(provider));
 const note=document.createElement('div');note.id='model-config-status';note.className='notice';note.setAttribute('aria-live','polite');form.provider.closest('label').after(note);
 const button=form.querySelector('button'),actionBar=form.querySelector('.model-actions');
 const readBack=document.createElement('button');readBack.type='button';readBack.className='quiet';readBack.textContent='取消修改';button.after(readBack);
 readBack.onclick=async()=>{if(uncertain){if(!confirm('上次保存结果尚未确认，将重新读取已保存配置；这不会撤销服务器已保存的内容。继续吗？'))return;readBack.disabled=true;try{await reload()}catch(e){status('重新读取失败：'+e.message,true)}finally{readBack.disabled=false}return}form.reset();provider=form.provider.value;drafts.clear();drafts.set(provider,cfg?.vlm?{...cfg.vlm}:empty(provider));indicate();};
 function status(message,error=false){note.textContent=message;note.className='notice '+(error?'error':'');}
 function indicate(){const ep=endpoint(),same=cfg&&['provider','model','base_url','api_key_ref'].every(k=>ep[k]===(cfg.vlm[k]||''));
 form.dataset.unsaved=String(!same||['fps','frames','edge'].some((n,i)=>+form.elements[n].value!==+(cfg?.sampling?.[['coarse_fps','coarse_max_frames','coarse_resize_edge'][i]]??[.5,120,600][i])));
 const saved=form.dataset.unsaved==='false';
 actionBar.hidden=saved&&!uncertain&&!saving;
 status(saved?'当前配置：'+(names[provider]||provider)+' · 第'+cfg.version+'版。修改后请保存，仅用于新建任务。'+(!cfg.vlm.key_configured?' 服务端密钥尚未配置，请联系管理员。':''):!ep.model?'尚未配置 '+(names[provider]||provider)+'。请填写已开通的视觉模型及对应地址、密钥引用。':'当前修改尚未保存，保存后仅用于新建任务。',saved&&!cfg.vlm.key_configured);
 }
 form.addEventListener('input',e=>{if(e.target===form.provider)return;drafts.set(provider,endpoint());indicate()});
 form.provider.onchange=()=>{
  const previous=drafts.get(provider)||empty(provider);drafts.set(provider,{...previous,...Object.fromEntries(Object.entries(fields).map(([name,key])=>[key,form.elements[name].value]))});
  provider=form.provider.value;const ep=drafts.get(provider)||history[provider]||empty(provider);drafts.set(provider,{...ep});
  for(const [name,key] of Object.entries(fields))form.elements[name].value=ep[key]||'';
  form.model.placeholder='填写已开通的视觉模型名称';form.base.placeholder='填写该服务商的 API 地址';form.ref.placeholder='填写服务端环境变量名';
  indicate();
 };
 // History is optional; failure must not disable editing or silently replace a draft.
 api('/api/v1/configs/providers').then(data=>{history=Object.fromEntries(data.configs.map(c=>[c.vlm.provider,c.vlm]));const current=drafts.get(provider);if(current&&!current.model&&!current.base_url&&!current.api_key_ref&&history[provider]){drafts.set(provider,{...history[provider]});for(const [name,key] of Object.entries(fields))form.elements[name].value=history[provider][key]||'';indicate()}}).catch(()=>{});
 indicate();
 form.onsubmit=async e=>{
  e.preventDefault();if(saving)return;const ep=endpoint(),problem=endpointProblem(ep);
  if(problem){status(problem,true);toast(problem);return}
  saving=true;button.disabled=true;button.textContent='正在保存…';
  // Freeze controls until the receipt is known; no edits can disappear on success.
  const controls=[...form.elements];controls.forEach(c=>c.disabled=true);
  try{if(uncertain){const saved=await api('/api/v1/configs');uncertain=false;if(['provider','model','base_url','api_key_ref'].every(k=>saved.vlm[k]===ep[k])&&['fps','frames','edge'].every((n,i)=>+form.elements[n].value===+saved.sampling[['coarse_fps','coarse_max_frames','coarse_resize_edge'][i]])){form.dataset.unsaved='false';toast('已确认服务器保存成功');await reload()}else status('已重新读取服务器状态，尚未找到与本页一致的配置。内容已保留，核对后可再次保存。',true);return}
   await post('/api/v1/configs',{model_config_id:'default',vlm:ep,llm:cfg?.llm||ep,sampling:{...cfg?.sampling,coarse_fps:+form.fps.value,coarse_max_frames:+form.frames.value,coarse_resize_edge:+form.edge.value}});
   form.dataset.unsaved='false';toast('配置已保存为新版本；已有任务保持原配置');await reload();
  }catch(error){uncertain=![400,401,403,404,409,413,422,429].includes(error.status);status((uncertain?'保存未确认：':'保存失败：')+error.message+'。填写内容已保留。'+(uncertain?'再次点击会先读取服务器状态，不直接重复保存。':''),true);}
  finally{saving=false;controls.forEach(c=>c.disabled=false);button.textContent=uncertain?'确认保存状态':'保存配置';}
 };
}

// A new selection must survive a visit to preferences or a page reload.
export function readDraft(raw){
 let value;try{value=JSON.parse(raw||'null')}catch{}
 if(!value||typeof value!=='object'||Array.isArray(value))value={};
 const result={...value,items:Array.isArray(value.items)?value.items.filter(i=>i&&typeof i.asset_id==='string'):[],title:typeof value.title==='string'?value.title:'',requirement:typeof value.requirement==='string'?value.requirement:'',budget:Number.isFinite(+value.budget)&&+value.budget>0?+value.budget:1};
 return result;
}
export function rememberOptions(draft,form){
 Object.assign(draft,{title:form.title.value,requirement:form.requirement.value,budget:+form.budget.value,prefVersion:form.pref.value,best:form.best.checked,hold:+form.hold.value});
}
export function selectionOptions(draft,pref){
 return {best:typeof draft.best==='boolean'?draft.best:!!pref.export_defaults.best_frame_enabled,hold:Number.isFinite(draft.hold)?draft.hold:(pref.export_defaults.best_frame_hold_ms??300)/1000};
}

// Persisted browser IDs can outlive archived tasks or a restored workspace.
export function reconcileDraftItems(items,assets,validateIds=null){
 const available=new Map(assets.filter(a=>a.available!==false).map(a=>[a.asset_id,a]));
 let removed=0;
 const kept=items.flatMap(item=>{
  if(!item.asset_id||item.file||(validateIds&&!validateIds.has(item.asset_id)))return [item];
  const current=available.get(item.asset_id);
  if(!current){removed++;return []}
  return [{...item,name:current.name,size_bytes:current.size_bytes,media_type:current.media_type,status:'ready',uid:item.asset_id}];
 });
 return {items:kept,removed};
}

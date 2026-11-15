import {esc,imageURL,empty} from './base.js';

// Both task editing and creation use the same available/missing-file layout.
export function libraryHTML(assets,selected,name){
 if(!assets.length)return empty('还没有上传素材','返回页面添加照片或视频。');
 return `<div class="library-grid">${assets.map(a=>{const available=a.available!==false;return `<label class="library-item ${available?'':'unavailable'}"><span class="library-preview">${available?`<img loading="lazy" src="${imageURL(a)}" alt="${esc(a.name)}">`:'<span class="unavailable-preview">原文件待恢复</span>'}</span><span class="library-caption"><input type="checkbox" name="${esc(name)}" value="${esc(a.asset_id)}" ${available?'':'disabled'} ${selected.includes(a.asset_id)?'checked':''}><span class="library-filename" title="${esc(a.name)}">${esc(a.name)}</span></span></label>`}).join('')}</div>`;
}

// Shared feedback for free estimates. Minimum visibility avoids a blink on fast responses.
export async function estimateFeedback(work){
 const started=performance.now();
 try{return await work()}
 finally{await new Promise(resolve=>setTimeout(resolve,Math.max(0,600-(performance.now()-started))))}
}
export function estimateLabel(text,busy=false){
 return `<span class="estimate-spinner" aria-hidden="true" ${busy?'':'hidden'}></span><span class="estimate-button-label">${text}</span>`;
}
export function setEstimateButton(button,busy,text){
 if(!button)return;
 button.classList.add('estimate-trigger');
 button.setAttribute('aria-busy',String(busy));
 if(!button.querySelector('.estimate-button-label'))button.innerHTML=estimateLabel(text,busy);
 else{button.querySelector('.estimate-spinner').hidden=!busy;button.querySelector('.estimate-button-label').textContent=text}
}
export function estimateLoadingHTML(){
 return '<p class="estimate-loading" role="status"><span class="estimate-spinner" aria-hidden="true"></span>正在免费预估费用与时间…</p>';
}

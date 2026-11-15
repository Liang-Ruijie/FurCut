// Approved legacy refinement is now the default; no preview/original mode switch.
function enhance() {
  const form = document.querySelector('#new-form');
  if (form && !form.querySelector('.polish-options')) {
    const best = form.querySelector('[name="best"]')?.closest('label');
    const hold = form.querySelector('[name="hold"]')?.closest('label');
    if (best && hold) {
      // Move the existing controls within the same form: do not copy values or change submission.
      const details = document.createElement('details');
      details.className = 'polish-options';
      const summary = document.createElement('summary');
      summary.textContent = '视频下载选项 · 最佳帧开头版';
      details.append(summary);
      best.before(details);
      details.append(best, hold);
    }
  }
  const drop = document.querySelector('#dropzone');
  if (drop) drop.tabIndex = 0;
  if (drop && !drop.dataset.keyboardReady) {
    drop.dataset.keyboardReady = 'true';
    drop.tabIndex = 0;
    drop.addEventListener('keydown', event => {
      if (event.target === drop && ['Enter', ' '].includes(event.key)) {
        event.preventDefault();
        drop.querySelector('input[type=file]')?.click();
      }
    });
  }
}


new MutationObserver(enhance).observe(document.querySelector('#main'), { childList:true, subtree:true });
enhance();

// Unsaved settings are deliberate edits; navigating away must not silently lose them.
const editedSettings=()=>document.querySelector('#model-form[data-unsaved="true"],#preference-form[data-unsaved="true"],#task-settings-form[data-unsaved="true"]');
document.addEventListener('click',event=>{
 const link=event.target.closest('a[href]');
 if(link&&editedSettings()&&!event.defaultPrevented&&link.href!==location.href){
  if(!confirm('设置尚未保存，离开会放弃当前修改。继续离开吗？')){event.preventDefault();event.stopImmediatePropagation()}
  else editedSettings().dataset.unsaved='false';
 }
},true);
window.addEventListener('beforeunload',event=>{if(editedSettings()){event.preventDefault();event.returnValue=''}});

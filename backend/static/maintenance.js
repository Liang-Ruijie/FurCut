// Display helpers only. Maintenance actions continue to use the existing APIs.
window.Maintenance={
 escape:value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
 status:value=>({created:'待准备',assets_locked:'准备就绪',analyzing:'处理中',selecting:'精选中',exporting:'导出中',ready_for_review:'待审核',completed:'审核已确认',failed:'有异常',cancelled:'已取消',pending_reconciliation:'费用待核对'}[value]||value),
 date:value=>value?new Date(value).toLocaleString('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}):'—'
};

// The library editor replaces only items represented in the loaded library.
// Pending uploads and items outside that snapshot remain in the draft.
export function applyLibrarySelection(items, assets, selectedIds) {
  const library = new Map(assets.map(a => [a.asset_id, a]));
  const selected = new Set(selectedIds.filter(id => library.has(id) && library.get(id).available !== false));
  const result = items.filter(i => !library.has(i.asset_id) || selected.has(i.asset_id));
  const present = new Set(result.map(i => i.asset_id).filter(Boolean));
  for (const id of selected) {
    if (!present.has(id)) {
      result.push({...library.get(id), uid:id, status:'ready'});
      present.add(id);
    }
  }
  return result;
}

export function visibleVideos(view, alternatives=false) {
  const rows=[...(view.video_segments||[]),...[...(view.unverified_videos||[]),...(view.video_fragments||[])].filter(c=>alternatives||c.review_decision)];
  return [...new Map(rows.map(c=>[c.candidate_id,c])).values()];
}

// Tabs and cards share the same collection, including reviewed alternatives.
export function galleryItems(view, kind, alternatives=false, filter='all') {
  const recommended=new Set((view.recommended_photos||[]).map(c=>c.candidate_id));
  const rows=kind==='photos'
    ? (view.photos||[]).filter(c=>alternatives||recommended.has(c.candidate_id)||c.review_decision)
    : visibleVideos(view,alternatives);
  return rows.filter(c=>filter==='all'||(filter==='unreviewed'?!c.review_decision:c.review_decision===filter));
}

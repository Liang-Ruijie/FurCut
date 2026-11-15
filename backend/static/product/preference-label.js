export function preferenceLabel(p){return p?.version?(p.display_name?`${p.display_name} · v${p.version}`:`v${p.version}`):'默认偏好'}
export function versionLabel(setup,version){return preferenceLabel((setup.preference_labels||[]).find(p=>String(p.version)===String(version))||{version})}

// Missing bounds must never be interpreted as a free or instant task.
export function estimateGate(e,budget){
 const cost=[e?.cost_low_minor,e?.cost_high_minor],time=[e?.seconds_low,e?.seconds_high];
 if(!cost.every(n=>Number.isFinite(n)&&n>=0)||cost[1]<cost[0]||!time.every(n=>Number.isFinite(n)&&n>=0)||time[1]<time[0])return 'unknown';
 return Number.isFinite(budget)&&budget>=cost[1]?'ready':'insufficient';
}

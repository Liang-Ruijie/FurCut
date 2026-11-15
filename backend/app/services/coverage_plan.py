"""Bounded uncertain/discarded-region audits derived from the current source, never example time lists."""
from __future__ import annotations


def complement(intervals,duration):
    merged=[]
    for start,end in sorted(intervals):
        start=max(0,int(start));end=min(duration,int(end))
        if start>=end:continue
        if merged and start<=merged[-1][1]:merged[-1][1]=max(end,merged[-1][1])
        else:merged.append([start,end])
    result=[];cursor=0
    for start,end in merged:
        if start>cursor:result.append((cursor,start))
        cursor=max(cursor,end)
    if cursor<duration:result.append((cursor,duration))
    return result


def audit_windows(annotations,selected,duration,uncertain_limit=4,discard_limit=3):
    def covered(t):return any(start<=t<=end for start,end in selected)
    candidates=[]
    for frame in annotations:
        obs=frame.get('observations') or {};score=float(frame.get('aes_score') or 0)
        ambiguous=obs.get('eye_state') in {'unclear','unknown'} or obs.get('face_visible') in {'partial','unclear','unknown'} or obs.get('clarity')=='soft'
        if not covered(frame['t_ms']) and (ambiguous or .45<=score<.55):
            candidates.append((not ambiguous,abs(score-.55),frame['t_ms']))
    audits=[]
    for _,_,t in sorted(candidates):
        if len(audits)>=uncertain_limit:break
        start,end=max(0,t-750),min(duration,t+750)
        if end>start and not any(a['start_ms']<=t<=a['end_ms'] for a in audits):
            audits.append({'start_ms':start,'end_ms':end,'purpose':'uncertain'})
    remaining=complement([*selected,*[(a['start_ms'],a['end_ms']) for a in audits]],duration)
    total=sum(b-a for a,b in remaining)
    if total and discard_limit:
        # Stratify across all discarded time, so no fixed source point is favoured.
        for i in range(discard_limit):
            offset=(i+.5)*total/discard_limit
            for a,b in remaining:
                if offset<=b-a:
                    t=round(a+offset);break
                offset-=b-a
            start,end=max(0,t-750),min(duration,t+750)
            if end>start and not any(a['start_ms']<=t<=a['end_ms'] for a in audits):
                audits.append({'start_ms':start,'end_ms':end,'purpose':'discard_audit'})
    return audits


def photo_peak_windows(photos,duration,limit,processes=()):
    """Budgeted round-robin across observed processes; do not spend every window on one scene."""
    from .analysis import dedup_similar_photos,_photo_rank_key,SelectParams
    seeds=dedup_similar_photos([f for f in photos if f.get('promo_worthy') is True],set())
    groups=[];by_process={}
    for f in sorted(seeds,key=lambda f:f['t_ms']):
        process=next((i for i,s in enumerate(processes) if s['start_ms']<=f['t_ms']<=s['end_ms']),None)
        if process is not None:
            by_process.setdefault(process,[]).append(f)
            continue
        if not groups or f['t_ms']-groups[-1][-1]['t_ms']>SelectParams().gap_ms:groups.append([])
        groups[-1].append(f)
    groups.extend(by_process.values())
    for group in groups:group.sort(key=lambda f:_photo_rank_key(f,set()))
    groups.sort(key=lambda g:_photo_rank_key(g[0],set()))
    planned=[];depth=0
    while len(planned)<limit and any(depth<len(g) for g in groups):
        for group in groups:
            if len(planned)>=limit:break
            if depth>=len(group):continue
            t=group[depth]['t_ms']
            if any(e['start_ms']<=t<=e['end_ms'] for e in planned):continue
            a,b=max(0,t-250),min(duration,t+250)
            if a<b:planned.append({'start_ms':a,'end_ms':b,'purpose':'photo_peak','seed_ms':t})
        depth+=1
    return planned

"""Auditable visual inputs and non-destructive comparison, not a released policy.

No source-specific times, review answers, enhancement or generated pixels.
Every detail view comes from one exact decoded source frame. Ranking alone
never proves another frame is redundant.
"""
import hashlib
import json
from pathlib import Path
from ..media.sample import extract_frame_at, _pil_to_data_url

PROMPT = '''只核对眼睛和脸部是否可见，不评价好看、宣传价值或动作大小。
图像标有source_frame和view_id。同一source_frame的局部视图只是原图的裁片，不是另一时刻，不把裁片边缘当作原片裁切。
输出一个JSON对象 {"observations":[{"source_frame":"...","eyes_visible":"both|one|none|unclear","eye_state":"open|narrowed|closed|unclear","face_visibility":"full|side|partial|hidden|unclear","evidence_view_ids":["..."],"visible_evidence":"具体说哪只眼/脸部哪些部位可见，不猜测"}]}。
逐个source_frame独立判断。不知道用unclear，不能根据旁边照片补出看不到的眼睛。'''


def packet(path: Path, *, source_sha256: str, source_pts: int, time_base: str,
           t_ms: int, mode: str):
    if mode not in {'small','large','detail'}:
        raise ValueError('Unknown visual input mode')
    img, actual = extract_frame_at(path,t_ms,source_pts=source_pts,time_base=time_base)
    if img is None:
        raise ValueError('Exact source frame missing; no neighbouring-frame fallback')
    w,h=img.size
    identity=f'{source_sha256}:{time_base}:{source_pts}'
    source_frame=hashlib.sha256(identity.encode()).hexdigest()[:24]
    boxes=[(0,0,w,h)]
    if mode=='detail':
        # Full coverage with overlapping tiles; no assumed cat/face position.
        cw,ch=(w*2+2)//3,(h*2+2)//3
        boxes += [(x,y,x+cw,y+ch) for y in (0,h-ch) for x in (0,w-cw)]
    views=[]
    for n,box in enumerate(boxes):
        crop=img.crop(box)
        url=_pil_to_data_url(crop,600 if mode=='small' else 1536 if mode=='large' else 1024,90)
        import base64
        raw=base64.b64decode(url.split(',',1)[1])
        views.append({'view_id':f'{source_frame}-v{n}','bbox':list(box),'data_url':url,
                      'jpeg_sha256':hashlib.sha256(raw).hexdigest()})
    return {'source_frame':source_frame,'source_sha256':source_sha256,'source_pts':source_pts,
            'time_base':time_base,'actual_ms':actual,'source_dimensions':[w,h],
            'source_pixel_sha256':hashlib.sha256(img.convert('RGB').tobytes()).hexdigest(),
            'mode':mode,'views':views}


def messages(packets):
    ids=[p['source_frame'] for p in packets]
    if not ids or len(set(ids))!=len(ids):raise ValueError('Duplicate source frame')
    blocks=[]
    for p in packets:
        for v in p['views']:
            blocks += [{'type':'text','text':json.dumps({'source_frame':p['source_frame'],
                       'view_id':v['view_id'],'source_dimensions':p['source_dimensions'],'crop_bbox':v['bbox']})},
                       {'type':'image_url','image_url':{'url':v['data_url']}}]
    return [{'role':'system','content':PROMPT},{'role':'user','content':blocks}]


def parse(text, packets):
    raw=text.strip()
    if raw.startswith('```'):raw=raw.strip('`').removeprefix('json').strip()
    d=json.loads(raw);obs=d.get('observations') if isinstance(d,dict) else None
    known={p['source_frame']:p for p in packets}
    if not isinstance(obs,list) or len(obs)!=len(known) or any(not isinstance(o,dict) for o in obs) or {o.get('source_frame') for o in obs}!=known.keys():
        raise ValueError('Missing or duplicate frame observations')
    for o in obs:
        for field,allowed in {'eyes_visible':{'both','one','none','unclear'},'eye_state':{'open','narrowed','closed','unclear'},'face_visibility':{'full','side','partial','hidden','unclear'}}.items():
            if o.get(field) not in allowed:raise ValueError('Missing '+field)
        views={v['view_id'] for v in known[o['source_frame']]['views']}
        cited=o.get('evidence_view_ids')
        if not isinstance(cited,list) or not cited or any(not isinstance(i,str) or i not in views for i in cited):
            raise ValueError('Evidence belongs to another frame or is missing')
        if not isinstance(o.get('visible_evidence'),str) or not o['visible_evidence'].strip():raise ValueError('Missing visible evidence')
    return d


def comparison_plan(packets, observations, preferred_id):
    """Ranking is not deletion. Conflicting/uncertain observations block ranking.

    Caller supplies independently collected observations, not rewritten by the
    comparator. Agreement is a consistency check, not proof of correctness.
    Only equal decoded pixels can collapse items here; semantic dedup is separate.
    """
    ids=[p['source_frame'] for p in packets]
    if len(ids)!=2 or len(set(ids))!=2 or preferred_id not in ids:
        raise ValueError('Invalid pair')
    fields=('eyes_visible','eye_state','face_visibility')
    valid={'eyes_visible':{'both','one','none'},'eye_state':{'open','narrowed','closed'},
           'face_visibility':{'full','side','partial','hidden'}}
    if any(o.get('source_frame') not in ids for o in observations):raise ValueError('Unrelated observations')
    for i in ids:
        rows=[o for o in observations if o.get('source_frame')==i]
        if not rows or any(any(o.get(f) not in valid[f] for f in fields) for o in rows):
            return {'status':'needs_observation','ordered_ids':ids,'suppressed_ids':[]}
        if len({tuple(o.get(f) for f in fields) for o in rows})!=1:
            return {'status':'observation_conflict','ordered_ids':ids,'suppressed_ids':[]}
    other=next(i for i in ids if i!=preferred_id)
    same=all(packets[0].get(k) and packets[0][k]==packets[1].get(k) for k in ('source_dimensions','source_pixel_sha256'))
    return {'status':'ranked_not_quality_verified','ordered_ids':[preferred_id,other],
            'suppressed_ids':[other] if same else [],'semantic_duplicate_decision':'not_established' if not same else 'identical_pixels'}


def observation_registry(packets, observations):
    """Keep disagreements visible instead of silently trusting the latest answer.

    Agreement means consistency, never ground truth. Entries bind observations
    to source pixels, not timestamps or the current position in an image list.
    """
    fields=('eyes_visible','eye_state','face_visibility')
    ids={p['source_frame'] for p in packets}
    if len(ids)!=len(packets) or any(o.get('source_frame') not in ids for o in observations):
        raise ValueError('Duplicate packet or unrelated observation')
    result=[]
    for p in packets:
        rows=[o for o in observations if o['source_frame']==p['source_frame']]
        values={f:sorted({o.get(f,'unclear') for o in rows}) for f in fields}
        facts={f:v[0] for f,v in values.items() if len(v)==1 and v[0]!='unclear'}
        conflicts={f:v for f,v in values.items() if len(v)!=1 or 'unclear' in v}
        binding={k:p[k] for k in ('source_frame','source_sha256','source_pts','time_base','source_pixel_sha256')}
        result.append({**binding,'facts':facts,'conflicts':conflicts,'observation_count':len(rows),
                       'status':'consistent_not_truth_verified' if not conflicts else 'needs_observation'})
    return result


def validate_comparison_claims(registry, claims):
    """A comparator may rank values; it may not invent/change observed facts.

    This validates structured claims only, not arbitrary prose or aesthetics.
    Rejected comparison is unresolved, never an automatic photo rejection.
    """
    expected={r['source_frame']:r for r in registry}
    if not expected or len(expected)!=len(registry):
        raise ValueError('Missing or duplicate observation registry')
    if any(r['status']!='consistent_not_truth_verified' for r in registry):
        raise ValueError('Observation disagreement must be resolved before comparison')
    if not isinstance(claims,list) or len(claims)!=len(expected) or any(not isinstance(c,dict) for c in claims):
        raise ValueError('Missing comparison claims')
    if {c.get('source_frame') for c in claims}!=expected.keys():
        raise ValueError('Comparison uses a different frame')
    for claim in claims:
        r=expected[claim['source_frame']]
        if claim.get('source_pixel_sha256')!=r['source_pixel_sha256']:
            raise ValueError('Comparison pixel identity changed')
        if claim.get('facts_used')!=r['facts']:
            raise ValueError('Comparison contradicts locked observations; do not suppress candidate')
    return True

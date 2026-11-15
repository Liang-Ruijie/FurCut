"""Isolated hypothesis tests before enabling a new selection policy.

No history labels, filenames or preferred answers enter messages. Permutations use
stable image IDs. Video context cannot supply the retained interval's good core.
These evaluations share the production request/cost journal but do not publish
recommendations until separately validated.
"""
import json

FACT_PROMPT='''只描述图像中实际可见的事实，不评价可爱、好看或宣传价值。不从问题猜测答案。
逐个image_id输出JSON {"observations":[{"image_id":"...","head_direction":"front|left|right|down|away|unclear","body_pose":"upright|crouched|lying|unclear","raised_front_paws":0,"face_visibility":"full|side|partial|hidden|unclear","evidence":"具体可见依据"}]}。
左右按画面坐标描述；前爪看不清时raised_front_paws为null，不能默认抬爪。不知道就unclear。'''
PAIR_PROMPT='''比较两张同场景候选的独立宣传价值。两张均非指定参考，不知道任何人工标签。
分别观察脸部展示、眼神、表情、爪子自然度、主体构图；同样技术可用不代表同样值得精选。
输出JSON {"observations":[{"image_id":"...","strength":"具体优点或无","weakness":"具体不足或无"}],"relation":"a_better|b_better|both_distinct|equivalent|neither|uncertain","preferred_id":null,"reason":"基于可见差异说明"}。
a/b对应输入顺序。只有a_better或b_better需要preferred_id；有实质差异且各有价值可both_distinct。
静态和侧脸不扣死分，正脸抬爪不自动胜出；不能仅为必须选一张而造差异。'''
VIDEO_PROMPT='''先判断candidate区间内的连续观看价值，再借助outside_context判断自然起止。
外侧帧不能充当candidate的好核心，不能把前后好动作描述成区间内部已有动作。不要用圆眼、呆萌、抬爪的套话替代动作过程。
输出JSON {"candidate_event":"只描述区间内部发生的过程","candidate_evidence_ids":["..."],"outside_event":"仅外侧发生的过程或无","decision":"keep|trim|reject|uncertain","good_core_ids":["..."],"start_ms":null,"end_ms":null,"boundary_reason":"结合内外侧说明","reason":"独立观看是否值得保留"}。
keep/trim必须有区间内部实际好核心ID及整数起止；keep保留原边界，trim在上下文范围内给自然边界并包含内部好核心。
不能因片段短直接拒绝，正常眨眼不直接删除；反之动作不完整、持续低头背脸或遮挡不能凭外侧一帧好画面通过。
reject/uncertain时边界为null；观察不足用uncertain，不能猜测。'''


def _blocks(frames, scope=None):
    blocks=[]
    for f in frames:
        label={'image_id':f['image_id']}
        if scope is not None:label.update(scope=scope,t_ms=f['t_ms'])
        blocks.extend([{'type':'text','text':json.dumps(label,ensure_ascii=False)},
                       {'type':'image_url','image_url':{'url':f['data_url']}}])
    return blocks


def build(kind, frames, *, bounds=None):
    ids=[f['image_id'] for f in frames]
    if not ids or len(ids)!=len(set(ids)):raise ValueError('Images need unique IDs')
    if kind=='facts':return [{'role':'system','content':FACT_PROMPT},{'role':'user','content':_blocks(frames)}]
    if kind=='pair':
        if len(frames)!=2:raise ValueError('Pair requires two images')
        return [{'role':'system','content':PAIR_PROMPT},{'role':'user','content':_blocks(frames)}]
    if kind!='video' or not bounds or bounds[0]>=bounds[1]:raise ValueError('Invalid evaluation')
    inside=[f for f in frames if bounds[0]<=f['t_ms']<bounds[1]]
    outside=[f for f in frames if not bounds[0]<=f['t_ms']<bounds[1]]
    if not inside:raise ValueError('No candidate images')
    return [{'role':'system','content':VIDEO_PROMPT},
            {'role':'user','content':[{'type':'text','text':f'candidate 原边界 {bounds[0]}—{bounds[1]} ms；先只观察以下内部画面。'}]+_blocks(inside,'candidate')},
            {'role':'user','content':[{'type':'text','text':'以下仅为outside_context；不能拿这里的好画面替代candidate的观看价值。'}]+_blocks(outside,'outside_context')}]


def parse(kind,text,frames,*,bounds=None):
    raw=text.strip()
    if raw.startswith('```'):raw=raw.strip('`').removeprefix('json').strip()
    d=json.loads(raw)
    if not isinstance(d,dict):raise ValueError('Expected object')
    ids={f['image_id'] for f in frames}
    def reason(key,obj=d):
        if not isinstance(obj.get(key),str) or not obj[key].strip():raise ValueError('Missing '+key)
    def observations():
        obs=d.get('observations')
        if not isinstance(obs,list) or len(obs)!=len(ids) or {x.get('image_id') for x in obs if isinstance(x,dict)}!=ids:raise ValueError('Missing/duplicate image observations')
        return obs
    if kind=='facts':
        for o in observations():
            for k,values in {'head_direction':{'front','left','right','down','away','unclear'},'body_pose':{'upright','crouched','lying','unclear'},'face_visibility':{'full','side','partial','hidden','unclear'}}.items():
                if o.get(k) not in values:raise ValueError('Invalid '+k)
            if 'raised_front_paws' not in o:raise ValueError('Missing paws observation')
            if o.get('raised_front_paws') is not None and (type(o['raised_front_paws']) is not int or o['raised_front_paws'] not in (0,1,2)):raise ValueError('Invalid paws')
            reason('evidence',o)
    elif kind=='pair':
        for o in observations():reason('strength',o);reason('weakness',o)
        rel=d.get('relation')
        if rel not in {'a_better','b_better','both_distinct','equivalent','neither','uncertain'}:raise ValueError('Invalid comparison')
        expected=frames[0]['image_id'] if rel=='a_better' else frames[1]['image_id'] if rel=='b_better' else None
        if d.get('preferred_id')!=expected:raise ValueError('Winner conflicts with order')
        reason('reason')
    elif kind=='video':
        internal={f['image_id']:f['t_ms'] for f in frames if bounds[0]<=f['t_ms']<bounds[1]}
        for key in ('candidate_evidence_ids','good_core_ids'):
            values=d.get(key)
            if not isinstance(values,list) or any(not isinstance(v,str) for v in values) or len(set(values))!=len(values) or not set(values)<=internal.keys():raise ValueError('Outside/unknown frame used for candidate')
        if not d['candidate_evidence_ids']:raise ValueError('No internal evidence')
        for key in ('candidate_event','outside_event','boundary_reason','reason'):reason(key)
        decision=d.get('decision');a,b=d.get('start_ms'),d.get('end_ms')
        if decision in {'keep','trim'}:
            lo=min(bounds[0],*(f['t_ms'] for f in frames));hi=max(bounds[1],*(f['t_ms'] for f in frames))
            if type(a) is not int or type(b) is not int or not lo<=a<b<=hi:raise ValueError('Invalid boundaries')
            if decision=='keep' and (a,b)!=tuple(bounds):raise ValueError('Keep silently changed bounds')
            if not d['good_core_ids'] or not all(a<=internal[i]<b for i in d['good_core_ids']):raise ValueError('No retained candidate core')
        elif decision not in {'reject','uncertain'} or a is not None or b is not None:raise ValueError('Invalid rejection/uncertainty')
    else:raise ValueError('Unknown evaluation')
    return d


def evaluate(conn,task_id,kind,frames,transport,request_id,execution_id,*,bounds=None):
    from .compare import _guarded_call
    messages=build(kind,frames,bounds=bounds)
    response,cost_id=_guarded_call(conn,task_id,messages,transport,request_id,execution_id,max_retries=0)
    return {'assessment':parse(kind,response['text'],frames,bounds=bounds),'response':response,'cost_id':cost_id}

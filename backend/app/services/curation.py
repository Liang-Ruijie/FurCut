"""Versioned curation experiment contract; never publishes unvalidated decisions.

Labels stay in the evaluator, not candidate messages. A curated choice needs
positive evidence; a usable alternative is not counted as a rejection or a hit.
The same contract is used by the uncalibrated control to isolate rubric effects.
"""
import json
from . import grounded_selection as grounded

VERSION = 'curation-1.1'
RUBRIC = '''精选标准（由既有评价归纳，不是当前候选的答案）：
技术可用不等于值得独立交付。综合脸部完整饱满、眼睛可见状态、表情神态、爪子自然度、清晰度、构图与主体位置。
静态、完整侧脸可以优秀；不以正脸、圆眼、抬爪、动作多作为自动加分捷径。不把猫本身可爱或背景漂亮当作每张都精选的理由。
可用但没有明确亮点的内容可列备选；独立精选必须指出画面自身的价值。相似内容择优，有实质差异且各自优秀可都保留，不设必须淘汰数量。
照片的眯眼、脸部不可见、残影、遮挡、爪子别扭需具体观察；观察不清时标不确定，不能编造缺陷。
视频判断连续观看而非逐帧照片分数：自然静态展示也有观看价值，正常眨眼和短暂转头不自动切碎。短瞬间只要完整精彩可保留，不设最短时长。
抬爪不是价值的充分条件；需看脸部展示、动作自然、观看过程和构图。不能借外侧精彩帧批准内部差内容。
修边必须有具体内部缺陷和自然边界依据，保留实际优秀核心；不能仅因静态、侧脸或短而拒绝，也不能拼入差画面来凑长度。'''

PHOTO_SCHEMA = '''逐张独立观察，输出JSON {"items":[{"image_id":"...","tier":"curated|alternative|reject|uncertain","observations":{"face":"具体观察","eyes":"具体观察","pose":"具体观察","clarity":"具体观察","composition":"具体观察"},"positive_evidence":"明确亮点或无","limitation":"具体不足或无","distinct_value":"与同组其他内容的实质差异或无","reason":"选择理由"}]}。
不要用批次里必须选一张代替判断；tier=alternative表示可用备选，不等于淘汰。'''
VIDEO_SCHEMA = '''另加 value 字段 {"mode":"static_display|action|mixed|unclear","visible_value":"具体观看价值或无","defects":[{"code":"face_hidden|blur|occlusion|awkward_pose|incomplete_event|poor_composition","evidence_ids":["内部帧ID"],"description":"具体依据"}]}。
reject或trim必须给出内部缺陷证据。静态、侧脸、时长短不是defect；没有充分依据用uncertain。keep允许static_display，不要求抬爪。'''


def build(kind, frames, *, bounds=None, calibrated=True):
    if kind == 'photo':
        # Reuse ID/uniqueness validation without borrowing diagnostic answers.
        messages = grounded.build('facts', frames)
        messages[0]['content'] = PHOTO_SCHEMA
    elif kind == 'video':
        messages = grounded.build('video', frames, bounds=bounds)
        # One complete root object: the old separate "add value" example led
        # a real response to emit {...},"value":{...}}. Never repair that
        # response into a pass or silently alter previously frozen messages.
        ending = '"reason":"独立观看是否值得保留"}'
        assert ending in messages[0]['content']
        messages[0]['content'] = messages[0]['content'].replace(
            ending, '"reason":"独立观看是否值得保留","value":{"mode":"static_display|action|mixed|unclear",'
            '"visible_value":"具体观看价值或无","defects":[]}}', 1)
        messages[0]['content'] += '\n' + VIDEO_SCHEMA + '\n只输出一个合法JSON对象；value必须位于同一个根对象内部。'
    else:
        raise ValueError('Unknown curation kind')
    if calibrated:
        messages[0]['content'] += '\n' + RUBRIC
    return messages


def _text(obj, key):
    if not isinstance(obj.get(key), str) or not obj[key].strip():
        raise ValueError('Missing ' + key)


def parse(kind, text, frames, *, bounds=None):
    if kind == 'video':
        d = grounded.parse('video', text, frames, bounds=bounds)
        value = d.get('value')
        if not isinstance(value, dict) or value.get('mode') not in {'static_display','action','mixed','unclear'}:
            raise ValueError('Missing viewing value')
        _text(value, 'visible_value')
        defects = value.get('defects')
        if not isinstance(defects, list):
            raise ValueError('Missing defects')
        internal = {f['image_id'] for f in frames if bounds[0] <= f['t_ms'] < bounds[1]}
        for defect in defects:
            if not isinstance(defect, dict) or defect.get('code') not in {'face_hidden','blur','occlusion','awkward_pose','incomplete_event','poor_composition'}:
                raise ValueError('Unsupported defect; short/static/side are not defects')
            evidence = defect.get('evidence_ids')
            if not isinstance(evidence, list) or not evidence or any(not isinstance(i,str) or i not in internal for i in evidence):
                raise ValueError('Defect needs internal evidence')
            _text(defect, 'description')
        if d['decision'] in {'reject','trim'} and not defects:
            raise ValueError('Unsubstantiated removal; requires review, not automatic rejection')
        return d
    if kind != 'photo':
        raise ValueError('Unknown curation kind')
    raw = text.strip()
    if raw.startswith('```'):
        raw = raw.strip('`').removeprefix('json').strip()
    d = json.loads(raw)
    items = d.get('items') if isinstance(d,dict) else None
    ids = {f['image_id'] for f in frames}
    if not isinstance(items,list) or len(items)!=len(ids) or any(not isinstance(i,dict) for i in items) or {i.get('image_id') for i in items}!=ids:
        raise ValueError('Missing/duplicate candidate')
    for item in items:
        if item.get('tier') not in {'curated','alternative','reject','uncertain'}:
            raise ValueError('Invalid tier')
        obs = item.get('observations')
        if not isinstance(obs,dict):
            raise ValueError('Missing observations')
        for key in ('face','eyes','pose','clarity','composition'):
            _text(obs,key)
        for key in ('positive_evidence','limitation','distinct_value','reason'):
            _text(item,key)
        if item['tier']=='curated' and item['positive_evidence'].strip().lower() in {'无','none','unknown','不确定'}:
            raise ValueError('Curation requires positive evidence')
    return d


def eligible_references(targets, references):
    """Conservative holdout: no same-source reference, even at distant timestamps.

    Separate source files can be duplicates; caller must supply independently
    audited similarity-group IDs. Missing identity fails closed. No tolerance
    matching of review timestamps is performed here.
    """
    keys = ('source_sha256','image_sha256','similarity_group')
    if any(not all(t.get(k) for k in keys) for t in targets) or not targets:
        raise ValueError('Targets need audited identity')
    return [r for r in references if all(r.get(k) for k in keys)
            and all(all(r[k]!=t[k] for k in keys) for t in targets)]


def photo_metrics(items, labels):
    """Strict curated recall plus visible alternatives, without changing labels.

    labels is an evaluator-only ID→keep/reject mapping. Missing predictions are
    unresolved AND count as missing positives; abstention cannot fake recall.
    """
    predictions={i['image_id']:i['tier'] for i in items}
    if len(predictions)!=len(items) or not predictions.keys()<=labels.keys():
        raise ValueError('Duplicate or unlabelled prediction')
    if any(v not in {'keep','reject'} for v in labels.values()):
        raise ValueError('Unknown historical label')
    if any(v not in {'curated','alternative','reject','uncertain'} for v in predictions.values()):
        raise ValueError('Unknown tier')
    return {
        'false_curated':[i for i,v in labels.items() if v=='reject' and predictions.get(i)=='curated'],
        'missing_from_curated':[i for i,v in labels.items() if v=='keep' and predictions.get(i)!='curated'],
        'positive_in_alternatives':[i for i,v in labels.items() if v=='keep' and predictions.get(i)=='alternative'],
        'negative_in_alternatives':[i for i,v in labels.items() if v=='reject' and predictions.get(i)=='alternative'],
        'unresolved':[i for i in labels if predictions.get(i) in {None,'uncertain'}],
        'curated_count':sum(v=='curated' for v in predictions.values()),
        'review_count':sum(v in {'alternative','uncertain'} for v in predictions.values())+len(labels.keys()-predictions.keys()),
    }


def interval_metrics(retained, positive, negative, core_times):
    """Offline union metrics; overlap cannot double count or hide edge losses."""
    def union(xs):
        out=[]
        for a,b in sorted(xs):
            if a>=b:raise ValueError('Empty/reversed interval')
            if out and a<=out[-1][1]:out[-1][1]=max(b,out[-1][1])
            else:out.append([a,b])
        return out
    kept=union(retained)
    def overlap(a,b):
        return sum(max(0,min(b,y)-max(a,x)) for x,y in kept)
    rows=[]
    for a,b in positive:
        parts=[(max(a,x),min(b,y)) for x,y in kept if max(a,x)<min(b,y)]
        rows.append({'bounds':[a,b],'retained_ms':overlap(a,b),
                     'head_loss_ms':parts[0][0]-a if parts else b-a,
                     'tail_loss_ms':b-parts[-1][1] if parts else b-a,
                     'missing_ms':b-a-overlap(a,b)})
    return {'negative_residual_ms':sum(overlap(a,b) for a,b in union(negative)),
            'positive_intervals':rows,'cores':[{'t_ms':t,'retained':any(a<=t<b for a,b in kept)} for t in core_times],
            'note':'Core retention does not prove natural boundaries; intervals with no coverage lose both edges.'}

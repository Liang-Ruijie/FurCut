"""An auditable continuous-viewing contract; photo defects never become video cuts by keyword."""
import json

PROMPT = '''你是猫舍连续视频审核员。先逐帧观察，再判断连续观看；不能用一条“可爱/抬爪/无遮挡”的总评替代局部检查。
segment_usable 表示有值得交付的连续片段。按输入的每个 frame_id 输出且仅输出一次 evidence：
{"evidence":[{"frame_id":"f0","viewing":"good|transition|defect|uncertain","face":"full|side|insufficient|occluded","eyes":"open|half|closed|unclear","paws":"natural|awkward|not_visible","clarity":"sharp|soft|blurry","reason":"描述实际看见的局部事实"}],
"segment_usable":true,"segments":[{"start_ms":0,"end_ms":1000,"natural_start":true,"natural_end":true,"start_reason":"结合前一帧/外侧上下文说明为何此处开始","end_reason":"结合后一帧/外侧上下文说明为何此处结束","reason":"这段的观看价值"}],"omissions":[{"frame_id":"f2","reason":"解释为何未纳入这张区间内good画面，例如同一内容已有更完整版本"}],"reason":"总体结论"}
完整侧脸、静态可以good，正脸或抬爪不自动good。照片眯眼不合格与视频自然眨眼不同：自然眨眼/正常动作过渡可标transition；遮脸、背脸看不到足够面部、勾爪别扭、主体模糊应标defect，无法看准明确uncertain，不编造。
先核实脸/眼/爪的位置和清晰度，前景/背景景深不等于主体模糊；爪不入镜不等于别扭。持续坏过程不能用精彩帧的高分抵消。
segments可有零个、一个或多个，必须按时间排序且不重叠；不能包含任何defect或uncertain输入帧。内部坏区须保留两侧各自自然完整且值得观看的段，否则不要拼坏画面凑长度。优秀短瞬间可以保留，不按固定最短时长删。
完整合格段返回原受审边界，不得机械缩到采样中心点；可利用外侧上下文，开始和结束都必须自然。被省略的受审区间内good帧逐项在omissions解释，不能悄悄删优秀开头结尾。false时segments为空。
只输出JSON，时间使用输入标注的毫秒；不虚构未观察的精确动作边界。'''


class ContinuousParseError(ValueError):
    pass


def input_manifest(frames,candidate_bounds):
    entries=[{'frame_id':f'f{i}','t_ms':f['t_ms'],'scope':'candidate' if candidate_bounds[0]<=f['t_ms']<candidate_bounds[1] else 'outside_context'} for i,f in enumerate(frames)]
    return ('本次共'+str(len(entries))+'张输入。evidence必须包含清单中每个ID，包含首尾和外侧上下文，不能仅返回你选中片段内的帧。'
            '外侧动作的缺陷用于确定起止，不应被误算为保留段内坏帧。完整输入清单：'+json.dumps(entries,ensure_ascii=False))


def parse(text, frames, candidate_bounds, context_bounds):
    raw=(text or '').strip()
    if raw.startswith('```'):raw=raw.strip('`').removeprefix('json').strip()
    try:d=json.loads(raw)
    except (ValueError,TypeError) as exc:raise ContinuousParseError('连续判断不是JSON') from exc
    if not isinstance(d,dict) or not isinstance(d.get('segment_usable'),bool):raise ContinuousParseError('缺少连续判断结论')
    evidence=d.get('evidence');segments=d.get('segments');omissions=d.get('omissions',[])
    if not isinstance(evidence,list) or not isinstance(segments,list) or not isinstance(omissions,list):raise ContinuousParseError('缺少逐帧证据/区间/遗漏说明')
    expected={f'f{i}':f['t_ms'] for i,f in enumerate(frames)};seen={};conflicts={}
    enums={'viewing':{'good','transition','defect','uncertain'},'face':{'full','side','insufficient','occluded'},'eyes':{'open','half','closed','unclear'},'paws':{'natural','awkward','not_visible'},'clarity':{'sharp','soft','blurry'}}
    def reason(v):return isinstance(v,str) and bool(v.strip()) and len(v)<=1000
    for e in evidence:
        if not isinstance(e,dict) or e.get('frame_id') not in expected or e['frame_id'] in seen:raise ContinuousParseError('证据帧遗漏、重复或越界')
        if any(e.get(k) not in vals for k,vals in enums.items()) or not reason(e.get('reason')):raise ContinuousParseError('逐帧观察不完整')
        hard=e['face'] in {'insufficient','occluded'} or e['paws']=='awkward' or e['clarity']=='blurry'
        if hard and e['viewing'] not in {'defect','uncertain'}:conflicts[e['frame_id']]='可见硬伤与观看判断矛盾'
        if e['eyes']=='unclear' and e['viewing']=='good':conflicts[e['frame_id']]='眼睛观察不清不能充当好核心'
        seen[e['frame_id']]={**e,'t_ms':expected[e['frame_id']]}
    if set(seen)!=set(expected):raise ContinuousParseError('连续判断未覆盖全部输入帧')
    if bool(segments)!=d['segment_usable']:raise ContinuousParseError('可用结论与返回区间矛盾')
    result=[];last=context_bounds[0]
    for s in segments:
        if not isinstance(s,dict):raise ContinuousParseError('可用区间不是对象')
        a,b=s.get('start_ms'),s.get('end_ms')
        if type(a) is not int or type(b) is not int or not context_bounds[0]<=a<b<=context_bounds[1] or a<last:raise ContinuousParseError('区间越界、重叠或乱序')
        if s.get('natural_start') is not True or s.get('natural_end') is not True or any(not reason(s.get(k)) for k in ('start_reason','end_reason','reason')):raise ContinuousParseError('缺少自然起止及外侧上下文依据')
        inside=[e for e in seen.values() if a<=e['t_ms']<b]
        if any(e['frame_id'] in conflicts for e in inside):raise ContinuousParseError('保留段内可见硬伤与观看判断矛盾')
        if not any(e['viewing']=='good' for e in inside):raise ContinuousParseError('片段没有已观察到的好核心')
        if any(e['viewing'] in {'defect','uncertain'} for e in inside):raise ContinuousParseError('保留片段包含已知坏画面或不确定画面')
        result.append(dict(s));last=b
    omitted={}
    for o in omissions:
        if not isinstance(o,dict) or o.get('frame_id') not in expected or o['frame_id'] in omitted or not reason(o.get('reason')):raise ContinuousParseError('遗漏解释非法')
        omitted[o['frame_id']]=o['reason']
    for fid,e in seen.items():
        if candidate_bounds[0]<=e['t_ms']<candidate_bounds[1] and e['viewing']=='good' and not any(s['start_ms']<=e['t_ms']<s['end_ms'] for s in result) and fid not in omitted:raise ContinuousParseError('优秀核心或首尾被删除但没有解释')
    if not reason(d.get('reason')):raise ContinuousParseError('缺少整体原因')
    return {'segment_usable':d['segment_usable'],'segments':result,'evidence':list(seen.values()),'omissions':omissions,'reason':d['reason'],
            'observation_conflicts':[{'frame_id':fid,'t_ms':expected[fid],'reason':why} for fid,why in conflicts.items()],
            'boundary_precision':'近似（有序采样与外侧上下文，非逐帧精确）'}

"""Read-only planning hints: no model calls, budget writes or task changes."""
import json, math, statistics
from datetime import datetime, timezone
from .. import security
from ..config import settings
from . import assets, model_config


def _time(value):
    parsed=datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def media_seconds(conn, aid):
    asset=assets.get_asset(conn,aid)
    if not asset:raise security.SecurityError('asset_not_found','素材不存在，请重新添加')
    path=security.resolve_within_allowed(asset['source_path'],settings.allowed_asset_dirs)
    if not path.is_file():raise security.SecurityError('asset_not_found','原文件不可用，请重新上传')
    if asset['media_type']=='image':return 12.,0.
    import av
    with av.open(str(path)) as src:
        stream=src.streams.video[0]
        seconds=float(src.duration/av.time_base) if src.duration else float(stream.duration*stream.time_base)
    return max(1.,seconds),seconds


def estimate(conn,ids,ref=None,budget=None,pipeline_version=2):
    if not isinstance(ids,list) or not ids or len(ids)>200 or any(not isinstance(a,str) for a in ids):
        raise security.SecurityError('invalid_assets','请先选择本次处理的素材')
    ids=list(dict.fromkeys(ids))
    if budget is not None and (type(budget) is not int or budget<=0):raise security.SecurityError('invalid_budget','预算须为正数')
    if ref is not None and (not isinstance(ref,dict) or set(ref)-{'model_config_id','version'}):raise security.SecurityError('invalid_config_ref','配置引用无效')
    cfg=model_config.get_config(conn,**(ref or {}));ref={'model_config_id':cfg['model_config_id'],'version':cfg['version']}
    measures=[media_seconds(conn,a) for a in ids];units=sum(x[0] for x in measures);samples=[]
    rows=conn.execute("SELECT t.task_id,t.model_config_ref_json,t.snapshot_ref_json FROM tasks t JOIN analysis_jobs j ON j.task_id=t.task_id WHERE j.status='completed' AND j.mode='real' AND t.outcome IN ('complete','partial','none_qualified') ORDER BY j.updated_at DESC LIMIT 30").fetchall()
    for row in rows:
        if json.loads(row['model_config_ref_json'] or '{}')!=ref:continue
        snap=json.loads(row['snapshot_ref_json'] or '{}')
        if snap.get('selection_pipeline_version',2 if snap.get('quality_policy_version',1)>=2 else 1)!=pipeline_version:continue
        calls=conn.execute("SELECT r.created_at,r.updated_at,c.amount_minor,c.status,c.call_source,r.status request_status FROM model_requests r JOIN cost_records c ON c.cost_id=r.cost_id WHERE r.task_id=?",(row['task_id'],)).fetchall()
        active=[r for r in calls if r['request_status']!='reconciled']
        if not active or any(r['request_status']!='responded' or r['status']!='settled' or r['call_source']!='real' for r in active):continue
        try:
            oldids=[r[0] for r in conn.execute('SELECT asset_id FROM task_assets WHERE task_id=?',(row['task_id'],))]
            oldmeasures=[media_seconds(conn,a) for a in oldids]
            if {v[1]>0 for v in oldmeasures}!={v[1]>0 for v in measures}:continue
            oldunits=sum(v[0] for v in oldmeasures)
            elapsed=max(1,(max(_time(r['updated_at']) for r in active)-min(_time(r['created_at']) for r in active)).total_seconds())
            samples.append((sum(r['amount_minor'] for r in active)/oldunits,elapsed/oldunits,oldunits))
        except (OSError,ValueError,TypeError,security.SecurityError):continue
    samples=sorted(samples,key=lambda s:abs(math.log(max(.01,units/s[2]))))[:5]
    low=high=sl=sh=None
    if samples:
        factor=1.6 if len(samples)>=3 else 2.
        low=max(1,round(units*statistics.median(s[0] for s in samples)/factor));high=max(low,math.ceil(units*statistics.median(s[0] for s in samples)*factor))
        sl=max(15,round(units*statistics.median(s[1] for s in samples)/factor));sh=max(sl,math.ceil(units*statistics.median(s[1] for s in samples)*factor+30))
        basis=f'参考同一配置的{len(samples)}个真实完成任务；样本有限，画面复杂度与服务速度会改变结果。'
    else:basis='当前配置缺少可用的真实完成记录，暂不能可靠预估。建议先处理短素材，不要反复追加预算试完整长片。'
    status='unknown' if low is None or budget is None else 'insufficient' if budget<low else 'at_risk' if budget<high else 'likely_sufficient'
    return dict(asset_count=len(ids),duration_seconds=round(sum(x[1] for x in measures),2),model_config_ref=ref,cost_low_minor=low,cost_high_minor=high,seconds_low=sl,seconds_high=sh,budget_status=status,sample_count=len(samples),basis=basis,paid_calls=0,notice='按当前配置的历史用量计价记录推算，非报价或完成保证。预估不启动处理，也不占用预算。')


def stage_progress(conn,tid,job,refs):
    name=(job or {}).get('stage') or ''
    steps=[('读取素材','读取本次素材，准备处理。'),('初筛画面','浏览整条素材，寻找值得细看的区域。'),('细查好瞬间','仔细查看候选画面，寻找照片和连续片段。'),('检查与补查','检查照片质量、精彩瞬间和视频起止。'),('整理推荐','整理结果，准备进入人工挑选。')]
    index=3 if any(s in name for s in ('加密','补查','质检','峰值','视频段')) else 2 if '细查' in name else 1 if '粗筛' in name else 0
    current=(job or {}).get('current_asset_id');asset=next((a for a in refs if a['asset_id']==current),None);count=None
    if asset:
        plan=asset.get('coverage_plan') or {}
        if index==1:
            coarse=plan.get('coarse') or {};total=coarse.get('planned_batches')
            if total:count=dict(done=len(coarse.get('batches') or []),total=total,unit='批初筛')
        elif index==2:
            planned=[p for p in plan.get('planned',[]) if p.get('purpose')!='photo_peak'];done=[p for p in plan.get('checked',[]) if p.get('purpose')!='photo_peak']
            if planned:count=dict(done=len(done),total=len(planned),unit='个细查窗口')
    first=conn.execute("SELECT MIN(created_at) FROM model_requests WHERE task_id=? AND status!='reconciled'",(tid,)).fetchone()[0]
    try:elapsed=max(0,round((datetime.now(timezone.utc)-_time(first)).total_seconds())) if first else 0
    except (ValueError,TypeError):elapsed=0
    return dict(steps=[s[0] for s in steps],index=index,description=steps[index][1],detail=name,current_asset=asset['name'] if asset else None,count=count,elapsed_seconds=elapsed,note='可以离开页面，稍后从任务记录回来。本地处理需保持电脑唤醒和服务运行。')

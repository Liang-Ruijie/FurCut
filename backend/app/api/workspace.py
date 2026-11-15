"""Product-facing views over canonical task/review/export services; no duplicate selection logic."""
from __future__ import annotations
import io,json,sqlite3,tempfile,zipfile
from datetime import datetime,timezone
from pathlib import Path
from fastapi import APIRouter,Depends,Query
from fastapi.responses import FileResponse,Response
from starlette.background import BackgroundTask
from .. import security,schemas
from ..config import settings
from ..services import tasks,results,assets,preferences,model_config
from .routes import get_conn

router=APIRouter(prefix='/api/v1/workspace')

@router.post('/estimate')
def estimate_usage(req:dict,conn:sqlite3.Connection=Depends(get_conn)):
    from ..services.usage_preview import estimate
    return estimate(conn,req.get('asset_ids'),req.get('model_config_ref'),req.get('budget_minor'))

@router.get('/tasks/{task_id}/estimate')
def estimate_task(task_id:str,conn:sqlite3.Connection=Depends(get_conn)):
    from ..services.usage_preview import estimate
    t=tasks.get_task(conn,task_id)
    from ..services.quality_policy import selection_version
    result=estimate(conn,[a['asset_id'] for a in t['asset_refs']],json.loads(t['model_config_ref_json'] or 'null'),t['budget_limit_minor'],selection_version(conn,task_id))
    result['already_spent_minor']=t['budget_settled_minor']
    return result
DEFAULT_QUALITY={'required':['基本可见','无严重模糊/遮挡','脸部完整']}
DEFAULT_VISUAL={'priority':['眼神传神','脸型表情好看','姿态自然','构图协调'],'bonus':['嘴套饱满','毛蓬松'],'action_bonus':['自然互动','有实质差异的好瞬间']}
DEFAULT_EXPORT={'best_frame_enabled':True,'best_frame_hold_ms':300,'prefer_continuous':True}

def _title(task,refs):
    return tasks.display_title({**task,'asset_refs':refs})

def _analysis_sources(conn,task_id):
    # Historical provenance, deliberately not a claim that current results passed.
    return [r[0] for r in conn.execute("SELECT DISTINCT source FROM analysis_runs WHERE task_id=? AND status='complete' ORDER BY source",(task_id,))]

@router.get('/setup')
def setup(conn:sqlite3.Connection=Depends(get_conn)):
    try: pref=preferences.get_preference(conn,include_name=True)
    except security.SecurityError: pref=None
    try: cfg=model_config.get_config(conn)
    except security.SecurityError: cfg=None
    endpoint=(cfg or {}).get('vlm',{})
    ready=bool(endpoint.get('key_configured') and endpoint.get('model') and endpoint.get('base_url'))
    from ..services.cost import workspace_budget
    return {'workspace_budget':workspace_budget(conn),'real_ready':ready,'preference':pref,'preference_versions':preferences.list_versions(conn),'preference_labels':preferences.list_labels(conn),'default_preference':{'quality_floor':DEFAULT_QUALITY,'visual_rules':DEFAULT_VISUAL,'export_defaults':DEFAULT_EXPORT},'model_config_ref':{'model_config_id':cfg['model_config_id'],'version':cfg['version']} if cfg else None,'model_name':endpoint.get('model') or None,'message':'已配置真实分析' if ready else '真实分析尚未配置，请管理员在设置中检查模型和服务端密钥引用。模拟仅用于体验流程。'}

@router.get('/tasks')
def history(q:str='',limit:int=Query(12,ge=1,le=60),offset:int=Query(0,ge=0),conn:sqlite3.Connection=Depends(get_conn)):
    pattern='%'+q.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
    where="WHERE task_id LIKE ? ESCAPE '\\' OR requirement LIKE ? ESCAPE '\\' OR business_ref_json LIKE ? ESCAPE '\\' OR task_id IN (SELECT ta.task_id FROM task_assets ta JOIN assets a ON a.asset_id=ta.asset_id WHERE a.source_path LIKE ? ESCAPE '\\')" if q else ''
    args=[pattern]*4 if q else []
    total=conn.execute('SELECT COUNT(*) FROM tasks '+where,args).fetchone()[0]
    rows=conn.execute('SELECT * FROM tasks '+where+' ORDER BY created_at DESC LIMIT ? OFFSET ?',args+[limit,offset]).fetchall()
    items=[]
    for row in rows:
        t=tasks.get_task(conn,row['task_id']);refs=t['asset_refs']
        cover=json.loads(t.get('business_ref_json') or '{}').get('cover')
        if not isinstance(cover,dict) or cover.get('asset_id') not in {a['asset_id'] for a in refs} or type(cover.get('t_ms')) is not int or cover['t_ms']<0:cover=None
        items.append({'cover':cover,'task_id':t['task_id'],'title':_title(t,refs),'status':t['status'],'outcome':t['outcome'],'created_at':t['created_at'],'updated_at':t['updated_at'],'analysis_sources':_analysis_sources(conn,t['task_id']),'asset_count':len(refs),'assets':[{'asset_id':a['asset_id'],'name':Path(a['source_path']).name,'media_type':a['media_type']} for a in refs[:4]]})
    return {'tasks':items,'total':total,'limit':limit,'offset':offset}

@router.patch('/tasks/{task_id}/title')
def rename(task_id:str,req:dict,conn:sqlite3.Connection=Depends(get_conn)):
    title=req.get('title')
    if not isinstance(title,str) or not 1<=len(title.strip())<=100:raise security.SecurityError('invalid_title','任务名称须为1–100字')
    task=tasks.get_task(conn,task_id);b=json.loads(task.get('business_ref_json') or '{}');b['title']=title.strip()
    conn.execute('UPDATE tasks SET business_ref_json=?,updated_at=? WHERE task_id=?',(json.dumps(b,ensure_ascii=False),datetime.now(timezone.utc).isoformat(),task_id));conn.commit()
    return {'title':b['title']}

@router.get('/tasks/{task_id}/progress')
def progress(task_id:str,conn:sqlite3.Connection=Depends(get_conn)):
    from ..services import analysis_job
    t=tasks.get_task(conn,task_id);j=analysis_job.get_task_analysis(conn,task_id)
    from ..services import request_journal
    source_hold=None
    try:request_journal.assert_sources_clear(conn,task_id)
    except security.SecurityError as exc:
        if exc.code!='source_pending_reconciliation':raise
        source_hold=exc.message
    quality_assets={e['asset_id'] for e in results.get_task_results(conn,task_id)['quality_errors']} if t['outcome']=='incomplete_quality' else set()
    refs=[]
    for a in t['asset_refs']:
        e=conn.execute('SELECT status,coverage_plan_json FROM executions WHERE task_id=? AND asset_id=? ORDER BY created_at DESC LIMIT 1',(task_id,a['asset_id'])).fetchone()
        refs.append({'asset_id':a['asset_id'],'name':Path(a['source_path']).name,'media_type':a['media_type'],'status':e['status'] if e else 'unprocessed','coverage_plan':json.loads(e['coverage_plan_json']) if e and e['coverage_plan_json'] else None})
        if a['asset_id'] in quality_assets:refs[-1]['status']='quality_incomplete'
        elif e and e['status'] in ('processing','running') and (not j or j['status']!='running'):refs[-1]['status']='paused'
    requests=conn.execute("SELECT status,COUNT(*) n FROM model_requests WHERE task_id=? GROUP BY status",(task_id,)).fetchall()
    ref=json.loads(t.get('model_config_ref_json') or 'null');ready=False
    if ref:
        cfg=model_config.get_config(conn,ref['model_config_id'],ref['version']);ready=bool(cfg['vlm'].get('key_configured'))
    preference_ref=json.loads(t.get('preference_ref_json') or 'null')
    task_options={'requirement':t.get('requirement') or '', 'available':False}
    if preference_ref:
        try:
            pref=preferences.get_preference(conn,preference_ref['preference_id'],preference_ref['version'])
            task_options.update(available=True,priority=pref['visual_rules'].get('priority',[]),best_frame_enabled=pref['export_defaults'].get('best_frame_enabled',False),best_frame_hold_ms=pref['export_defaults'].get('best_frame_hold_ms',300))
        except security.SecurityError as exc:
            if exc.code!='preference_not_found':raise
    task_options['asset_names']=[a['name'] for a in refs]
    from ..services.usage_preview import stage_progress
    processing=stage_progress(conn,task_id,j,refs)
    return {'processing':processing,'task_options':task_options,'task_id':task_id,'title':_title(t,t['asset_refs']),'status':t['status'],'outcome':t['outcome'],'real_ready':ready,'source_hold':source_hold,'experimental_quality':json.loads(t['snapshot_ref_json'] or '{}').get('quality_policy_version',1)>=2,'error':json.loads(t['error_json']) if t.get('error_json') else None,'job':j,'assets':refs,'completed_assets':sum(a['status']=='complete' for a in refs),'total_assets':len(refs),'budget':{'limit':t['budget_limit_minor'],'settled':t['budget_settled_minor'],'reserved':t['budget_reserved_minor'],'unknown':t['budget_unknown_minor']},'requests':{r['status']:r['n'] for r in requests},'created_at':t['created_at'],'updated_at':t['updated_at']}

@router.get('/assets')
def library(conn:sqlite3.Connection=Depends(get_conn)):
    rows=conn.execute('SELECT * FROM assets ORDER BY created_at DESC').fetchall()
    return {'assets':[{'asset_id':r['asset_id'],'name':Path(r['source_path']).name,'media_type':r['media_type'],'size_bytes':r['size_bytes'],'available':Path(r['source_path']).is_file()} for r in rows]}

@router.get('/assets/{asset_id}/image')
def image(asset_id:str,t_ms:int=Query(0,ge=0),task_id:str|None=None,source_pts:int|None=None,time_base:str|None=None,full:bool=False,conn:sqlite3.Connection=Depends(get_conn)):
    from ..media.sample import extract_frame_at
    src=Path(assets.check_export_asset(conn,task_id,asset_id));im,_=extract_frame_at(src,t_ms,resize_edge=0 if full else 600,source_pts=source_pts,time_base=time_base)
    if im is None:raise security.SecurityError('frame_not_found','源帧暂时不可用，请检查素材后重试')
    buffer=io.BytesIO();im.convert('RGB').save(buffer,format='JPEG',quality=94 if full else 80)
    return Response(buffer.getvalue(),media_type='image/jpeg',headers={'Cache-Control':'private, max-age=120'})

@router.get('/tasks/{task_id}/assets/{asset_id}/original')
def original(task_id:str,asset_id:str,conn:sqlite3.Connection=Depends(get_conn)):
    path=Path(assets.check_export_asset(conn,task_id,asset_id))
    return FileResponse(path,filename=path.name,content_disposition_type='inline')

@router.post('/default-preference')
def default_preference(conn:sqlite3.Connection=Depends(get_conn)):
    # Explicit first-use action, never seeds/writes on GET or overwrites existing defaults.
    try:return preferences.get_preference(conn,include_name=True)
    except security.SecurityError:
        r=preferences.save_preference(conn,None,DEFAULT_QUALITY,DEFAULT_VISUAL,DEFAULT_EXPORT)
        return preferences.get_preference(conn,r['preference_id'],r['version'],include_name=True)

@router.post('/experience-config')
def experience_config(conn:sqlite3.Connection=Depends(get_conn)):
    try:cfg=model_config.get_config(conn)
    except security.SecurityError:
        ep={'provider':'deepseek','model':'offline','base_url':'http://127.0.0.1:1','api_key_ref':'AI_VIDEO_UNCONFIGURED'}
        r=model_config.save_config(conn,None,ep,ep,schemas.SamplingConfig().model_dump())
        cfg=model_config.get_config(conn,r['model_config_id'],r['version'])
    return {'model_config_id':cfg['model_config_id'],'version':cfg['version']}

@router.get('/tasks/{task_id}/deliveries')
def deliveries(task_id:str,conn:sqlite3.Connection=Depends(get_conn)):
    tasks.get_task(conn,task_id);root=settings.output_dir/'packages';rows=[]
    for directory in sorted(root.glob(task_id+'-*'),key=lambda p:p.stat().st_mtime,reverse=True):
        try:
            doc=json.loads((directory/'manifest.json').read_text())
            if doc['task_id']!=task_id:continue
            from ..services.delivery import verify_package
            verify_package(directory,decode=False)
            rows.append({'name':directory.name,'status':'verified','count':len(doc['items']),'build':doc['build']['version'],'url':f'/api/v1/packages/{directory.name}/index.html','download_url':f'/api/v1/workspace/tasks/{task_id}/deliveries/{directory.name}/download','created_at':datetime.fromtimestamp(directory.stat().st_mtime,timezone.utc).isoformat()})
        except (OSError,ValueError,KeyError,security.SecurityError):
            rows.append({'name':directory.name,'status':'damaged','message':'历史包校验失败；原包保留，请重新生成修复版本'})
    return {'deliveries':rows}

@router.get('/tasks/{task_id}/deliveries/{name}/download')
def download_package(task_id:str,name:str,conn:sqlite3.Connection=Depends(get_conn)):
    tasks.get_task(conn,task_id)
    if Path(name).name!=name or not name.startswith(task_id+'-'):raise security.SecurityError('invalid_package','交付包不属于本任务')
    directory=settings.output_dir/'packages'/name
    from ..services.delivery import verify_package
    verify_package(directory,decode=False)
    if json.loads((directory/'manifest.json').read_text())['task_id']!=task_id:raise security.SecurityError('invalid_package','交付包归属不符')
    staging=settings.output_dir/'.downloads';staging.mkdir(exist_ok=True,parents=True)
    handle=tempfile.NamedTemporaryFile(dir=staging,suffix='.zip',delete=False);path=Path(handle.name);handle.close()
    try:
        with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_STORED) as z:
            for p in directory.iterdir():
                if not p.is_file() or p.is_symlink():raise security.SecurityError('invalid_package','包内包含非法文件')
                z.write(p,p.name)
        return FileResponse(path,filename='精选成果-'+name[-16:]+'.zip',background=BackgroundTask(path.unlink,missing_ok=True))
    except Exception:
        path.unlink(missing_ok=True);raise

@router.get('/admin/requests')
def unresolved_requests(conn:sqlite3.Connection=Depends(get_conn)):
    rows=conn.execute("""SELECT r.request_id,r.task_id,r.cost_id,r.status,r.input_digest,r.error,r.created_at,
        c.amount_minor,c.status AS cost_status,c.provider,c.model,c.usage_json
        FROM model_requests r JOIN cost_records c ON c.cost_id=r.cost_id
        WHERE r.status IN ('inflight','pending_reconciliation')
        OR (r.status='responded' AND c.status IN ('unknown','reserved')) ORDER BY r.created_at""").fetchall()
    items=[]
    for row in rows:
        item={k:row[k] for k in row.keys() if k!='usage_json'}
        usage=json.loads(row['usage_json'] or '{}');item['supplier_request_id']=usage.get('supplier_request_id') or usage.get('request_id');items.append(item)
    return {'requests':items}

@router.get('/tasks/{task_id}/export-history')
def export_history(task_id:str,conn:sqlite3.Connection=Depends(get_conn)):
    tasks.get_task(conn,task_id)
    from ..services import export_job
    ids=conn.execute('SELECT export_job_id FROM export_jobs WHERE task_id=? ORDER BY created_at DESC',(task_id,)).fetchall()
    return {'jobs':[export_job.get_job(conn,row[0]) for row in ids]}


@router.get('/tasks/{task_id}/settings')
def task_settings(task_id:str,conn:sqlite3.Connection=Depends(get_conn)):
    t=tasks.get_task(conn,task_id)
    ref=json.loads(t.get('preference_ref_json') or 'null')
    try:pref=preferences.get_preference(conn,ref['preference_id'],ref['version'],include_name=True) if ref else None
    except security.SecurityError:pref=None
    return {'task_id':task_id,'can_edit':tasks.can_edit_settings(conn,t),'revision':tasks.settings_revision(t),'lock_reason':_settings_lock_reason(conn,t),'requirement':t.get('requirement') or '', 'preference':pref,'assets':[{'asset_id':a['asset_id'],'name':Path(a['source_path']).name,'size_bytes':conn.execute('SELECT size_bytes FROM assets WHERE asset_id=?',(a['asset_id'],)).fetchone()[0],'media_type':a['media_type']} for a in t['asset_refs']]}


@router.patch('/tasks/{task_id}/settings')
def save_task_settings(task_id:str,req:schemas.TaskSettingsRequest,conn:sqlite3.Connection=Depends(get_conn)):
    tasks.update_settings(conn,task_id,req.preference_ref,req.requirement,req.best_frame_enabled,req.best_frame_hold_ms,req.asset_ids,req.expected_revision)
    return task_settings(task_id,conn)


def _settings_lock_reason(conn,t):
    if tasks.can_edit_settings(conn,t):return ''
    if t['status']=='pending_reconciliation' or t.get('budget_unknown_minor'):return '有请求或费用待核对，素材和挑选标准保持锁定；请先核账。'
    if t['status']=='cancelled':return '任务已取消，原素材、标准和成果保留；如需调整，请按此设置新建。'
    if t['status']=='failed':return '任务已中断，继续处理仍使用原素材和标准；如需调整，请按此设置新建。'
    if t['status'] in ('ready_for_review','completed'):return '已有分析结果，素材和挑选标准已锁定；仍可审核、修边和下载。'
    return '任务已开始过处理，素材和挑选标准已锁定；如需调整，请按此设置新建。'

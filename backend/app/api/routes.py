"""FastAPI 路由（/api/v1）。统一错误结构由 app 级 SecurityError 处理器返回 {"error":{"code","message"}}。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import FileResponse

from .. import db, schemas, security
from ..config import PROJECT_ROOT, settings
from ..services import assets, cost, feedback, model_config, preferences, results, tasks

router = APIRouter(prefix="/api/v1")


def get_conn():
    conn = db.get_conn(settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def _task_view(t: dict) -> schemas.TaskView:
    b = {
        "currency": t.get("budget_currency", "CNY"),
        "limit": t.get("budget_limit_minor"),
        "settled": t.get("budget_settled_minor", 0),
        "reserved_inflight": t.get("budget_reserved_minor", 0),
        "unknown": t.get("budget_unknown_minor", 0),
        "status": t.get("budget_status"),
    }
    return schemas.TaskView(
        task_id=t["task_id"],
        status=t["status"],
        outcome=t["outcome"],
        error=json.loads(t["error_json"]) if t.get("error_json") else None,
        resume_point=t.get("resume_point"),
        asset_refs=t.get("asset_refs", []),
        preference_ref=json.loads(t["preference_ref_json"]) if t.get("preference_ref_json") else None,
        model_config_ref=json.loads(t["model_config_ref_json"]) if t.get("model_config_ref_json") else None,
        snapshot_ref=json.loads(t["snapshot_ref_json"]) if t.get("snapshot_ref_json") else None,
        budget=schemas.BudgetView(**b),
        business_ref=json.loads(t["business_ref_json"]) if t.get("business_ref_json") else None,
        created_at=t["created_at"],
        updated_at=t["updated_at"],
    )


@router.get("/tasks/{task_id}/results")
def task_results(task_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    return results.get_task_results(conn, task_id)


@router.get("/tasks/{task_id}/feedback")
def task_feedback(task_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    return {"feedback": feedback.list_feedback(conn, task_id)}


@router.post("/tasks/{task_id}/feedback")
def save_feedback(task_id: str, req: dict, conn: sqlite3.Connection = Depends(get_conn)):
    tasks.get_task(conn, task_id)
    asset_id = req.get("asset_id")
    if asset_id:
        _check_asset_in_task(conn, task_id, asset_id)
    r = feedback.record_feedback(conn, task_id, asset_id, req.get("run_id"),
                                 req.get("kind", "note"), req.get("payload", {}))
    return r


def _single_reviewed_item(conn,req,kind):
    item = next((i for i in results.reviewed_items(conn,req.task_id) if i["kind"]==kind and i["candidate_id"]==req.candidate_id),None)
    keys = ("asset_id","t_ms") if kind=="photo" else ("asset_id","start_ms","end_ms")
    if item is None or any(getattr(req,k) != item.get(k) for k in keys):
        raise security.SecurityError("stale_review","正式单项导出须使用当前已保留的候选与已保存边界")
    if kind == "photo":
        from ..media.sample import extract_frame_at
        path=Path(assets.check_export_asset(conn,req.task_id,req.asset_id))
        image,_=extract_frame_at(path,item["t_ms"])
        if image is None:raise security.SecurityError("frame_not_found","候选源帧不存在")
        for key in ("source_pts","time_base"):
            actual=image.info.get(key)
            provided=getattr(req,key)
            if (provided is not None and provided!=actual) or (item.get(key) is not None and item[key]!=actual):
                raise security.SecurityError("source_identity_mismatch","候选源帧身份不一致")
            item[key]=actual
    return item


@router.post("/preview/video")
def preview_video(req: schemas.ExportVideoRequest, conn: sqlite3.Connection = Depends(get_conn)):
    _check_asset_in_task(conn,req.task_id,req.asset_id)
    view = results.get_task_results(conn,req.task_id)
    candidate = next((s for s in view["video_candidates"]+view["video_fragments"] if s["candidate_id"]==req.candidate_id and s["asset_id"]==req.asset_id),None)
    if candidate is None:
        raise security.SecurityError("invalid_candidate","预览候选不存在或已变更批次")
    from ..build import BUILD
    from ..services.batch_identity import digest
    previews=settings.output_dir/'previews';previews.mkdir(parents=True,exist_ok=True)
    key=digest({'build':BUILD['source_sha256'],'asset':req.asset_id,'start':req.start_ms,'end':req.end_ms})
    receipt=previews/('.preview-'+key+'.json')
    if receipt.exists():
        try:
            cached=json.loads(receipt.read_text());name=cached['file']
            if Path(name).name!=name:raise ValueError('invalid cache filename')
            path=previews/name
            results.validate_media(path,'video')
            if security.sha256_file(path)!=cached['sha256']:raise ValueError('preview changed')
            return {'file':name,'url':'/api/v1/preview/file/'+name,'purpose':'preview','cached':True}
        except (OSError,ValueError,KeyError,security.SecurityError):
            pass  # preserve a corrupt preview; generate a new file and atomically replace only the cache pointer
    out=results.export_video_clip(conn,req.asset_id,req.start_ms,req.end_ms,previews)
    temp=previews/('.preview-'+key+'-'+assets.new_id('write')+'.tmp')
    temp.write_text(json.dumps({'file':out.name,'sha256':security.sha256_file(out)}));temp.replace(receipt)
    return {"file":out.name,"url":"/api/v1/preview/file/"+out.name,"purpose":"preview"}


@router.get("/preview/file/{name}")
def preview_file(name: str):
    if Path(name).name != name:
        raise security.SecurityError("invalid_path","非法预览路径")
    path=settings.output_dir/"previews"/name
    if not path.is_file():raise security.SecurityError("preview_not_found","预览不存在")
    return FileResponse(path)


@router.post("/export/video")
def export_video(req: schemas.ExportVideoRequest, conn: sqlite3.Connection = Depends(get_conn)):
    _check_asset_in_task(conn, req.task_id, req.asset_id)
    item = _single_reviewed_item(conn,req,"video")
    if req.start_ms >= req.end_ms:
        raise security.SecurityError("invalid_boundary", f"起止时间非法：{req.start_ms}-{req.end_ms}")
    from ..services.video_variants import export_reviewed_video
    out,variants = export_reviewed_video(conn,req.task_id,item,settings.output_dir)
    from ..services import artifacts
    for path in [out]+[settings.output_dir/v['file'] for v in variants]:artifacts.record(conn,req.task_id,path,[item])
    return {"file":out.name,"path":str(out),"variant_files":variants,**results.video_identity(out)}


@router.post("/export/photo")
def export_photo(req: schemas.ExportPhotoRequest, conn: sqlite3.Connection = Depends(get_conn)):
    _check_asset_in_task(conn, req.task_id, req.asset_id)
    item = _single_reviewed_item(conn,req,"photo")
    out = results.export_photo(conn, req.asset_id, req.t_ms, settings.output_dir,source_pts=item.get("source_pts"),time_base=item.get("time_base"))
    from ..services import artifacts
    artifacts.record(conn,req.task_id,out,[item])
    return {"file": out.name, "path": str(out), **results.photo_identity(out)}


@router.post("/export/manifest")
def export_manifest(req: schemas.ExportManifestRequest, conn: sqlite3.Connection = Depends(get_conn)):
    """按审核结果导出来源清单（JSON）。"""
    from datetime import datetime
    items = results.validate_reviewed_items(conn,req.task_id,req.items)
    for it in items:
        _check_asset_in_task(conn, req.task_id, it["asset_id"])
    out_dir = settings.output_dir
    out = results._unique_path(out_dir, f"来源清单-{req.task_id[:12]}.json")
    from ..services.video_variants import export_options
    doc = {"task_id": req.task_id, "created_at": datetime.now().isoformat(), "export_options":export_options(conn,req.task_id), "items": items}
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    from ..services import artifacts
    artifacts.record(conn,req.task_id,out,items)
    return {"file": out.name, "path": str(out)}


def _check_asset_in_task(conn: sqlite3.Connection, task_id: str | None, asset_id: str):
    """导出/预览前统一校验：存在、归属、源文件存在且指纹与锁定一致。"""
    assets.check_export_asset(conn, task_id, asset_id)


@router.get("/export/file/{name}")
def download_export(name: str, conn: sqlite3.Connection = Depends(get_conn)):
    from ..services import artifacts
    p=artifacts.verify(conn,name)
    return FileResponse(str(p), filename=name)


@router.post("/export/batch")
def start_batch_export(req: schemas.BatchExportRequest, conn: sqlite3.Connection = Depends(get_conn)):
    """批量导出保留项：创建/恢复任务，后台执行，支持中断恢复（已完成不重复）。"""
    from ..services import export_job
    task_id = req.task_id
    items = results.validate_reviewed_items(conn,req.task_id,req.items)
    for it in items:
        _check_asset_in_task(conn, task_id, it["asset_id"])
    if not items:
        raise security.SecurityError("no_items", "没有可导出的保留项")
    jid = export_job.start_batch(conn, task_id, items)
    return {"export_job_id": jid}


@router.post("/tasks/{task_id}/package")
def create_delivery(task_id: str, req: dict, conn: sqlite3.Connection = Depends(get_conn)):
    from ..services.delivery import create_package
    from ..build import BUILD
    package = create_package(conn,task_id,settings.output_dir / "packages",BUILD["version"])
    return {"package":package.name,"path":str(package),"url":f"/api/v1/packages/{package.name}/index.html"}


@router.get("/packages/{package}/{name}")
def package_file(package: str, name: str):
    from ..services.delivery import verify_package
    if Path(package).name != package or Path(name).name != name:
        raise security.SecurityError("invalid_path","非法交付路径")
    directory=settings.output_dir / "packages" / package
    verify_package(directory,decode=False)
    return FileResponse(directory / name)


@router.get("/export/jobs/{job_id}")
def get_export_job(job_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    from ..services import export_job
    return export_job.get_job(conn, job_id)


@router.get("/export/jobs/task/{task_id}")
def get_task_export_job(task_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    from ..services import export_job
    j = export_job.get_task_job(conn, task_id)
    return {"job": j}


@router.get("/tasks")
def list_tasks(limit: int = 20, offset: int = 0, conn: sqlite3.Connection = Depends(get_conn)):
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    rows = conn.execute("SELECT task_id, status, outcome, created_at FROM tasks ORDER BY created_at DESC LIMIT ? OFFSET ?",
                        (limit, offset)).fetchall()
    total = conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"]
    return {"tasks": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/assets", response_model=schemas.AssetRegisterResponse)
def list_assets(conn: sqlite3.Connection = Depends(get_conn)):
    rows = conn.execute("SELECT * FROM assets ORDER BY created_at").fetchall()
    refs = [{"asset_id": r["asset_id"], "fingerprint": r["fingerprint"], "media_type": r["media_type"],
             "size_bytes": r["size_bytes"], "source_path": r["source_path"]} for r in rows]
    return schemas.AssetRegisterResponse(asset_refs=[schemas.AssetRef(**r) for r in refs])


@router.get("/assets/files")
def list_asset_files():
    """列出允许目录内的媒体文件，供前端选择导入（不读取目录之外）。"""
    files = []
    for d in settings.allowed_asset_dirs:
        p = Path(d)
        if not p.exists():
            continue
        for f in sorted(p.iterdir()):
            if f.is_file() and f.suffix.lower() in assets._ALLOWED_SUFFIXES:
                files.append({"name": f.name, "path": str(f), "size_bytes": f.stat().st_size})
    return {"files": files}


@router.post("/assets/upload")
async def upload_asset(file: UploadFile = File(...), conn: sqlite3.Connection = Depends(get_conn)):
    """浏览器上传素材：格式/真实类型/大小/安全文件名校验，存到允许目录并登记。"""
    name = assets.safe_filename(file.filename or "")
    suffix = Path(name).suffix.lower()
    if suffix not in assets._ALLOWED_SUFFIXES:
        raise security.SecurityError("asset_unsupported", f"不支持的文件类型：{suffix or '无扩展名'}")
    dest_dir = settings.allowed_asset_dirs[0]
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    i = 1
    while dest.exists():  # 防覆盖：同名加序号
        stem, suf = Path(name).stem, Path(name).suffix
        dest = dest_dir / f"{stem}_{i}{suf}"
        i += 1
    size = 0
    created = False
    try:
        with open(dest, "xb") as out:
            created = True
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > assets.MAX_UPLOAD_BYTES:
                    raise security.SecurityError("asset_too_large", "文件超过 2GB 限制")
                out.write(chunk)
        mtype = "video" if suffix in {".mov", ".mp4", ".avi", ".mkv", ".m4v", ".webm"} else "image"
        assets.validate_real_type(dest, mtype)
        refs = assets.register_assets(conn, [str(dest)],allow_source_repair=True)
        if Path(refs[0]['source_path']) != dest:
            # The existing canonical source is intact. This request created the duplicate,
            # so removing only this new copy cannot affect an earlier original.
            dest.unlink()
        return refs[0]
    except Exception:
        if created:
            dest.unlink(missing_ok=True)
        raise


@router.get("/assets/{asset_id}/meta")
def asset_meta(asset_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    from ..media.meta import read_video_metadata
    row = conn.execute("SELECT * FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
    if row is None:
        raise security.SecurityError("asset_not_found", f"素材不存在：{asset_id}")
    assets.check_export_asset(conn,None,asset_id)
    if row["media_type"] == "image":
        from ..media.sample import extract_frame_at
        img, _ = extract_frame_at(Path(row["source_path"]),0)
        m = {"width":img.width,"height":img.height,"duration_ms":0,"rotation":0,"has_audio":False}
    else:
        m = read_video_metadata(Path(row["source_path"]))
    return {"asset_id": asset_id, "source": Path(row["source_path"]).name, "media_type": row["media_type"], **m}


@router.get("/assets/{asset_id}/thumbnail")
def asset_thumbnail(asset_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    return asset_frame(asset_id, 0, conn)


@router.get("/assets/{asset_id}/frame")
def asset_frame(asset_id: str, t_ms: int = 0, conn: sqlite3.Connection = Depends(get_conn), source_pts: int | None = None, time_base: str | None = None):
    from ..media.sample import extract_frame_at, _pil_to_data_url
    src = Path(assets.check_export_asset(conn,None,asset_id))
    img, pts = extract_frame_at(src,t_ms,resize_edge=600,source_pts=source_pts,time_base=time_base)
    if img is None:
        raise security.SecurityError("frame_not_found","无法定位源帧")
    return {"asset_id":asset_id,"t_ms":round(pts),"pts_ms":pts,"source_pts":img.info.get("source_pts"),"time_base":img.info.get("time_base"),
            "data_url":_pil_to_data_url(img,600,80)}


@router.post("/assets/register", response_model=schemas.AssetRegisterResponse)
def register(req: schemas.AssetRegisterRequest, conn: sqlite3.Connection = Depends(get_conn)):
    # Validate every untrusted local import before registering any item in the batch.
    for raw in req.paths:
        path=security.resolve_within_allowed(raw,settings.allowed_asset_dirs)
        if not path.is_file():
            raise security.SecurityError("asset_not_found","素材文件不存在")
        assets.validate_real_type(path,assets._media_type(path))
    refs = assets.register_assets(conn, req.paths)
    return schemas.AssetRegisterResponse(asset_refs=[schemas.AssetRef(**r) for r in refs])


@router.post("/tasks", response_model=schemas.TaskView)
def create_task(req: schemas.TaskCreateRequest, conn: sqlite3.Connection = Depends(get_conn)):
    t = tasks.create_task(conn, req.asset_ids, req.client_request_id, req.preference_ref, req.model_config_ref, req.budget, req.business_ref, req.requirement)
    return _task_view(t)


@router.get("/tasks/{task_id}", response_model=schemas.TaskView)
def get_task(task_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    return _task_view(tasks.get_task(conn, task_id))


@router.post("/tasks/{task_id}/analyze")
def start_analyze(task_id: str, req: dict | None = None, conn: sqlite3.Connection = Depends(get_conn)):
    """显式启动任务分析：默认 mock；真实调用须显式 mode='real'（复用任务锁定配置+预算护栏）。"""
    from ..services import analysis_job
    mode = (req or {}).get("mode", "mock")
    jid = analysis_job.start_analysis(conn, task_id, mode)
    return {"analysis_job_id": jid, "mode": mode}


@router.post("/tasks/{task_id}/requests/{request_id}/reconcile")
def reconcile_request(task_id: str, request_id: str, req: dict, conn: sqlite3.Connection = Depends(get_conn)):
    from ..services import request_journal
    return request_journal.reconcile(conn, task_id, request_id, req.get("evidence"), req.get("disposition"))


@router.get("/tasks/{task_id}/requests")
def task_requests(task_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    tasks.get_task(conn, task_id)
    return {"requests": [dict(r) for r in conn.execute("SELECT request_id,cost_id,status,input_digest,error,reconciliation_json,created_at,updated_at FROM model_requests WHERE task_id=?", (task_id,))]}


@router.get("/tasks/{task_id}/analysis")
def get_analysis(task_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    from ..services import analysis_job
    j = analysis_job.get_task_analysis(conn, task_id)
    return {"job": j}


@router.post("/tasks/{task_id}/reselect")
def reselect_task(task_id: str, req: dict, conn: sqlite3.Connection = Depends(get_conn)):
    old = tasks.get_task(conn,task_id)
    from ..services.request_journal import ensure_legacy_requests, assert_clear
    ensure_legacy_requests(conn,task_id)
    assert_clear(conn,task_id)
    new = tasks.create_task(conn,[a["asset_id"] for a in old["asset_refs"]],
                            client_request_id=req.get("client_request_id"),
                            budget={"currency":old["budget_currency"],"limit":old["budget_limit_minor"]},
                            requirement=old.get("requirement"),business_ref={**json.loads(old.get("business_ref_json") or "{}"),"reselected_from":task_id})
    return _task_view(new)


@router.post("/tasks/{task_id}/cancel", response_model=schemas.TaskView)
def cancel_task(task_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    return _task_view(tasks.cancel_task(conn, task_id))


@router.post("/tasks/{task_id}/reopen-review", response_model=schemas.TaskView)
def reopen_review(task_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    return _task_view(tasks.reopen_review(conn,task_id))


@router.post("/tasks/{task_id}/confirm-review", response_model=schemas.TaskView)
def confirm_review(task_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    return _task_view(tasks.confirm_review(conn, task_id))


@router.post("/tasks/{task_id}/verify-assets")
def verify_assets(task_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    tasks.get_task(conn, task_id)
    mismatches = assets.verify_asset_integrity(conn, task_id)
    return {"task_id": task_id, "mismatches": mismatches, "ok": len(mismatches) == 0}


@router.post("/tasks/{task_id}/budget")
def adjust_budget(task_id: str, req: schemas.BudgetAdjustRequest, conn: sqlite3.Connection = Depends(get_conn)):
    return cost.adjust_budget(conn, task_id, req.limit_minor)


@router.post("/configs", response_model=schemas.ModelConfigView)
def save_config(req: schemas.ModelConfigSaveRequest, conn: sqlite3.Connection = Depends(get_conn)):
    r = model_config.save_config(conn, req.model_config_id, req.llm.model_dump(), req.vlm.model_dump(), req.sampling.model_dump())
    return schemas.ModelConfigView(**model_config.get_config(conn, r["model_config_id"], r["version"]))


@router.get("/configs", response_model=schemas.ModelConfigView)
def get_config(conn: sqlite3.Connection = Depends(get_conn)):
    return schemas.ModelConfigView(**model_config.get_config(conn))


@router.get("/configs/providers")
def provider_configs(conn: sqlite3.Connection = Depends(get_conn)):
    return {"configs": model_config.provider_profiles(conn)}


@router.get("/configs/all")
def list_configs(conn: sqlite3.Connection = Depends(get_conn)):
    return {"configs": model_config.list_all_configs(conn)}


@router.post("/preferences", response_model=schemas.PreferenceView)
def save_preference(req: schemas.PreferenceSaveRequest, conn: sqlite3.Connection = Depends(get_conn)):
    r = preferences.save_preference(conn, req.preference_id, req.quality_floor, req.visual_rules, req.export_defaults, req.display_name)
    return schemas.PreferenceView(**preferences.get_preference(conn, r["preference_id"], r["version"], include_name=True))


@router.patch("/preferences/{version}/name", response_model=schemas.PreferenceView)
def rename_preference(version: int, req: schemas.PreferenceNameRequest, conn: sqlite3.Connection = Depends(get_conn)):
    return schemas.PreferenceView(**preferences.rename_preference(conn, version, req.display_name))


@router.get("/preferences", response_model=schemas.PreferenceView)
def get_preference(version: int | None = None, conn: sqlite3.Connection = Depends(get_conn)):
    return schemas.PreferenceView(**preferences.get_preference(conn, None, version, include_name=True))


@router.post("/costs/reserve", response_model=schemas.CostView)
def reserve_cost(req: schemas.ReserveRequest, conn: sqlite3.Connection = Depends(get_conn)):
    r = cost.reserve_cost(conn, req.task_id, req.amount_minor, req.request_id, req.provider, req.model, req.operation, req.retry_of, req.cache_hit)
    return schemas.CostView(**r)


@router.get("/costs/{cost_id}", response_model=schemas.CostView)
def get_cost(cost_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    row = conn.execute("SELECT * FROM cost_records WHERE cost_id=?", (cost_id,)).fetchone()
    if row is None:
        raise security.SecurityError("cost_not_found", f"费用记录不存在：{cost_id}")
    return schemas.CostView(**dict(row))


@router.post("/costs/{cost_id}/settle", response_model=schemas.CostView)
def settle_cost(cost_id: str, req: schemas.SettleRequest, conn: sqlite3.Connection = Depends(get_conn)):
    r = cost.settle_cost(conn, cost_id, req.actual_amount_minor, req.usage)
    return schemas.CostView(**r)


@router.post("/costs/{cost_id}/unknown", response_model=schemas.CostView)
def unknown_cost(cost_id: str, req: schemas.UnknownRequest, conn: sqlite3.Connection = Depends(get_conn)):
    r = cost.mark_unknown(conn, cost_id, req.note)
    return schemas.CostView(**r)

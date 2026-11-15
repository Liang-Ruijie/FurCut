"""批量导出任务：按导出参数身份幂等、批次快照、重启恢复。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .. import db, security
from ..config import PROJECT_ROOT, settings
from . import results

EXPORT_DIR = settings.output_dir

_running: set[str] = set()
_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _item_key(it: dict) -> str:
    """导出参数身份：kind + 素材 + 有效时点/边界。参数变化即生成新结果。"""
    if it["kind"] == "photo":
        return f"photo:{it['asset_id']}:{it['t_ms']}"
    return f"video:{it['asset_id']}:{it['start_ms']}:{it['end_ms']}"


def _batch_digest(task_id: str, items: list[dict]) -> str:
    keys = sorted(json.dumps({k:it.get(k) for k in ("kind","asset_id","t_ms","start_ms","end_ms","candidate_id","execution_id","analysis_version","source_fingerprint","source_pts","time_base","sources","review_evidence","quality_version")}, sort_keys=True,ensure_ascii=False) for it in items)
    from .video_variants import RENDER_VERSION
    renderer=f'\nvideo-render-v{RENDER_VERSION}' if any(it['kind']=='video' for it in items) else ''
    return hashlib.sha256((task_id + "\n" + "\n".join(keys)+renderer).encode()).hexdigest()[:16]


def _new_job_id(task_id: str, digest: str) -> str:
    return f"exp-{task_id[:16]}-{digest}"


def _new_conn() -> sqlite3.Connection:
    return db.get_conn(settings.db_path)


def start_batch(conn: sqlite3.Connection, task_id: str, items: list[dict]) -> str:
    """提交一批导出：规范化去重后原子创建 job+items；同参数恢复复用（失败项重试、缺文件重导）。"""
    from ..db import atomic
    for it in items:
        from .assets import check_export_asset
        check_export_asset(conn,task_id,it["asset_id"])
        it["source_fingerprint"] = conn.execute("SELECT fingerprint_at_lock FROM task_assets WHERE task_id=? AND asset_id=?",(task_id,it["asset_id"])).fetchone()[0]
    # 规范化去重：同输出参数（kind+asset+时点/边界）归并为一个 item，来源候选合并保留
    merged: dict[str, dict] = {}
    for it in items:
        key = _item_key(it)
        if key in merged:
            prev = merged[key]
            prev.setdefault("sources", [dict(prev)]).append(dict(it))
            if it.get("candidate_id") and it.get("candidate_id") != prev.get("candidate_id"):
                prev["candidate_id"] = f"{prev.get('candidate_id', '')}|{it.get('candidate_id')}"
            for k in ("source_type", "from", "run_id", "reason", "promo_worthy", "boundary_note", "analysis_version", "source_runs"):
                if prev.get(k) is None and it.get(k) is not None:
                    prev[k] = it[k]
        else:
            merged[key] = dict(it)
    items = list(merged.values())
    digest = _batch_digest(task_id, items)
    jid = _new_job_id(task_id, digest)
    now = _now()
    existing = conn.execute("SELECT status FROM export_jobs WHERE export_job_id=?", (jid,)).fetchone()
    if existing is None:
        with atomic(conn):
            if conn.execute("SELECT 1 FROM export_jobs WHERE export_job_id=?", (jid,)).fetchone():
                return jid
            conn.execute(
                "INSERT INTO export_jobs(export_job_id,task_id,batch_digest,status,total_items,done_items,failed_items,created_at,updated_at) VALUES(?,?,?,?,?,0,0,?,?)",
                (jid, task_id, digest, "running", len(items), now, now),
            )
            for it in items:
                key = _item_key(it)
                meta = {k: it.get(k) for k in ("source_type", "from", "run_id", "reason", "promo_worthy",
                                                 "boundary_note", "analysis_version", "source_runs", "frames", "source_fingerprint", "execution_id", "source_pts", "time_base", "pts_ms", "sources", "review_evidence", "quality_version") if it.get(k) is not None}
                if it['kind']=='video':
                    from .video_variants import RENDER_VERSION
                    meta['render_version']=RENDER_VERSION
                conn.execute(
                    "INSERT INTO export_items(export_item_id,export_job_id,task_id,kind,asset_id,candidate_id,t_ms,start_ms,end_ms,status,meta_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"{jid}:{key}", jid, task_id, it["kind"], it["asset_id"], it.get("candidate_id") or key,
                     it.get("t_ms"), it.get("start_ms"), it.get("end_ms"), "pending",
                     json.dumps(meta, ensure_ascii=False) if meta else None, now),
                )
    else:
        # 同参数批次：已完成则复用（核实 done 文件存在）；否则恢复（失败项重试、缺文件重导）
        if existing["status"] != "completed":
            conn.execute("UPDATE export_items SET status='pending', error=NULL, updated_at=? WHERE export_job_id=? AND status='failed'", (now, jid))
            conn.execute("UPDATE export_jobs SET status='running', error_json=NULL, updated_at=? WHERE export_job_id=?", (now, jid))
            conn.commit()
            _requeue_missing_files(conn,jid)
        else:
            # 已完成：核实 done 文件是否仍在；缺失则重导
            _requeue_missing_files(conn, jid)
    _spawn(jid)
    return jid


def _requeue_missing_files(conn: sqlite3.Connection, jid: str) -> None:
    """已完成批次中 done 文件丢失/不可解码 → 重新置 pending 并恢复 running。"""
    missing = 0
    for r in conn.execute("SELECT * FROM export_items WHERE export_job_id=? AND status='done'", (jid,)).fetchall():
        valid = False
        try:
            path = EXPORT_DIR / r["file_name"]
            meta = json.loads(r["meta_json"] or "{}")
            results.validate_media(path,r["kind"])
            valid = bool(meta.get("output_sha256")) and meta["output_sha256"] == security.sha256_file(path)
            for variant in meta.get('variant_files',[]):
                derived=EXPORT_DIR/variant['file']
                results.validate_media(derived,'video')
                valid = valid and security.sha256_file(derived)==variant['sha256']
        except Exception:
            pass
        if not valid:
            conn.execute("UPDATE export_items SET status='pending', file_name=NULL, error=NULL, updated_at=? WHERE export_item_id=?",
                         (_now(), r["export_item_id"]))
            missing += 1
    if missing:
        conn.execute("UPDATE export_jobs SET status='running', error_json=NULL, updated_at=? WHERE export_job_id=?", (_now(), jid))
        conn.commit()


def resume_running_jobs() -> None:
    """服务启动时恢复：继续执行因服务退出而中断的 running 任务。"""
    conn = _new_conn()
    try:
        rows = conn.execute("SELECT export_job_id FROM export_jobs WHERE status='running'").fetchall()
        for r in rows:
            _requeue_missing_files(conn,r["export_job_id"])
            _spawn(r["export_job_id"])
    finally:
        conn.close()


def _spawn(jid: str) -> None:
    with _lock:
        if jid in _running:
            return
        _running.add(jid)
    t = threading.Thread(target=_run, args=(jid,), daemon=True)
    t.start()


def _run(jid: str) -> None:
    conn = _new_conn()
    try:
        while True:
            item = conn.execute(
                "SELECT * FROM export_items WHERE export_job_id=? AND status='pending' ORDER BY updated_at LIMIT 1",
                (jid,),
            ).fetchone()
            if item is None:
                break
            _export_one(conn, item)
    finally:
        try:
            _finalize(conn, jid)
        except Exception as exc:
            conn.execute("UPDATE export_jobs SET status='failed',error_json=?,updated_at=? WHERE export_job_id=?",(json.dumps({'message':str(exc)[:400]},ensure_ascii=False),_now(),jid))
        finally:
            conn.close()
            with _lock:
                _running.discard(jid)


def _export_one(conn: sqlite3.Connection, item: sqlite3.Row) -> None:
    try:
        from . import assets as assets_svc
        assets_svc.check_export_asset(conn, item["task_id"], item["asset_id"])
        if item["kind"] == "photo":
            meta = json.loads(item["meta_json"] or "{}")
            out = results.export_photo(conn, item["asset_id"], int(item["t_ms"] or 0), EXPORT_DIR,source_pts=meta.get("source_pts"),time_base=meta.get("time_base"))
        else:
            from .video_variants import export_reviewed_video
            raw_meta=json.loads(item["meta_json"] or "{}")
            media_item={**dict(item),"frames":raw_meta.get("frames",[])}
            out,variants=export_reviewed_video(conn,item["task_id"],media_item,EXPORT_DIR)
        if not out.is_file():
            raise security.SecurityError("export_missing", f"导出文件不存在：{out.name}")
        results.validate_media(out,item["kind"])
        meta = json.loads(item["meta_json"] or "{}")
        if item["kind"] == "photo":
            meta.update(results.photo_identity(out))
        else:
            meta.update(results.video_identity(out))
            meta['variant_files']=variants
        meta["output_sha256"] = security.sha256_file(out)
        meta["output_size"] = out.stat().st_size
        from . import artifacts
        snapshot={**dict(item),**meta}
        for path in [out]+[EXPORT_DIR/v['file'] for v in meta.get('variant_files',[])]:
            artifacts.record(conn,item['task_id'],path,[snapshot])
        conn.execute("UPDATE export_items SET meta_json=? WHERE export_item_id=?", (json.dumps(meta,ensure_ascii=False),item["export_item_id"]))
        conn.execute("UPDATE export_items SET status='done', file_name=?, error=NULL, updated_at=? WHERE export_item_id=?",
                     (out.name, _now(), item["export_item_id"]))
    except Exception as e:  # 部分失败：记录原因，继续下一项
        conn.execute("UPDATE export_items SET status='failed', error=?, updated_at=? WHERE export_item_id=?",
                     (str(e)[:500], _now(), item["export_item_id"]))
    conn.commit()


def _source_name(conn: sqlite3.Connection, asset_id: str) -> str:
    row = conn.execute("SELECT source_path FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
    return Path(row["source_path"]).name if row else asset_id


def _write_manifest(conn: sqlite3.Connection, jid: str) -> str | None:
    rows = conn.execute(
        "SELECT * FROM export_items WHERE export_job_id=? AND status='done' ORDER BY kind, asset_id",
        (jid,),
    ).fetchall()
    if not rows:
        return None
    items = []
    for r in rows:
        meta = json.loads(r["meta_json"]) if r["meta_json"] else {}
        items.append({
            "candidate_id": r["candidate_id"],
            "kind": r["kind"],
            "asset_id": r["asset_id"],
            "source": _source_name(conn, r["asset_id"]),
            "t_ms": r["t_ms"],
            "start_ms": r["start_ms"],
            "end_ms": r["end_ms"],
            "file": r["file_name"],
            **meta,
        })
    out = EXPORT_DIR / f"批量导出清单-{jid}.json"
    doc = {"export_job_id": jid, "created_at": _now(), "items": items}
    if out.exists():
        try:previous = json.loads(out.read_text(encoding="utf-8"))
        except (ValueError,OSError):previous={}
        if previous.get("items") == items:
            from . import artifacts
            try:
                artifacts.record(conn,rows[0]['task_id'],out,items)
                return out.name
            except security.SecurityError as exc:
                if exc.code!='artifact_conflict':raise
                # A previous failed restore may have written a new timestamp
                # under a historical name. Preserve it and publish a new name.
        out = results._unique_path(EXPORT_DIR,out.name)
    # A receipt survives a missing file or workspace restore. A new manifest
    # contains a new creation time: never reuse that immutable receipt's name.
    base=out
    suffix=0
    while out.exists() or conn.execute('SELECT 1 FROM artifact_receipts WHERE file_name=?',(out.name,)).fetchone():
        suffix+=1
        out=base.with_name(f'{base.stem}_{suffix}{base.suffix}')
    temp = out.with_suffix(".tmp")
    temp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(out)
    from . import artifacts
    artifacts.record(conn,rows[0]['task_id'],out,items)
    return out.name


def _finalize(conn: sqlite3.Connection, jid: str) -> None:
    row = conn.execute(
        "SELECT COUNT(*) c, SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) d, SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) f FROM export_items WHERE export_job_id=?",
        (jid,),
    ).fetchone()
    done = row["d"] or 0
    failed = row["f"] or 0
    status = "running" if done + failed < row["c"] else ("completed" if failed == 0 else ("failed" if done == 0 else "partial"))
    manifest_file = _write_manifest(conn, jid)
    conn.execute("UPDATE export_jobs SET status=?, done_items=?, failed_items=?, manifest_file=?, error_json=NULL, updated_at=? WHERE export_job_id=?",
                 (status, done, failed, manifest_file, _now(), jid))
    conn.commit()


def get_job(conn: sqlite3.Connection, jid: str) -> dict:
    job = conn.execute("SELECT * FROM export_jobs WHERE export_job_id=?", (jid,)).fetchone()
    if job is None:
        raise security.SecurityError("export_job_not_found", f"导出任务不存在：{jid}")
    rows=conn.execute("SELECT * FROM export_items WHERE export_job_id=? ORDER BY kind, updated_at",(jid,)).fetchall()
    output=[];snapshots=[];damaged=False;render_current=True
    for row in rows:
        meta=json.loads(row['meta_json'] or '{}');status=row['status'];error=row['error']
        if row['kind']=='video':
            from .video_variants import RENDER_VERSION
            render_current=render_current and meta.get('render_version',1)==RENDER_VERSION
        if status=='done':
            from . import artifacts
            try:
                artifacts.verify(conn,row['file_name'])
                for variant in meta.get('variant_files',[]):artifacts.verify(conn,variant['file'])
            except security.SecurityError as exc:status='needs_repair';error=exc.message;damaged=True
        snapshots.extend(meta.get('sources') or [{**dict(row),**meta}])
        output.append({'candidate_id':row['candidate_id'],'kind':row['kind'],'asset_id':row['asset_id'],
                       't_ms':row['t_ms'],'start_ms':row['start_ms'],'end_ms':row['end_ms'],
                       'status':status,'file_name':row['file_name'],'error':error,'source':_source_name(conn,row['asset_id']),
                       'variant_files':meta.get('variant_files',[])})
    error=json.loads(job['error_json'] or 'null')
    if job['manifest_file']:
        try:
            from . import artifacts
            artifacts.verify(conn,job['manifest_file'])
        except security.SecurityError as exc:damaged=True;error={'message':exc.message}
    fields=('candidate_id','kind','asset_id','t_ms','start_ms','end_ms','review_evidence','quality_version')
    signature=lambda items:sorted(json.dumps({k:i.get(k) for k in fields},sort_keys=True,ensure_ascii=False) for i in items)
    try:current=signature(snapshots)==signature(results.reviewed_items(conn,job['task_id']))
    except security.SecurityError:current=False
    return {'export_job_id':jid,'task_id':job['task_id'],'created_at':job['created_at'],
            'status':'needs_repair' if damaged and job['status']!='running' else job['status'],
            'total_items':job['total_items'],'done_items':sum(i['status']=='done' for i in output),
            'failed_items':sum(i['status'] in ('failed','needs_repair') for i in output),
            'manifest_file':job['manifest_file'] if not damaged else None,
            'snapshot_matches_review':current,'render_matches_current':render_current,'error':error,'items':output}


def get_task_job(conn: sqlite3.Connection, task_id: str) -> dict | None:
    """返回该任务最近一批导出（历史批次独立保留）。"""
    job = conn.execute(
        "SELECT export_job_id FROM export_jobs WHERE task_id=? ORDER BY created_at DESC LIMIT 1", (task_id,)
    ).fetchone()
    return get_job(conn, job["export_job_id"]) if job else None

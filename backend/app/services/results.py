"""任务分析结果聚合与导出：从 analysis_runs / experiments 读取真实结果，按真实来源映射素材。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .. import security
from .assets import new_id


def _asset_map(conn: sqlite3.Connection, task_id: str) -> dict[str, str]:
    """source 文件名 → asset_id（供 experiments 结果回填资产）。"""
    rows = conn.execute(
        "SELECT a.asset_id, a.source_path FROM task_assets ta JOIN assets a ON a.asset_id=ta.asset_id WHERE ta.task_id=?",
        (task_id,),
    ).fetchall()
    m = {}
    for r in rows:
        m[Path(r["source_path"]).name] = r["asset_id"]
        m[r["asset_id"]] = r["asset_id"]
    return m


def _candidate_id(kind: str, asset_id: str, t_ms: int | None, start_ms: int | None, end_ms: int | None,
                  from_: str, run_id: str | None) -> str:
    """候选身份 = 媒体时点 + 来源（from/run_id），保证与去重、反馈同口径，不串源。"""
    if kind == "photo":
        return f"photo:{asset_id}:{t_ms}:{from_}:{run_id}"
    return f"video:{asset_id}:{start_ms}:{end_ms}:{from_}:{run_id}"


def _manual_verdict_rows(conn: sqlite3.Connection, task_id: str):
    """读取该任务最新 review_version 的人工决定行（历史版本保留，不跨批次串用）。"""
    rv = conn.execute(
        "SELECT review_version FROM manual_reviews WHERE task_id=? ORDER BY created_at DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if rv is None:
        return None, []
    rows = conn.execute(
        "SELECT * FROM manual_reviews WHERE task_id=? AND review_version=? ORDER BY kind, t_ms, start_ms",
        (task_id, rv["review_version"]),
    ).fetchall()
    return rv["review_version"], rows


def _apply_review_verdicts(conn: sqlite3.Connection, task_id: str, dedup_photos: list[dict], natural_video: list[dict], latest_exec: dict):
    """统一审核视图：冻结人工评价（manual_reviews，须批次命中）+ 实时页面反馈（feedback_events，按候选身份，优先级更高）。
    最终交付集合由此视图导出；自动选片（recommended/video_segments）不在此过滤。"""
    rv, rows = _manual_verdict_rows(conn, task_id)
    photo_v = [dict(r) for r in rows if r["kind"] == "photo"]
    video_v = [dict(r) for r in rows if r["kind"] == "video"]
    applied: list[dict] = []
    pending: list[dict] = []

    def _batch_ok(v):
        return v.get("execution_id") is not None and v.get("execution_id") == latest_exec.get(v.get("asset_id"))

    for p in dedup_photos:
        best = None
        for v in photo_v:
            if v["asset_id"] == p.get("asset_id") and abs((v["t_ms"] or 0) - (p.get("t_ms") or 0)) <= 50:
                if best is None or abs((v["t_ms"] or 0) - (p.get("t_ms") or 0)) < abs((best["t_ms"] or 0) - (p.get("t_ms") or 0)):
                    best = v
        if best:
            if _batch_ok(best) and best.get("t_ms") == p.get("t_ms"):
                p["manual_decision"] = best["decision"]
                p["manual_reason"] = best["reason"]
                p["manual_score"] = best["score"]
                applied.append({"kind": "photo", "asset_id": p["asset_id"], "t_ms": p.get("t_ms"),
                                "decision": best["decision"], "reason": best["reason"]})
            else:
                p["manual_decision"] = None
                pending.append({"kind": "photo", "asset_id": p["asset_id"], "t_ms": p.get("t_ms"),
                                "decision": best["decision"], "reason": best["reason"],
                                "review_execution_id": best.get("execution_id"), "candidate_execution_id": latest_exec.get(p.get("asset_id"))})
        else:
            p["manual_decision"] = None

    for s in natural_video:
        best = None
        for v in video_v:
            if v["asset_id"] == s.get("asset_id") and (v["start_ms"] or 0) <= (s.get("end_ms") or 0) and (s.get("start_ms") or 0) <= (v["end_ms"] or 0):
                span = (v["end_ms"] or 0) - (v["start_ms"] or 0)
                if best is None or span < ((best["end_ms"] or 0) - (best["start_ms"] or 0)):
                    best = v
        if best:
            if _batch_ok(best) and best.get("start_ms") == s.get("start_ms") and best.get("end_ms") == s.get("end_ms"):
                s["manual_decision"] = best["decision"]
                s["manual_reason"] = best["reason"]
                s["manual_score"] = best["score"]
                applied.append({"kind": "video", "asset_id": s["asset_id"], "start_ms": s.get("start_ms"), "end_ms": s.get("end_ms"),
                                "decision": best["decision"], "reason": best["reason"]})
            else:
                s["manual_decision"] = None
                pending.append({"kind": "video", "asset_id": s["asset_id"], "start_ms": s.get("start_ms"), "end_ms": s.get("end_ms"),
                                "decision": best["decision"], "reason": best["reason"],
                                "review_execution_id": best.get("execution_id"), "candidate_execution_id": latest_exec.get(s.get("asset_id"))})
        else:
            s["manual_decision"] = None

    # 实时页面反馈（feedback_events）：按候选身份，优先级高于冻结评价
    fb_rows = conn.execute(
        "SELECT feedback_id,kind, payload_json, created_at FROM feedback_events WHERE task_id=? AND kind IN ('keep','reject','trim','boundary_adjust','clear_decision') ORDER BY created_at, rowid",
        (task_id,),
    ).fetchall()
    fb_dec: dict[str, str] = {}
    fb_ids = {}
    bnd_ids = {}
    fb_bnd: dict[str, dict] = {}
    for r in fb_rows:
        try:
            p = json.loads(r["payload_json"])
        except Exception:
            continue
        cid = p.get("candidate_id")
        if not cid:
            continue
        if r["kind"] in ("keep", "reject", "trim"):
            fb_dec[cid] = r["kind"]
            fb_ids[cid] = r["feedback_id"]
        elif r["kind"] == "clear_decision":
            fb_dec[cid] = None
            fb_ids[cid] = r["feedback_id"]
        elif r["kind"] == "boundary_adjust" and p.get("start_ms") is not None and p.get("end_ms") is not None:
            bnd_ids[cid] = r["feedback_id"]
            fb_bnd[cid] = {"start_ms": int(p["start_ms"]), "end_ms": int(p["end_ms"])}

    def _final(c, manual_dec):
        return fb_dec.get(c.get("candidate_id"), manual_dec)

    for p in dedup_photos:
        p["review_decision"] = _final(p, p.get("manual_decision"))
    for s in natural_video:
        s["review_decision"] = _final(s, s.get("manual_decision"))
        bnd = fb_bnd.get(s.get("candidate_id"))
        if bnd:
            s["review_start_ms"] = bnd["start_ms"]
            s["review_end_ms"] = bnd["end_ms"]

    for candidate in dedup_photos + natural_video:
        cid=candidate.get("candidate_id")
        candidate["review_evidence"]={"decision_feedback_id":fb_ids.get(cid),"boundary_feedback_id":bnd_ids.get(cid),"manual_review_version":rv if candidate.get("manual_decision") else None}
    final_photos = [p for p in dedup_photos if p.get("review_decision") == "keep"]
    # 待修剪（trim）不等于已保留：须边界确认后才进正式成品；keep 若带边界调整则用调整后边界
    final_videos = []
    for s in natural_video:
        if s.get("review_decision") != "keep":
            continue
        d = dict(s)
        if s.get("review_start_ms") is not None and s.get("review_end_ms") is not None:
            d["start_ms"] = s["review_start_ms"]
            d["end_ms"] = s["review_end_ms"]
            d["boundary_adjusted"] = True
        final_videos.append(d)
    return {"review_version": rv, "applied": applied, "pending": pending,
            "final_photos": final_photos, "final_videos": final_videos}


def _uniq_by_candidate(items: list[dict]) -> list[dict]:
    seen: set = set()
    out: list[dict] = []
    for it in items:
        k = it.get("candidate_id")
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


def get_task_results_v3(conn: sqlite3.Connection, task_id: str, task: dict) -> dict:
    """v3 新路径结果聚合：读各素材 v3 注解，任务级重选（每素材独立选片 + 任务级 Top 1–3）。"""
    exec_rows = conn.execute(
        "SELECT asset_id, execution_id FROM executions WHERE task_id=? AND status='complete' ORDER BY created_at DESC",
        (task_id,),
    ).fetchall()
    latest_exec: dict[str, str] = {}
    for r in exec_rows:
        latest_exec.setdefault(r["asset_id"], r["execution_id"])

    from .selection_v2 import SelectParams, select as select_v3
    annotations: list[dict] = []
    run_by_asset: dict[str, str] = {}
    src_by_asset: dict[str, str] = {}
    for aid, eid in latest_exec.items():
        row = conn.execute(
            "SELECT run_id, source, annotations_json FROM analysis_runs WHERE task_id=? AND asset_id=? AND execution_id=? AND status='complete' AND annotations_json IS NOT NULL ORDER BY created_at DESC LIMIT 1",
            (task_id, aid, eid),
        ).fetchone()
        if row is None:
            continue
        run_by_asset[aid] = row["run_id"]
        src_by_asset[aid] = row["source"]
        for a in json.loads(row["annotations_json"]):
            a = dict(a)
            a["asset_id"] = aid
            annotations.append(a)

    sel = select_v3(annotations, None, SelectParams())

    def _attach_photo(items: list[dict]) -> list[dict]:
        out: list[dict] = []
        for p in items:
            aid = p.get("asset_id")
            d = dict(p)
            d["source_type"] = src_by_asset.get(aid, "mock")
            d["run_id"] = run_by_asset.get(aid)
            d["from"] = "selection_v2"
            d["execution_id"] = latest_exec.get(aid)
            d["candidate_id"] = _candidate_id("photo", aid, p.get("t_ms"), None, None, "selection_v2", run_by_asset.get(aid))
            out.append(d)
        return out

    def _attach_video(items: list[dict]) -> list[dict]:
        out: list[dict] = []
        for s in items:
            aid = s.get("asset_id")
            d = dict(s)
            d["source_type"] = src_by_asset.get(aid, "mock")
            d["run_id"] = run_by_asset.get(aid)
            d["from"] = "selection_v2"
            d["execution_id"] = latest_exec.get(aid)
            d["candidate_id"] = _candidate_id("video", aid, None, s.get("start_ms"), s.get("end_ms"), "selection_v2", run_by_asset.get(aid))
            out.append(d)
        return out

    curated_photos = _attach_photo(sel.get("curated_photos", []))
    alternative_photos = _attach_photo(sel.get("alternative_photos", []))
    photos = _attach_photo(sel.get("photos", []))
    curated_videos = _attach_video(sel.get("curated_videos", []))
    alternative_videos = _attach_video(sel.get("alternative_videos", []))
    video_segments = _attach_video(sel.get("video_segments", []))
    top_photo = curated_photos[0] if curated_photos else None

    return {
        "task_id": task_id,
        "pipeline_version": 3,
        "quality_errors": [],
        "visible_batches": latest_exec,
        "active_executions": {aid: {"execution_id": eid, "status": "complete"} for aid, eid in latest_exec.items()},
        "status": task["status"],
        "has_results": bool(curated_photos or alternative_photos or curated_videos or alternative_videos),
        "video_segments": video_segments,
        "video_candidates": video_segments,
        "unverified_videos": [],
        "video_fragments": video_segments,
        "video_representatives": video_segments,
        "photos": photos,
        "curated_photos": curated_photos,
        "alternative_photos": alternative_photos,
        "curated_videos": curated_videos,
        "alternative_videos": alternative_videos,
        "recommended_photos": curated_photos,
        "top_photo": top_photo,
        "manual_review_version": None,
        "manual_verdicts_applied": [],
        "manual_verdicts_pending": [],
        "final_recommended_photos": curated_photos,
        "final_video_segments": curated_videos,
    }


def get_task_results(conn: sqlite3.Connection, task_id: str, *, execution_overrides: dict | None = None, require_qc: bool = True) -> dict:
    """聚合任务的分析结果：视频段 + 照片候选，带稳定候选标识、来源类型。"""
    task = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if task is None:
        raise security.SecurityError("task_not_found", f"任务不存在：{task_id}")

    from .quality_policy import version as quality_policy_version, selection_version, is_v3
    if is_v3(conn, task_id):
        return get_task_results_v3(conn, task_id, task)
    quality_v2 = quality_policy_version(conn,task_id)>=2
    selection_v2 = selection_version(conn,task_id)>=2

    asset_map = _asset_map(conn, task_id)
    video = []
    photos = []

    # 有效批次隔离：每个素材取最新已完成执行（executions）；无执行成员记录时回退到 run 自身 execution_id
    exec_rows = conn.execute(
        "SELECT asset_id, execution_id FROM executions WHERE task_id=? AND status='complete' ORDER BY created_at DESC",
        (task_id,),
    ).fetchall()
    latest_exec: dict[str, str] = {}
    for r in exec_rows:
        latest_exec.setdefault(r["asset_id"], r["execution_id"])
    all_executions = conn.execute("SELECT * FROM executions WHERE task_id=? ORDER BY created_at DESC",(task_id,)).fetchall()
    active_executions = {}
    for execution in all_executions:
        active_executions.setdefault(execution["asset_id"],dict(execution))
        # If there is no published batch yet, completed partial members are visible
        # for review, with explicit unfinished publication state and QC gating.
        latest_exec.setdefault(execution["asset_id"],execution["execution_id"])
    latest_exec.update(execution_overrides or {})
    visible_run_ids: dict[str, set[str]] = {}
    if latest_exec:
        for _aid, _eid in latest_exec.items():
            from .batch_identity import runs as member_runs
            _runs = member_runs(conn,task_id,_aid,_eid)
            visible_run_ids[_aid] = {_r["run_id"] for _r in _runs}
    # Legacy per-asset fallback; never mix foreign batch members.
    for r in conn.execute(
            "SELECT asset_id, execution_id FROM analysis_runs WHERE task_id=? AND status='complete' AND execution_id IS NOT NULL ORDER BY created_at DESC",
            (task_id,),
    ).fetchall():
        latest_exec.setdefault(r["asset_id"], r["execution_id"])
    # preserve membership of modern assets; legacy fallback stays per-asset

    def _run_visible(aid: str, run_id: str) -> bool:
        if visible_run_ids:
            if aid in visible_run_ids:
                return run_id in visible_run_ids[aid]
            rr = conn.execute("SELECT execution_id FROM analysis_runs WHERE run_id=?", (run_id,)).fetchone()
            return rr is not None and rr["execution_id"] == latest_exec.get(aid)
        return True  # 回退模式在下方用 latest_exec 过滤

    # 1) analysis_runs 的 selections（全片粗筛/细查/校准），仅最新批次
    rows = conn.execute(
        "SELECT asset_id, source, run_id, selections_json, execution_id FROM analysis_runs WHERE task_id=? AND status='complete' AND selections_json IS NOT NULL",
        (task_id,),
    ).fetchall()
    for r in rows:
        if visible_run_ids and not _run_visible(r["asset_id"], r["run_id"]):
            continue  # 非可见批次成员，不参与拼接
        if not visible_run_ids and latest_exec.get(r["asset_id"]) not in (None, r["execution_id"]):
            continue  # 回退：非最新批次
        sel = json.loads(r["selections_json"])
        identity = r["run_id"] + (":" + latest_exec[r["asset_id"]] if r["asset_id"] in latest_exec else "")
        for s in sel.get("video_segments", []):
            video.append({**s, "asset_id": r["asset_id"], "source_type": r["source"],
                          "run_id": r["run_id"], "from": "analysis_runs", "execution_id":latest_exec.get(r["asset_id"]),
                          "candidate_id": _candidate_id("video", r["asset_id"], None, s.get("start_ms"), s.get("end_ms"), "analysis_runs", identity)})
        for p in sel.get("photo_frames", []):
            photos.append({**p, "asset_id": r["asset_id"], "source_type": r["source"],
                           "run_id": r["run_id"], "from": "analysis_runs", "execution_id":latest_exec.get(r["asset_id"]),
                           "candidate_id": _candidate_id("photo", r["asset_id"], p.get("t_ms"), None, None, "analysis_runs", identity)})

    # 2) experiments 的照片评测（按 source 文件名回填 asset_id）
    for r in conn.execute(
        "SELECT result_json, experiment FROM experiments WHERE task_id=? AND status='complete' AND mode='photo' AND result_json IS NOT NULL",
        (task_id,),
    ).fetchall():
        d = json.loads(r["result_json"])
        for a in d.get("annotations", []):
            src_name = (a.get("source") or "").split("/")[-1]
            aid = asset_map.get(src_name) or asset_map.get(a.get("asset_id")) or a.get("asset_id")
            if not aid or aid in latest_exec:
                continue  # 无法映射到素材，丢弃而非混入
            photos.append({**a, "asset_id": aid, "source_type": "experiment",
                           "run_id": r["experiment"], "from": "experiments",
                           "candidate_id": _candidate_id("photo", aid, a.get("t_ms"), None, None, "experiments", r["experiment"])})

    # 去重：同素材同时点同来源去重；不同素材/来源不合并
    seen = set()
    dedup_photos = []
    for p in photos:
        k = (p.get("asset_id"), p.get("t_ms"), p.get("from"), p.get("run_id"))
        if k in seen:
            continue
        seen.add(k)
        dedup_photos.append(p)
    dedup_photos.sort(key=lambda x: (x.get("asset_id", ""), x.get("t_ms", 0)))
    # 保守合并：同一时刻（±500ms）多 run 质量判断冲突取更差（避免误判 blurry/occluded 为精选）
    _by_asset_ph = {}
    for p in dedup_photos:
        _by_asset_ph.setdefault(p.get("asset_id"), []).append(p)
    dedup_photos = []
    for _aid, _pl in _by_asset_ph.items():
        _by_t = {}
        for p in _pl:
            _by_t.setdefault(p.get("t_ms", 0), []).append(p)
        dedup_photos.extend(_conservative_merge(g) for g in _by_t.values())
    dedup_photos.sort(key=lambda x: (x.get("asset_id", ""), x.get("t_ms", 0)))
    # 相似择优去重：按素材分组，同时间窗同画面签名只留最佳（避免重新汇入各阶段全部候选）
    from .analysis import dedup_similar_photos
    _by_asset: dict[str, list[dict]] = {}
    for p in dedup_photos:
        _by_asset.setdefault(p.get("asset_id"), []).append(p)
    dedup_photos = []
    for _aid, _plist in _by_asset.items():
        dedup_photos.extend(_plist if selection_v2 else dedup_similar_photos(_plist, set(), window_ms=1500))
    dedup_photos.sort(key=lambda x: (x.get("asset_id", ""), x.get("t_ms", 0)))

    seen_v = set()
    dedup_video = []
    for v in video:
        k = (v.get("asset_id"), v.get("start_ms"), v.get("end_ms"), v.get("from"), v.get("run_id"))
        if k in seen_v:
            continue
        seen_v.add(k)
        dedup_video.append(v)
    dedup_video.sort(key=lambda s: (s.get("asset_id", ""), s.get("start_ms", 0)))
    # 重叠视频段分组（保留原候选/来源/反馈关联，不并集）
    video_fragments = _group_video_segments(dedup_video)
    # 自然连续段：用全量帧（粗筛+细查+加密的 annotations，含候选之外前后画面）确定自然起止
    from .analysis import SelectParams, merge_natural_segments
    # 质检失败帧（photo_checks 当前 check_version fail）：照片降级与视频修剪分开，不互相污染
    from .analysis_job import _check_version as _pcv2
    from .analysis import _photo_quality_score as _pqs2
    _check_fail_photo: set = set()
    _check_fail_video: set = set()
    quality_errors = []
    checked_pass = set()
    photo_check_evidence = {}
    managed_assets = {r["asset_id"] for r in all_executions} | set(execution_overrides or {})
    _video_hard_fail: set = set()
    def _hard_issues(issues_json):
        try:
            _iss = json.loads(issues_json or "[]")
        except Exception:
            return False
        _hard_kw = ("模糊", "残影", "虚影", "遮挡", "爪子不自然", "眼睛", "眯", "脸部不饱满", "看不到脸")
        return any(any(k in (s or "") for k in _hard_kw) for s in _iss)
    # 每素材参考图身份（与生成端同口径）：selections photo_frames 综合分最高正例
    for _aid, _batch in latest_exec.items():
        _pf: dict[int, dict] = {}
        for _r in conn.execute("SELECT selections_json FROM analysis_runs WHERE task_id=? AND asset_id=? AND status='complete' AND selections_json IS NOT NULL AND execution_id=?", (task_id, _aid, _batch)).fetchall():
            for _a in json.loads(_r["selections_json"]).get("photo_frames", []):
                if _a.get("promo_worthy") is True:
                    _pf[_a["t_ms"]] = _a
        _ref_t = max(_pf, key=lambda _t: _pqs2(_pf[_t]), default=None)
        _cvp = _pcv2(conn, task_id, None, _batch)
        _cvv = _pcv2(conn, task_id, None, _batch)
        for _c in conn.execute("SELECT asset_id, t_ms, check_kind, tier, status, quality_pass, issues_json, evidence_json FROM photo_checks WHERE task_id=? AND asset_id=? AND check_version IN (?,?)", (task_id, _aid, _cvp, _cvv)).fetchall():
            _f = (_c["asset_id"], _c["t_ms"])
            if _c['check_kind']=='photo':
                checks=json.loads(_c['evidence_json'] or '[]')
                last=checks[-1].get('assessment',{}) if checks else {}
                photo_check_evidence[_f]={'status':_c['status'],'reason':last.get('promo_reason') or '；'.join(json.loads(_c['issues_json'] or '[]')),
                                          'observations':last.get('observations'),'visible_evidence':last.get('evidence')}
            if _c["status"] == "pass" and _c["check_kind"] == "photo":
                checked_pass.add(_f)
            if _c["status"] not in ("pass","fail"):
                quality_errors.append(dict(_c))
                _check_fail_photo.add(_f)
            if _c["check_kind"] == "video":
                if _c["status"] == "fail" and _c["quality_pass"] == 0:
                    _check_fail_video.add(_f)
            else:
                if _c["status"] == "fail" and _c["quality_pass"] == 0:
                    _check_fail_photo.add(_f)
                    # 照片硬伤（明显模糊/遮挡/爪子/眼睛）也参与视频段修剪；仅角度/光线/构图等软问题不切视频
                    if _hard_issues(_c["issues_json"]):
                        _video_hard_fail.add(_f)
    ann_rows = conn.execute(
        "SELECT asset_id, source, run_id, annotations_json, created_at, execution_id FROM analysis_runs WHERE task_id=? AND status='complete' AND annotations_json IS NOT NULL ORDER BY created_at",
        (task_id,),
    ).fetchall()
    frames_by_asset: dict[str, dict[int, list[dict]]] = {}  # asset_id -> t_ms -> [多次判断]
    for r in ann_rows:
        if visible_run_ids and not _run_visible(r["asset_id"], r["run_id"]):
            continue  # 非可见批次成员，不参与拼接
        if not visible_run_ids and latest_exec.get(r["asset_id"]) not in (None, r["execution_id"]):
            continue  # 回退：非最新批次
        aid = r["asset_id"]
        fm = frames_by_asset.setdefault(aid, {})
        for a in json.loads(r["annotations_json"]):
            t = a["t_ms"]
            a = {**a, "source": r["source"], "run_id": r["run_id"]}
            fm.setdefault(t, []).append(a)  # 保留同源帧多次判断，不覆盖
    # 同源帧（同一 t_ms）多次判断冲突：保守合并 + 保留来源冲突证据；相邻不同源帧不合并
    frames_by_asset = {
        aid: {t: _conservative_merge(group) for t, group in fm.items()}
        for aid, fm in frames_by_asset.items()
    }
    # 视频段修剪：视频单图失败 + 照片硬伤（模糊/遮挡/爪子/眼睛）参与；孤立失败帧不打断，仅密集坏区标弱信号
    _fail_by_asset: dict[str, list[int]] = {}
    for (_aid, _t) in (_check_fail_video | _video_hard_fail):
        _fail_by_asset.setdefault(_aid, []).append(_t)
    _dense_fail: set = set()
    for _aid, _ts in _fail_by_asset.items():
        _ts_sorted = sorted(set(_ts))
        for _t in _ts_sorted:
            if sum(1 for _x in _ts_sorted if abs(_x - _t) <= 1500) >= 3:
                _dense_fail.add((_aid, _t))
    # Frame-only defects flag context for review; segment judgment owns continuous boundaries.
    natural_video = []
    for aid in {s.get("asset_id") for s in video_fragments}:
        frames = sorted((frames_by_asset.get(aid) or {}).values(), key=lambda f: f.get("t_ms", 0))
        segs = merge_natural_segments(frames, SelectParams())
        if not segs and not frames:
            # 无标注：退回片段区间本身（仅有片段无帧信息）；已有标注但不合格则不回退冒充
            for frag in [s for s in video_fragments if s.get("asset_id") == aid]:
                segs.append({"start_ms": frag.get("start_ms"), "end_ms": frag.get("end_ms"),
                             "frames": [], "path": "natural", "source": "auto"})
        for seg in segs:
            seg["asset_id"] = aid
            seg["from"] = "natural"
            # 来源追溯：按实际参与决策的全部来源汇总（含同帧冲突 run），不把 mock+real 标成 real
            run_ids: set = set()
            sources: set = set()
            for f in seg.get("frames", []):
                if f.get("run_id"):
                    run_ids.add(f["run_id"])
                run_ids.update(f.get("conflict_runs") or [])
                if f.get("source"):
                    sources.add(f["source"])
                sources.update(f.get("conflict_sources") or [])
            source_type = "mock" if sources == {"mock"} else ("real" if sources == {"real"} else ("mixed" if sources else "unknown"))
            seg["source_type"] = source_type
            seg["run_id"] = None
            seg["source_runs"] = sorted(run_ids)
            # 分析版本+批次+配置快照构造身份，同起止跨批次不串用
            from .analysis import PROMPT_VERSION, SELECTION_RULE_VERSION
            import hashlib as _hl
            _batch = latest_exec.get(aid) or "nb"
            _snap = (task["snapshot_ref_json"] or "")[:200]
            version = f"pv{PROMPT_VERSION}-rv{SELECTION_RULE_VERSION}"
            identity = f"{version}-b{_hl.sha256(_batch.encode()).hexdigest()[:8]}-s{_hl.sha256(_snap.encode()).hexdigest()[:8]}"
            seg["analysis_version"] = version
            seg["execution_id"] = _batch
            seg["candidate_id"] = _candidate_id("video", aid, None, seg["start_ms"], seg["end_ms"], "natural", identity)
            natural_video.append(seg)
    natural_video.sort(key=lambda s: (s.get("asset_id", ""), s.get("start_ms", 0)))
    # 时长仅作参考：短段不统一删除，有精彩帧或足够高质量即保留；低质量短碎片才丢弃（不靠凑时长保留差画面）
    _min_dur = SelectParams().min_duration_ms
    def _seg_watchable(s):
        dur = (s.get("end_ms", 0) - s.get("start_ms", 0))
        if dur >= _min_dur:
            return True
        frames = s.get("frames", [])
        if not frames:
            return True  # 无帧信息不臆断
        aes = [float(f.get("aes_score", 0) or 0) for f in frames]
        avg = sum(aes) / len(aes) if aes else 0
        return avg >= 0.8 or any(a >= 0.85 for a in aes)
    natural_video = [s for s in natural_video if _seg_watchable(s)]

    # 段级连续判断（video_evals）：用连续上下文得到的自然起止修剪段；不可用段剔除
    def _apply_video_evals(segs):
        out = []
        for s in segs:
            from .batch_identity import video_version
            ev = conn.execute(
                "SELECT * FROM video_evals WHERE task_id=? AND asset_id=? AND execution_id=? AND judgment_version=? AND start_ms=? AND end_ms=? ORDER BY created_at DESC LIMIT 1",
                (task_id,s["asset_id"],latest_exec.get(s["asset_id"]),video_version(conn,task_id),s["start_ms"],s["end_ms"]),
            ).fetchone()
            if ev is not None and ev["status"] != "complete":
                out.append({**s,"judgment_status":ev["status"],"judgment_error":ev["error"]})
                continue
            if ev is not None and not quality_v2 and ev["segment_usable"] == 0:
                continue  # 连续判断为不可用
            if ev is not None and quality_v2:
                from .continuous_quality import parse as parse_continuous
                try:
                    assessment=json.loads(ev['assessment_json'] or '{}')
                    inputs=json.loads(ev['input_json'])
                    checked=parse_continuous(json.dumps(assessment),inputs['frames'],inputs['bounds'],inputs['context'])
                except (ValueError,KeyError,TypeError) as exc:
                    out.append({**s,'judgment_status':'error','judgment_error':'连续证据不完整或自相矛盾：'+str(exc)})
                    continue
                uncertain=[e['t_ms'] for e in checked['evidence'] if e['viewing']=='uncertain' and inputs['bounds'][0]<=e['t_ms']<inputs['bounds'][1]]
                uncertain.extend(e['t_ms'] for e in checked['observation_conflicts'] if inputs['bounds'][0]<=e['t_ms']<inputs['bounds'][1])
                if uncertain and require_qc:
                    quality_errors.append({'asset_id':s['asset_id'],'check_kind':'video','status':'uncertain',
                                           'start_ms':s['start_ms'],'end_ms':s['end_ms'],
                                           'issues_json':json.dumps(['连续判断仍有未看准的画面',uncertain],ensure_ascii=False)})
                for part,interval in enumerate(checked['segments']):
                    out.append({**s,'original_start_ms':s['start_ms'],'original_end_ms':s['end_ms'],
                                'start_ms':interval['start_ms'],'end_ms':interval['end_ms'],
                                'frames':[f for f in s.get('frames',[]) if interval['start_ms']<=f['t_ms']<interval['end_ms']],
                                'candidate_id':s['candidate_id']+':'+ev['eval_id']+':part'+str(part),
                                'judgment_id':ev['eval_id'],'judgment_status':'complete','quality_verified':True,
                                'segment_eval':interval['reason'],'boundary_evidence':{'start':interval['start_reason'],'end':interval['end_reason']},
                                'continuous_evidence':checked['evidence'],'omitted_core_evidence':checked['omissions']})
                continue
            if ev is not None and ev["segment_usable"] == 1 and ev["usable_start_ms"] is not None and ev["usable_end_ms"] is not None:
                d = dict(s)
                d["original_start_ms"], d["original_end_ms"] = s["start_ms"], s["end_ms"]
                d["judgment_id"] = ev["eval_id"]
                d["candidate_id"] += ":" + ev["eval_id"]
                d["start_ms"] = ev["usable_start_ms"]
                d["end_ms"] = ev["usable_end_ms"]
                d["segment_eval"] = ev["reason"]
                d["judgment_status"] = "complete"
                d["quality_verified"] = True
                out.append(d)
            else:
                out.append({**s,"judgment_status":"missing","quality_verified":False})
        return out
    natural_video = _apply_video_evals(natural_video)
    # Preserve candidates for manual review, but unfinished judgments are not automatic
    # recommendations. Their absence must never be reported as a completed quality pass.
    video_candidates = natural_video
    unverified_video = [s for s in natural_video if s['asset_id'] in managed_assets and not s.get('quality_verified')]
    if require_qc:
        for s in unverified_video:
            quality_errors.append({'asset_id':s['asset_id'],'check_kind':'video','status':s.get('judgment_status','missing'),
                                   'start_ms':s['start_ms'],'end_ms':s['end_ms'],
                                   'issues_json':json.dumps([s.get('judgment_error') or '当前批次连续判断尚未完成'],ensure_ascii=False)})
        natural_video = [s for s in natural_video if s['asset_id'] not in managed_assets or s.get('quality_verified')]

    # 分层：精选推荐=promo_worthy 显式为 true；最佳候选=推荐首位（无精选则空）
    from .analysis import _photo_quality_score
    def _rank_key(p):
        return (-_photo_quality_score(p), p.get("t_ms", 0))
    # 同源帧（t_ms 严格相等）的 annotations 完整质量证据：若有 run 判不合格 → 精选降级（不跨邻帧）
    def _frame_unqualified(p):
        fm = frames_by_asset.get(p.get("asset_id")) or {}
        ff = fm.get(p.get("t_ms"))
        return ff is not None and ff.get("promo_worthy") is False
    def _quality_rejected(p):
        return require_qc and (p.get("asset_id"), p.get("t_ms")) in _check_fail_photo
    def _hard_defect(p):
        # 事实观察层面的硬缺陷门：姿态别扭/几乎看不到脸/明显遮挡/严重模糊不得进精选，不靠排到最后掩盖
        obs = p.get("observations") or {}
        if p.get("posture_quality") == "别扭":
            return True
        if obs.get("face_visible") in ("cropped", "occluded"):
            return True
        if obs.get("clarity") in ("blurry", "模糊"):
            return True
        if (p.get("overall_harmony") if p.get("overall_harmony") is not None else 1.0) <= 0.05:
            return True
        return False
    for p in dedup_photos:
        if (p.get('asset_id'),p.get('t_ms')) in photo_check_evidence:
            p['photo_check']=photo_check_evidence[(p['asset_id'],p['t_ms'])]
    recommended = sorted([p for p in dedup_photos if p.get("promo_worthy") is True
                          and not _frame_unqualified(p) and not _quality_rejected(p) and not _hard_defect(p)
                          and (not require_qc or p.get("asset_id") not in managed_assets or (p.get("asset_id"),p.get("t_ms")) in checked_pass)], key=_rank_key)
    # 照片按素材隔离 + 实际画面相似性(dHash)去重：近似重复删除，不同姿态/表情/独立瞬间保留
    if require_qc or not selection_v2:
        recommended = _visual_dedup(recommended, conn, threshold=16)
    top_photo = recommended[0] if recommended else None

    # 统一审核视图叠加（冻结评价+实时反馈）：只用于最终交付，自动选片成绩仍按 recommended/video_segments 统计
    _mv = _apply_review_verdicts(conn, task_id, dedup_photos, video_candidates + video_fragments, latest_exec)
    final_recommended = _mv["final_photos"]

    return {
        "task_id": task_id,
        "quality_errors": quality_errors,
        "visible_batches": latest_exec,
        "active_executions": active_executions,
        "status": task["status"],
        "has_results": bool(video_candidates or video_fragments or dedup_photos),
        "video_segments": natural_video,
        "video_candidates": video_candidates,
        "unverified_videos": unverified_video,
        "video_fragments": video_fragments,
        "video_representatives": natural_video,
        "photos": dedup_photos,
        "recommended_photos": recommended,
        "top_photo": top_photo,
        "manual_review_version": _mv["review_version"],
        "manual_verdicts_applied": _mv["applied"],
        "manual_verdicts_pending": _mv["pending"],
        "final_recommended_photos": final_recommended,
        "final_video_segments": _mv["final_videos"],
    }


def _group_video_segments(segs: list[dict]) -> list[dict]:
    """按素材对重叠区间分组；每组标出最佳代表片段（保留原 candidate_id/来源，不并集成长段）。"""
    if not segs:
        return []
    ordered = sorted(segs, key=lambda s: (s.get("asset_id", ""), s.get("start_ms", 0), s.get("end_ms", 0)))
    result: list[dict] = []
    cluster = 0
    i, n = 0, len(ordered)
    while i < n:
        group = [ordered[i]]
        end = ordered[i].get("end_ms", 0)
        j = i + 1
        while j < n and ordered[j].get("asset_id") == ordered[i].get("asset_id") and ordered[j].get("start_ms", 0) <= end:
            group.append(ordered[j])
            end = max(end, ordered[j].get("end_ms", 0))
            j += 1
        def _seg_quality(s):
            frames = s.get("frames", [])
            aes_vals = [float(f.get("aes_score", 0) or 0) for f in frames]
            return (sum(aes_vals) / len(aes_vals) if aes_vals else 0, len(frames))
        rep = max(group, key=_seg_quality)
        for s in group:
            d = dict(s)
            d["cluster_id"] = cluster
            d["is_representative"] = (s is rep)
            result.append(d)
        cluster += 1
        i = j
    return result


def _extract_frame_pil(conn: sqlite3.Connection, asset_id: str, t_ms: int):
    """从原片抽取 t_ms 处的 PIL 图（旋转转正），用于画面相似性 dHash。"""
    from ..media.sample import extract_frame_at
    row = conn.execute("SELECT source_path FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
    if row is None:
        return None
    img, _ = extract_frame_at(Path(row["source_path"]), int(t_ms or 0))
    return img


def _dhash(img, hash_size: int = 8) -> int:
    """差异哈希（dHash）：缩到 9x8 灰度，按行相邻亮度比较得 64 位。"""
    from PIL import Image
    g = img.convert("L").resize((hash_size + 1, hash_size), Image.LANCZOS)
    px = list(g.getdata())
    bits = 0
    for row in range(hash_size):
        for col in range(hash_size):
            bits = (bits << 1) | (1 if px[row * (hash_size + 1) + col] > px[row * (hash_size + 1) + col + 1] else 0)
    return bits


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _image_features(img):
    import hashlib
    rgb=img.convert('RGB')
    return (rgb.size,hashlib.sha256(rgb.tobytes()).hexdigest()),_dhash(img)


from collections import OrderedDict as _OrderedDict
from threading import RLock as _RLock

_FRAME_FEATURES=_OrderedDict()
_FRAME_FEATURES_LOCK=_RLock()
_FRAME_FEATURES_LIMIT=2048


def _cached_feature(key):
    with _FRAME_FEATURES_LOCK:
        value=_FRAME_FEATURES.get(key)
        if value is not None:_FRAME_FEATURES.move_to_end(key)
        return value


def _save_feature(key,value):
    if value is None:return
    with _FRAME_FEATURES_LOCK:
        _FRAME_FEATURES[key]=value;_FRAME_FEATURES.move_to_end(key)
        while len(_FRAME_FEATURES)>_FRAME_FEATURES_LIMIT:_FRAME_FEATURES.popitem(last=False)


def _warm_frame_features(conn,items):
    if conn is None:return
    from ..media.file_identity import file_identity
    from ..media.sample import iter_source_frames_at_times
    grouped={}
    for item in items:
        aid=item.get('asset_id');t=item.get('t_ms') if item.get('t_ms') is not None else item.get('start_ms',0);t=int(t or 0)
        grouped.setdefault(aid,set()).add(t)
    for aid,times in grouped.items():
        try:
            row=conn.execute('SELECT source_path FROM assets WHERE asset_id=?',(aid,)).fetchone()
            if row is None:continue
            path=Path(row['source_path']);identity=file_identity(path)
            missing=[t for t in times if _cached_feature((identity,t)) is None]
            if not missing:continue
            # Images use the same EXIF-aware single-frame reader; video targets
            # share one decode. Store tiny signatures, never full-size images.
            if path.suffix.lower() in {'.jpg','.jpeg','.png','.webp','.bmp','.heic'}:continue
            for requested,img,_ in iter_source_frames_at_times(path,missing):
                _save_feature((identity,requested),_image_features(img))
        except Exception:
            # Preserve per-frame error handling below; prefetch cannot turn a
            # missing/corrupt frame into a valid signature.
            continue


def _frame_features(conn, asset_id: str, t_ms: int):
    if conn is None:
        img=_extract_frame_pil(conn,asset_id,t_ms)
        return _image_features(img) if img is not None else None
    row=conn.execute('SELECT source_path FROM assets WHERE asset_id=?',(asset_id,)).fetchone()
    if row is None:return None
    from ..media.file_identity import file_identity
    identity=file_identity(Path(row['source_path']));key=(identity,t_ms)
    cached=_cached_feature(key)
    if cached is not None:return cached
    from ..media.sample import extract_frame_at
    img,_=extract_frame_at(Path(identity[0]),t_ms)
    features=_image_features(img) if img is not None else None
    _save_feature(key,features)
    return features


def _visual_dedup(items: list[dict], conn: sqlite3.Connection, threshold: int = 10) -> list[dict]:
    """按实际画面相似性（dHash 汉明距离）去重：保留排序靠前者，丢弃近似重复。"""
    _warm_frame_features(conn,items)
    kept: list[dict] = []
    exact_pixels = {}
    kept_hash: list[tuple[str, int, int, str | None]] = []
    for it in items:
        aid = it.get("asset_id")
        t = it.get("t_ms") if it.get("t_ms") is not None else it.get("start_ms")
        try:
            features = _frame_features(conn, aid, int(t or 0))
        except Exception:
            features = None
        if features is None:
            kept.append(it)
            continue
        pixel_key,h = features
        if pixel_key in exact_pixels:
            it['duplicate_of']=exact_pixels[pixel_key]
            continue  # identical decoded source pixels across files, without inferring cat identity
        duplicate = next(((cid,) for ka,kt,kh,cid in kept_hash if ka==aid and _hamming(h,kh)<=threshold),None)
        if duplicate is not None:
            it['duplicate_of']=duplicate[0]
            continue  # 与已保留近似，丢弃（保留排序靠前者）
        exact_pixels[pixel_key]=it.get('candidate_id')
        kept.append(it)
        kept_hash.append((aid, t, h, it.get('candidate_id')))
    return kept


def _dedup_photos_by_process(photos: list[dict], segments: list[dict], pad_ms: int = 2000) -> list[dict]:
    """照片按动作过程去重：同一自然段（完整动作）内只保留排名最佳的一张，避免重复交付相似动作。"""
    if not segments:
        return photos
    seg_ranges = [(s["start_ms"] - pad_ms, s["end_ms"] + pad_ms) for s in segments]
    used: set[int] = set()
    kept: list[dict] = []
    for p in photos:
        t = p.get("t_ms", 0)
        idx = next((i for i, (a, b) in enumerate(seg_ranges) if a <= t <= b), None)
        if idx is None:
            kept.append(p)  # 段外孤立瞬间保留
        elif idx not in used:
            used.add(idx)
            kept.append(p)  # 该过程第一次（排名最高）
    return kept


SAME_FRAME_EPSILON_MS = 0  # 同源帧仅按 t_ms 严格相等归并（相邻不同源帧不合并）


def _conservative_merge(group: list[dict]) -> dict:
    """同一源帧多次判断冲突时，质量字段取保守（最差），并保留来源冲突证据。"""
    if len(group) == 1:
        return dict(group[0])
    base = {**group[0], "observations":dict(group[0].get("observations") or {})}
    base["quality_floor_pass"] = all(f.get("quality_floor_pass", True) for f in group)
    base["promo_worthy"] = all(f.get("promo_worthy") is True for f in group)
    bad_clarity = {"soft", "blurry", "模糊"}
    bad_face = {"occluded", "cropped"}
    for f in group[1:]:
        o1 = (base.get("observations") or {}).get("clarity")
        o2 = (f.get("observations") or {}).get("clarity")
        if o2 in bad_clarity and o1 not in bad_clarity:
            base.setdefault("observations", {})["clarity"] = o2
        f1 = (base.get("observations") or {}).get("face_visible")
        f2 = (f.get("observations") or {}).get("face_visible")
        if f2 in bad_face and f1 not in bad_face:
            base.setdefault("observations", {})["face_visible"] = f2
    if any(f.get("posture_quality") == "别扭" for f in group):
        base["posture_quality"] = "别扭"
    harmonies = [f["overall_harmony"] for f in group if f.get("overall_harmony") is not None]
    if harmonies:
        base["overall_harmony"] = min(harmonies)
    base["aes_score"] = min(float(f.get("aes_score") if f.get("aes_score") is not None else 1.0) for f in group)
    # 保留冲突证据：不同判断的来源
    base["conflict_sources"] = sorted({(f.get("source") or f.get("source_type") or "") for f in group if f.get("source") or f.get("source_type")})
    base["conflict_runs"] = sorted({f.get("run_id") for f in group if f.get("run_id")})
    return base


def _asset_path(conn, asset_id) -> Path:
    row = conn.execute("SELECT source_path FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
    if row is None:
        raise security.SecurityError("asset_not_found", f"素材不存在：{asset_id}")
    from .assets import check_export_asset
    return Path(check_export_asset(conn,None,asset_id))


def _unique_path(out_dir: Path, name: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / name
    if not p.exists():
        return p
    stem, suffix = name.rsplit(".", 1)
    i = 1
    while True:
        q = out_dir / f"{stem}_{i}.{suffix}"
        if not q.exists():
            return q
        i += 1


def export_video_clip(conn: sqlite3.Connection, asset_id: str, start_ms: int, end_ms: int, out_dir: Path) -> Path:
    """从原片导出视频片段：按素材实际处理音轨（有音轨保留，无音轨不静默加）。"""
    import static_ffmpeg, subprocess
    from ..media.meta import read_video_metadata
    static_ffmpeg.add_paths()
    src = _asset_path(conn, asset_id)
    md = read_video_metadata(src)
    if start_ms < 0 or end_ms > md["duration_ms"]:
        raise security.SecurityError("invalid_boundary","导出区间超出源文件")
    if start_ms >= end_ms:
        raise security.SecurityError("export_failed", "起止时间非法")
    out = _unique_path(out_dir, f"{src.stem}_{start_ms}-{end_ms}ms.mp4")
    from ..media.sample import extract_frame_at
    first,actual_start = extract_frame_at(src,start_ms)
    if first is None:
        raise security.SecurityError("export_failed","无法定位视频首帧")
    start_seconds = actual_start / 1000.0
    temp = out.with_name("." + new_id("media") + ".mp4")
    cmd = ["ffmpeg", "-y", "-i", str(src), "-ss", str(start_seconds), "-t", str(end_ms / 1000.0 - start_seconds),
           "-map", "0:v:0", "-map_metadata", "-1", "-write_tmcd", "0",
           "-c:v", "libx264", "-preset", "fast", "-crf", "18"]
    if md["has_audio"]:
        cmd += ["-map", "0:a:0?", "-c:a", "aac"]
    else:
        cmd += ["-an"]
    cmd += ["-metadata", "comment="+json.dumps({"source_start_pts":first.info.get("source_pts"),"source_time_base":first.info.get("time_base"),"source_start_ms":actual_start,"requested_start_ms":start_ms,"requested_end_ms":end_ms})]
    cmd.append(str(temp))
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        validate_media(temp,"video")
        temp.replace(out)
    finally:
        temp.unlink(missing_ok=True)
    return out


def export_photo(conn: sqlite3.Connection, asset_id: str, t_ms: int, out_dir: Path, *, source_pts: int | None = None, time_base: str | None = None) -> Path:
    """从原片导出原分辨率照片（旋转转正，t_ms 对应候选真实帧，与预览/质检同帧）。"""
    from PIL import Image
    from ..media.sample import extract_frame_at
    src = _asset_path(conn, asset_id)
    img, _pts = extract_frame_at(src, int(t_ms or 0),source_pts=source_pts,time_base=time_base)
    if img is None:
        raise security.SecurityError("export_failed", f"无法在 {t_ms}ms 抽帧")
    out = _unique_path(out_dir, f"{src.stem}_{round(_pts)}ms.jpg")
    exif = Image.Exif()
    exif[270] = json.dumps({"source_pts":img.info.get("source_pts"),"time_base":img.info.get("time_base"),
                           "pts_ms":_pts,"requested_t_ms":t_ms})
    temp = out.with_name("." + new_id("media") + ".jpg")
    try:
        img.save(temp,"JPEG",quality=95,exif=exif)
        validate_media(temp,"photo")
        temp.replace(out)
    finally:
        temp.unlink(missing_ok=True)
    return out


def validate_media(path: Path, kind: str) -> None:
    if kind == "photo":
        from PIL import Image
        with Image.open(path) as img:
            img.verify()
    else:
        import av
        with av.open(str(path)) as container:
            count = sum(1 for _ in container.decode(video=0))
            expected = container.streams.video[0].frames
            if count == 0 or (expected and count < expected):
                raise security.SecurityError("export_corrupt","视频解码不完整")


def assert_export_allowed(conn, task_id):
    """收口：v3 实验任务未补齐人工审核一致性前，禁止正式导出/交付。"""
    from .quality_policy import is_v3
    if is_v3(conn, task_id):
        raise security.SecurityError("v3_experimental_export_blocked",
                                     "v3 实验任务尚未补齐人工审核一致性，禁止正式导出/交付")


def reviewed_items(conn,task_id):
    assert_export_allowed(conn, task_id)
    r = get_task_results(conn,task_id)
    items = [{**p,"kind":"photo"} for p in r["final_recommended_photos"]] + [{**s,"kind":"video"} for s in r["final_video_segments"]]
    from .analysis import PROMPT_VERSION, SELECTION_RULE_VERSION
    from .analysis_job import _check_version
    for item in items:
        row=conn.execute("SELECT fingerprint_at_lock FROM task_assets WHERE task_id=? AND asset_id=?",(task_id,item["asset_id"])).fetchone()
        item["source_fingerprint"]=row[0]
        item.setdefault("analysis_version",f"pv{PROMPT_VERSION}-rv{SELECTION_RULE_VERSION}")
        item["quality_version"]=_check_version(conn,task_id,None,item.get("execution_id")) if item["kind"]=="photo" else item.get("judgment_id")
    return items


def validate_reviewed_items(conn,task_id,items):
    """Only server-resolved kept candidates and their saved effective ranges may export."""
    current = {i["candidate_id"]:i for i in reviewed_items(conn,task_id)}
    out = []
    for item in items:
        c = current.get(item.get("candidate_id"))
        if c is None or any(item.get(k) != c.get(k) for k in ("kind","asset_id","t_ms","start_ms","end_ms")):
            raise security.SecurityError("stale_review","候选或边界不是当前已保留审核集合，请重新加载")
        out.append(c)
    if {c["candidate_id"] for c in out} != set(current):
        raise security.SecurityError("incomplete_review_set","批量交付须使用完整的服务端保留集合")
    return out


def photo_identity(path: Path) -> dict:
    from PIL import Image
    with Image.open(path) as img:
        return json.loads(img.getexif().get(270,"{}"))


def video_identity(path: Path) -> dict:
    import av
    with av.open(str(path)) as container:
        return json.loads(container.metadata.get("comment","{}"))

"""全片粗筛 + 小窗口细查 + 待细查候选加密：复用 pipeline（任务/指纹/缓存/费用/恢复）。"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .. import security
from . import analysis, pipeline, vlm
from .assets import new_id


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

COARSE_FPS = 0.5
COARSE_MAX_FRAMES = 120
WINDOW_MS = 3000          # 细查小窗口，保证真实 2fps 密度
WINDOW_OVERLAP_MS = 500
FINE_FPS = 2.0
FINE_MAX_FRAMES = 8
DENSIFY_FPS = 4.0         # 待细查候选加密
DENSIFY_MAX_FRAMES = 12
DENSIFY_PAD_MS = 1500
CANDIDATE_AES = 0.55
ACTION_BONUS = {"抬爪", "歪头"}


def coarse_windows(duration_ms, sampling, bounded):
    """Bound each model response while retaining the configured whole-file grid.

    Historical task snapshots keep their original call shape and cache keys.
    New calls use ordinary durable analysis runs, so completed batches survive
    budget interruption without a new partially settled aggregation protocol.
    """
    count=max(1,min(sampling['max_frames'],math.ceil(duration_ms*sampling['frames_per_sec']/1000)))
    if not bounded or count<=16:return [(0,duration_ms,sampling['max_frames'])]
    return [(duration_ms*i//count,duration_ms*min(i+16,count)//count,min(16,count-i))
            for i in range(0,count,16)]


def _coarse_analysis(conn,task_id,asset_id,transport,duration_ms,sampling,execution_id,on_batch=None):
    snapshot=json.loads(conn.execute('SELECT snapshot_ref_json FROM tasks WHERE task_id=?',(task_id,)).fetchone()[0] or '{}')
    windows=coarse_windows(duration_ms,sampling,snapshot.get('coarse_batch_version',0)>=1)
    annotations=[];batches=[]
    for index,(start,end,limit) in enumerate(windows):
        if on_batch:on_batch(f'粗筛画面核对 · 第{index+1}/{len(windows)}批')
        result=_require(pipeline.analyze_asset(conn,task_id,asset_id,transport,start,end,
                         sampling={**sampling,'max_frames':limit},execution_id=execution_id))
        annotations.extend(result['annotations'])
        batches.append({'requested':[start,end],'max_frames':limit,'run_id':result['run_id'],
                        'reused':result['reused'],'sampled_times_ms':[f['t_ms'] for f in result['annotations']]})
        old=conn.execute('SELECT coverage_plan_json FROM executions WHERE execution_id=?',(execution_id,)).fetchone()
        plan=json.loads(old[0] or '{}') if old else {}
        plan.setdefault('planned',[]);plan.setdefault('checked',[]);plan.setdefault('version',1)
        plan['coarse']={'requested':[0,duration_ms],'planned_batches':len(windows),'batches':batches,
                        'sampled_times_ms':[f['t_ms'] for f in annotations]}
        conn.execute('UPDATE executions SET coverage_plan_json=? WHERE execution_id=?',(json.dumps(plan,ensure_ascii=False),execution_id))
    return {'annotations':sorted(annotations,key=lambda f:f['t_ms']),'batches':batches}


def _sampling_from_task(conn: sqlite3.Connection, task_id: str) -> dict:
    """读取任务锁定模型配置的采样参数（sampling 字段实际驱动执行）。"""
    row = conn.execute("SELECT model_config_ref_json,snapshot_ref_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if not row or not row["model_config_ref_json"]:
        return {}
    from . import model_config
    ref = json.loads(row["model_config_ref_json"])
    try:
        cfg = model_config.get_config(conn, ref.get("model_config_id"), ref.get("version"))
        sampling=dict(cfg.get("sampling", {}) or {})
        snapshot=json.loads(row['snapshot_ref_json'] or '{}')
        if snapshot.get('selection_pipeline_version',2 if snapshot.get('quality_policy_version',1)>=2 else 1)<2:
            sampling['photo_peak_max_windows']=0
        if not snapshot.get('coverage_plan_version'):
            # A legacy task must not silently gain paid audit windows during recovery.
            sampling.setdefault('uncertain_max_windows',0)
            sampling.setdefault('discard_audit_max_windows',0)
        return sampling
    except Exception:
        return {}


def effective_sampling(s: dict) -> dict:
    coarse = {"max_frames":s.get("coarse_max_frames") or COARSE_MAX_FRAMES,
              "frames_per_sec":s.get("coarse_fps") or COARSE_FPS,
              "resize_edge":s.get("coarse_resize_edge") or 600}
    fine = s.get("fine_fps") or s.get("frames_per_sec") or max(2.0,coarse["frames_per_sec"]*4)
    return {"coarse":coarse,"fine_fps":fine,
            "fine_max_frames":s.get("fine_max_frames") or s.get("max_frames") or FINE_MAX_FRAMES,
            "densify_fps":s.get("densify_fps") or max(4.0,fine*2),
            "densify_max_frames":s.get("densify_max_frames") or s.get("max_frames") or DENSIFY_MAX_FRAMES,
            "boundary_max_windows":s.get("boundary_max_windows",8),
            "uncertain_max_windows":s.get("uncertain_max_windows",4),
            "discard_audit_max_windows":s.get("discard_audit_max_windows",3),
            "photo_peak_max_windows":s.get('photo_peak_max_windows',4),
            "photo_peak_fps":s.get('photo_peak_fps',30),
            "photo_peak_max_frames":s.get('photo_peak_max_frames',16),
            "resize_edge":s.get("resize_edge") or coarse["resize_edge"]}


def _effective_coarse_sampling(conn: sqlite3.Connection, task_id: str) -> dict:
    return effective_sampling(_sampling_from_task(conn,task_id))["coarse"]


def _get_pref(conn: sqlite3.Connection, pref_ref: dict | None) -> dict:
    if not pref_ref:
        return {"quality_floor": {"required": ["基本可见", "无严重模糊/遮挡"]}}
    from .preferences import get_preference
    return get_preference(conn, pref_ref.get("preference_id"), pref_ref.get("version"))


def _windows(interval: tuple[int, int], window_ms: int = WINDOW_MS, overlap: int = WINDOW_OVERLAP_MS) -> list[tuple[int, int]]:
    s, e = interval
    out = []
    cur = s
    while cur < e:
        wend = min(cur + window_ms, e)
        out.append((cur, wend))
        if wend >= e:
            break
        cur = wend - overlap
    return out


def _candidates(annotations: list[dict]) -> list[dict]:
    cands = [a for a in annotations
             if float(a.get("aes_score", 0.0)) >= CANDIDATE_AES
             or bool(set(a.get("preference_hits", []) or []) & ACTION_BONUS)]
    # 优先眼神、脸部、整体表现好的候选
    cands.sort(key=lambda a: (-float(a.get("aes_score", 0.0)),
                              -float((a.get("scores", {}) or {}).get("eye", 0.0) or 0.0),
                              -float((a.get("scores", {}) or {}).get("face", 0.0) or 0.0)))
    return cands


def _intervals_from(cands: list[dict], duration_ms: int, pad: int = 1500, gap: int = 3000) -> list[tuple[int, int]]:
    if not cands:
        return []
    ts = sorted(a["t_ms"] for a in cands)
    ivs = [[max(0, ts[0] - pad), min(duration_ms, ts[0] + pad)]]
    for t in ts[1:]:
        s = max(0, t - pad); e = min(duration_ms, t + pad)
        if s <= ivs[-1][1] + gap:
            ivs[-1][1] = max(ivs[-1][1], e)
        else:
            ivs.append([s, e])
    return [tuple(x) for x in ivs]


def _require(r: dict) -> dict:
    """analyze_asset 结果必须包含可读 annotations；中断/在途/异常返回结构化错误而非裸 KeyError。"""
    if not r.get("annotations"):
        raise security.SecurityError("analysis_not_ready", r.get("hint") or "存在未完成/中断的运行记录，请先核查费用后重试")
    return r


def full_clip_analyze(conn: sqlite3.Connection, task_id: str, asset_id: str, transport: Callable,
                     on_progress: Callable[[str], None] | None = None, *, publish: bool = True, execution_id: str | None = None) -> dict:
    def _p(stage: str) -> None:
        if on_progress:
            on_progress(stage)
    execution_id = execution_id or new_id("exec")
    task = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if task is None:
        raise security.SecurityError("task_not_found", f"任务不存在：{task_id}")
    asset = conn.execute("SELECT media_type FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
    if asset is None:
        raise security.SecurityError("asset_not_found", f"素材不存在：{asset_id}")
    from .quality_policy import is_v3
    if is_v3(conn, task_id):
        raise security.SecurityError("v3_task_requires_selection_v2", "v3 任务必须走 selection_v2 新路径，禁止进入旧 fullscan")
    if asset["media_type"] == "image":
        return _analyze_image(conn, task_id, asset_id, transport, execution_id, task, publish=publish)
    pref = _get_pref(conn, json.loads(task["preference_ref_json"]) if task["preference_ref_json"] else None)
    action_bonus = set(pref.get("visual_rules", {}).get("action_bonus", []) or [])

    def _mark_execution(status: str) -> None:
        conn.execute("UPDATE executions SET status=?, updated_at=? WHERE execution_id=?",
                     (status, _now(), execution_id))
        conn.commit()

    coarse_sampling = _effective_coarse_sampling(conn, task_id)
    # 细查/加密按粗筛 fps 派生，保证密度合理；resize 与粗筛一致
    effective = effective_sampling(_sampling_from_task(conn,task_id))
    from .quality_policy import selection_version
    if selection_version(conn,task_id)<2:
        effective['photo_peak_max_windows']=0
    fine_fps, densify_fps = effective["fine_fps"], effective["densify_fps"]
    resize_edge = effective["resize_edge"]

    try:
        # 1) 全片粗筛（复用 pipeline，含缓存/指纹/费用；采样由任务锁定配置驱动）
        coarse = _coarse_analysis(conn,task_id,asset_id,transport,_duration(conn,asset_id),coarse_sampling,execution_id,_p)
        cands = _candidates(coarse["annotations"])
        intervals = _intervals_from(cands, _duration(conn, asset_id))
        from .coverage_plan import audit_windows
        audits=audit_windows(coarse['annotations'],intervals,_duration(conn,asset_id),
                             effective['uncertain_max_windows'],effective['discard_audit_max_windows'])
        plan={'version':1,'coarse':{'requested':[0,_duration(conn,asset_id)],'sampled_times_ms':[f['t_ms'] for f in coarse['annotations']],'batches':coarse['batches'],'planned_batches':len(coarse['batches'])},
              'planned':[{'start_ms':a,'end_ms':b,'purpose':'candidate'} for iv in intervals for a,b in _windows(iv)]+audits,'checked':[]}
        def save_plan():
            conn.execute('UPDATE executions SET coverage_plan_json=? WHERE execution_id=?',(json.dumps(plan,ensure_ascii=False),execution_id))
        prior=conn.execute('SELECT coverage_plan_json FROM executions WHERE execution_id=?',(execution_id,)).fetchone()
        prior=json.loads(prior[0] or '{}') if prior else {}
        if 'photo_peak_plan' in prior:plan['photo_peak_plan']=prior['photo_peak_plan']
        save_plan()
        _p("细查")

        # 2) 小窗口细查
        fine_segments: list[dict] = []
        photos: dict[int, dict] = {}
        pending: dict[int, dict] = {}
        windows = []
        for entry in plan['planned']:
                w=(entry['start_ms'],entry['end_ms'])
                _p({'candidate':'细查','uncertain':'核对不确定画面','discard_audit':'抽查未入选区间'}[entry['purpose']])
                windows.append(w)
                r = _require(pipeline.analyze_asset(conn, task_id, asset_id, transport, w[0], w[1],
                                           sampling={"max_frames": effective["fine_max_frames"], "frames_per_sec": fine_fps, "resize_edge": resize_edge},
                                           execution_id=execution_id))
                plan['checked'].append({**entry,'run_id':r['run_id'],'sampled_times_ms':[f['t_ms'] for f in r['annotations']]})
                save_plan()
                for s in r["selection"]["video_segments"]:
                    fine_segments.append(s)
                for p in r["selection"]["pending_candidates"]:
                    pending[p["frames"][0]["t_ms"]] = p
                for ph in r["selection"]["photo_frames"]:
                    photos[ph["t_ms"]] = ph

        # 3) 待细查候选加密（±1.5s，更高帧率，确认非零区间）
        _p("加密")
        for t_ms in sorted(pending.keys()):
            s = max(0, t_ms - DENSIFY_PAD_MS); e = min(_duration(conn, asset_id), t_ms + DENSIFY_PAD_MS)
            r = _require(pipeline.analyze_asset(conn, task_id, asset_id, transport, s, e,
                                       sampling={"max_frames": effective["densify_max_frames"], "frames_per_sec": densify_fps, "resize_edge": resize_edge},
                                       execution_id=execution_id))
            for seg in r["selection"]["video_segments"]:
                fine_segments.append(seg)
            for ph in r["selection"]["photo_frames"]:
                photos[ph["t_ms"]] = ph
            # 加密后若仍是单帧待细查，保留；否则已在 video_segments/photos
            for p in r["selection"]["pending_candidates"]:
                pending[p["frames"][0]["t_ms"]] = p
    except Exception:
        _mark_execution("failed")
        raise

    # 合并视频段（重叠合并，保留不同位置）
    fine_segments.sort(key=lambda s: s["start_ms"])
    merged: list[dict] = []
    for s in fine_segments:
        if merged and s["start_ms"] <= merged[-1]["end_ms"]:
            merged[-1]["end_ms"] = max(merged[-1]["end_ms"], s["end_ms"])
            merged[-1]["frames"].extend(s["frames"])
            merged[-1]["source"] = "auto"
        else:
            merged.append(dict(s)); merged[-1]["source"] = "auto"

    photo_list = list(photos.values())
    # 相似候选择优去重：同一时间窗内只保留最佳，减少相似/次优
    photo_list = analysis.dedup_similar_photos(photo_list, action_bonus, window_ms=1500)
    photo_list.sort(key=lambda f: analysis._photo_rank_key(f, action_bonus))
    # 分层：精选推荐=promo_worthy 显式为 true；最佳候选=推荐首位（无精选则为空，不强行凑）
    recommended = [f for f in photo_list if f.get("promo_worthy") is True]
    top_photo = recommended[0] if recommended else None
    top_photo_source = "rule"
    from . import feedback
    prefs = feedback.get_preferences(conn, task_id, asset_id)
    if prefs:
        chosen = feedback.resolve_top_photo(recommended, prefs)
        if chosen is not None:
            top_photo = chosen; top_photo_source = "user"

    _p("完成")
    _mark_execution("complete" if publish else "processing")
    return {
        "execution_id": execution_id,
        "sampling": {"coarse": coarse_sampling, "fine_fps": fine_fps, "densify_fps": densify_fps, "resize_edge": resize_edge},
        "coarse": {"frames_analyzed": len(coarse["annotations"]),
                   "candidate_t_ms": [a["t_ms"] for a in cands],
                   "intervals": [{"start_ms": a, "end_ms": b} for a, b in intervals],
                   "reference_coverage": None},
        "coverage_plan": plan,
        "video_segments": merged,
        "pending_candidates": list(pending.values()),
        "photo_frames": photo_list,
        "recommended_photos": recommended,
        "top_photo": top_photo,
        "top_photo_source": top_photo_source,
    }


def refine_photo_peaks(conn,task_id,asset_id,transport,execution_id):
    """Freeze bounded neighbourhoods AFTER initial QC; never spend peak budget on known rejected seeds.
    Frozen per execution, so retry/restart cannot walk to new neighbourhoods and silently add calls.
    """
    from .quality_policy import selection_version
    if selection_version(conn,task_id)<2:return 0
    if conn.execute('SELECT media_type FROM assets WHERE asset_id=?',(asset_id,)).fetchone()[0]=='image':return 0
    from .results import get_task_results
    from .batch_identity import frames as member_frames
    from .coverage_plan import photo_peak_windows
    row=conn.execute('SELECT coverage_plan_json FROM executions WHERE execution_id=?',(execution_id,)).fetchone()
    plan=json.loads(row[0] or '{}') if row else {}
    effective=effective_sampling(_sampling_from_task(conn,task_id))
    if 'photo_peak_plan' not in plan:
        view=get_task_results(conn,task_id,execution_overrides={asset_id:execution_id})
        seeds=[p for p in view['recommended_photos'] if p['asset_id']==asset_id]
        processes=analysis.merge_natural_segments(member_frames(conn,task_id,asset_id,execution_id))
        plan['photo_peak_plan']=photo_peak_windows(seeds,_duration(conn,asset_id),effective['photo_peak_max_windows'],processes)
        conn.execute('UPDATE executions SET coverage_plan_json=? WHERE execution_id=?',(json.dumps(plan),execution_id))
    for entry in plan['photo_peak_plan']:
        if entry not in plan['planned']:plan['planned'].append(entry)
        conn.execute('UPDATE executions SET coverage_plan_json=? WHERE execution_id=?',(json.dumps(plan),execution_id))
        r=_require(pipeline.analyze_asset(conn,task_id,asset_id,transport,entry['start_ms'],entry['end_ms'],
                   sampling={'max_frames':effective['photo_peak_max_frames'],'frames_per_sec':effective['photo_peak_fps'],'resize_edge':effective['resize_edge'],'source_frame_dedup':True},execution_id=execution_id))
        if not any(x.get('purpose')=='photo_peak' and x['start_ms']==entry['start_ms'] and x['end_ms']==entry['end_ms'] for x in plan['checked']):
            plan['checked'].append({**entry,'run_id':r['run_id'],'sampled_times_ms':[f['t_ms'] for f in r['annotations']]})
        conn.execute('UPDATE executions SET coverage_plan_json=? WHERE execution_id=?',(json.dumps(plan),execution_id))
    return len(plan['photo_peak_plan'])


def _analyze_image(conn: sqlite3.Connection, task_id: str, asset_id: str, transport, execution_id: str, task: dict, publish: bool = True) -> dict:
    """图片素材分析路径：单帧看图 → 照片候选（无视频段）。"""
    from . import analysis
    coarse = _require(pipeline.analyze_asset(conn, task_id, asset_id, transport, 0, 1,
                                            sampling={"max_frames": 1, "frames_per_sec": 1.0, "resize_edge": effective_sampling(_sampling_from_task(conn,task_id))["resize_edge"]},
                                            execution_id=execution_id))
    image_edge = effective_sampling(_sampling_from_task(conn,task_id))["resize_edge"]
    ann = coarse["annotations"]
    photo_list = [dict(a) for a in ann if a.get("promo_worthy") is True] or [dict(a) for a in ann]
    for a in photo_list:
        a["source"] = coarse.get("source", "mock")
    recommended = [a for a in photo_list if a.get("promo_worthy") is True]
    top_photo = recommended[0] if recommended else None
    conn.execute("UPDATE executions SET status=?, updated_at=? WHERE execution_id=?", ("complete" if publish else "processing",_now(), execution_id))
    conn.commit()
    return {
        "execution_id": execution_id,
        "sampling": {"coarse": {"max_frames": 1, "frames_per_sec": 1.0, "resize_edge": image_edge}},
        "coarse": {"frames_analyzed": 1, "candidate_t_ms": [0], "intervals": [], "reference_coverage": None},
        "video_segments": [],
        "pending_candidates": [],
        "photo_frames": photo_list,
        "recommended_photos": recommended,
        "top_photo": top_photo,
        "top_photo_source": "rule",
    }


def _duration(conn: sqlite3.Connection, asset_id: str) -> int:
    row = conn.execute("SELECT source_path, media_type FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
    if row["media_type"] == "image":
        return 1
    from ..media.meta import read_video_metadata
    return read_video_metadata(Path(row["source_path"]))["duration_ms"]

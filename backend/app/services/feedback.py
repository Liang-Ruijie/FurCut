"""用户审美反馈：记录相对偏好/评分，保留原始模型评分；反馈限定任务+素材+适用范围。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .. import security
from .assets import new_id


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_relative_preference(conn: sqlite3.Connection, task_id: str, asset_id: str, run_id: str | None,
                               preferred_t_ms: int, over_t_ms: int, note: str = "") -> dict:
    """记录相对偏好：preferred 优于 over（用户明确判定）。不改原始模型评分。"""
    fid = new_id("fb")
    payload = {"preferred_t_ms": preferred_t_ms, "over_t_ms": over_t_ms, "note": note}
    conn.execute(
        "INSERT INTO feedback_events(feedback_id, task_id, asset_id, run_id, kind, payload_json, created_at) VALUES(?,?,?,?,?,?,?)",
        (fid, task_id, asset_id, run_id, "relative_preference", json.dumps(payload, ensure_ascii=False), _now()),
    )
    conn.commit()
    return {"feedback_id": fid, "kind": "relative_preference", **payload}


def record_video_rating(conn: sqlite3.Connection, task_id: str, asset_id: str, run_id: str | None,
                        rating: str, note: str = "") -> dict:
    fid = new_id("fb")
    payload = {"rating": rating, "note": note}
    conn.execute(
        "INSERT INTO feedback_events(feedback_id, task_id, asset_id, run_id, kind, payload_json, created_at) VALUES(?,?,?,?,?,?,?)",
        (fid, task_id, asset_id, run_id, "video_rating", json.dumps(payload, ensure_ascii=False), _now()),
    )
    conn.commit()
    return {"feedback_id": fid, "kind": "video_rating", **payload}


def record_feedback(conn: sqlite3.Connection, task_id: str, asset_id: str | None, run_id: str | None,
                    kind: str, payload: dict) -> dict:
    """通用反馈：keep/reject/trim/boundary_adjust 等。校验边界合法（start<end、非负）。"""
    if kind == "boundary_adjust":
        s = payload.get("start_ms"); e = payload.get("end_ms")
        if s is None or e is None or isinstance(s, bool) or isinstance(e, bool):
            raise security.SecurityError("invalid_boundary", "边界调整缺少 start_ms/end_ms")
        try:
            if not isinstance(s,int) or not isinstance(e,int):
                raise ValueError("整数毫秒 required")
            s, e = int(s), int(e)
        except (TypeError, ValueError):
            raise security.SecurityError("invalid_boundary", "边界必须是整数毫秒")
        if s < 0 or e < 0 or s >= e:
            raise security.SecurityError("invalid_boundary", f"边界非法：start={s} end={e}（须 0≤start<end）")
    if kind in ("keep", "reject", "trim", "boundary_adjust", "clear_decision", "relative_preference", "candidate_comparison") and not payload.get("candidate_id"):
        raise security.SecurityError("invalid_candidate", "审核反馈缺少 candidate_id")
    if kind in ("keep","reject","trim","boundary_adjust","clear_decision","relative_preference","candidate_comparison"):
        task = conn.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if task and task["status"] == "completed":
            raise security.SecurityError("invalid_transition","审核已确认，请先点击继续修改审核；历史交付包将保留")
        from . import results, assets
        r = results.get_task_results(conn,task_id)
        candidates = r["photos"] + r["video_candidates"] + r["video_fragments"]
        candidate = next((c for c in candidates if c["candidate_id"] == payload["candidate_id"]),None)
        if candidate is None or asset_id != candidate["asset_id"] or payload.get("asset_id",asset_id) != asset_id:
            raise security.SecurityError("invalid_candidate","候选不存在、已换批次或素材归属不符")
        if run_id is not None and run_id != candidate.get("run_id"):
            raise security.SecurityError("invalid_candidate","run_id 不属于当前候选")
        if kind == 'relative_preference':
            ts={p.get('t_ms') for p in r['photos'] if p['asset_id']==asset_id}
            preferred,over=payload.get('preferred_t_ms'),payload.get('over_t_ms')
            if any(isinstance(t,bool) or not isinstance(t,int) or t not in ts for t in (preferred,over)) or preferred==over or preferred!=candidate.get('t_ms'):
                raise security.SecurityError('invalid_preference','相对偏好必须比较同一素材当前批次中的两张真实候选')
        if kind == 'candidate_comparison':
            other=next((p for p in r['photos'] if p['candidate_id']==payload.get('over_candidate_id')),None)
            if 't_ms' not in candidate or other is None or other['candidate_id']==candidate['candidate_id']:
                raise security.SecurityError('invalid_preference','须比较本任务当前批次的两张不同真实照片')
            assets.check_export_asset(conn,task_id,other['asset_id'])
            payload={**payload,'preferred':{k:candidate.get(k) for k in ('candidate_id','asset_id','t_ms','source_pts','time_base','execution_id')},
                     'over':{k:other.get(k) for k in ('candidate_id','asset_id','t_ms','source_pts','time_base','execution_id')}}
        src = assets.check_export_asset(conn,task_id,asset_id)
        if kind == "boundary_adjust":
            if "t_ms" in candidate:
                raise security.SecurityError("invalid_boundary","照片不支持视频边界")
            from ..media.meta import read_video_metadata
            from pathlib import Path
            if e > read_video_metadata(Path(src))["duration_ms"]:
                raise security.SecurityError("invalid_boundary","边界超过原片时长")
            payload = {**payload,"start_ms":s,"end_ms":e}
        canonical = {k:candidate.get(k) for k in ("asset_id","run_id","source_type","from","t_ms","execution_id")}
        canonical["kind"] = kind
        for key,value in canonical.items():
            if key in payload and payload[key] != value:
                raise security.SecurityError("invalid_candidate",f"审核来源字段 {key} 不属于当前候选")
        if kind != "boundary_adjust":
            for key in ("start_ms","end_ms"):
                if key in payload and payload[key] != candidate.get(key):
                    raise security.SecurityError("invalid_candidate","候选区间不一致，请使用边界调整")
        payload = {**payload,**canonical,"candidate_identity":candidate["candidate_id"]}
    fid = new_id("fb")
    conn.execute(
        "INSERT INTO feedback_events(feedback_id, task_id, asset_id, run_id, kind, payload_json, created_at) VALUES(?,?,?,?,?,?,?)",
        (fid, task_id, asset_id, run_id, kind, json.dumps(payload, ensure_ascii=False), _now()),
    )
    conn.commit()
    return {"feedback_id": fid, "kind": kind, **payload}


def list_feedback(conn: sqlite3.Connection, task_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT kind, payload_json, asset_id, run_id, created_at FROM feedback_events WHERE task_id=? ORDER BY created_at",
        (task_id,),
    ).fetchall()
    out = []
    for r in rows:
        p = json.loads(r["payload_json"])
        out.append({"kind": r["kind"], "asset_id": r["asset_id"], "run_id": r["run_id"],
                    "created_at": r["created_at"], **p})
    return out


def get_preferences(conn: sqlite3.Connection, task_id: str, asset_id: str | None = None) -> list[dict]:
    """相对偏好必须限定任务；给 asset_id 时只取该素材的反馈，不给时只取「任务级」反馈（不混入其他素材）。"""
    if asset_id:
        rows = conn.execute(
            "SELECT payload_json FROM feedback_events WHERE task_id=? AND kind='relative_preference' "
            "AND (asset_id=? OR asset_id IS NULL OR asset_id='') ORDER BY created_at",
            (task_id, asset_id),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT payload_json FROM feedback_events WHERE task_id=? AND kind='relative_preference' "
            "AND (asset_id IS NULL OR asset_id='') ORDER BY created_at",
            (task_id,),
        ).fetchall()
    return [json.loads(r["payload_json"]) for r in rows]


def resolve_top_photo(photo_frames: list[dict], preferences: list[dict]) -> dict | None:
    """用户明确选定的照片：若反馈的 preferred 与 over 都在候选内，直接采用 preferred（不受分差限制）。"""
    if not photo_frames or not preferences:
        return None
    ts = {f["t_ms"] for f in photo_frames}
    for pref in preferences:
        p = pref.get("preferred_t_ms")
        o = pref.get("over_t_ms")
        if p is not None and o is not None and p in ts and o in ts:
            return next(f for f in photo_frames if f["t_ms"] == p)
    return None

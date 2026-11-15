"""分析流水线编排：归属校验 + 缓存复用（防重复收费）+ 采样 + 护栏化理解 + 选片 + 持久化。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .. import security
from ..db import atomic
from . import analysis, feedback, vlm
from .assets import new_id

ANALYZABLE_STATUSES = {"assets_locked", "analyzing", "selecting", "exporting"}
STALE_SECONDS = 600  # running 记录超过此时长视为「中断待核查」


def _record_member(conn: sqlite3.Connection, execution_id: str | None, run_id: str, task_id: str, asset_id: str) -> None:
    """记录某 run 属于某执行批次（缓存复用与重跑都绑定，聚合按执行成员而非 run 自身 execution_id）。"""
    if not execution_id:
        return
    with atomic(conn):
        run = conn.execute("SELECT task_id,asset_id FROM analysis_runs WHERE run_id=?",(run_id,)).fetchone()
        if not run or run["task_id"] != task_id or run["asset_id"] != asset_id:
            raise security.SecurityError("invalid_batch_member","运行记录与批次归属不一致")
        conn.execute("INSERT OR IGNORE INTO executions(execution_id,task_id,asset_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                     (execution_id,task_id,asset_id,"running",_now(),_now()))
        execution = conn.execute("SELECT task_id,asset_id FROM executions WHERE execution_id=?",(execution_id,)).fetchone()
        if execution["task_id"] != task_id or execution["asset_id"] != asset_id:
            raise security.SecurityError("invalid_batch_member","执行批次已属于其他任务或素材")
        conn.execute("INSERT OR IGNORE INTO batch_members(execution_id,run_id) VALUES(?,?)",(execution_id,run_id))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_stale(created_at: str | None, stale_seconds: int = STALE_SECONDS) -> bool:
    if not created_at:
        return True
    try:
        started = datetime.fromisoformat(created_at)
    except ValueError:
        return True
    return datetime.now(timezone.utc) - started > timedelta(seconds=stale_seconds)


def _get_run(conn: sqlite3.Connection, task_id: str, asset_id: str, cache_key: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM analysis_runs WHERE task_id=? AND asset_id=? AND cache_key=?",
        (task_id, asset_id, cache_key),
    ).fetchone()
    return dict(row) if row else None


def _assert_task_owns_asset(conn: sqlite3.Connection, task: dict, asset: dict) -> str:
    """校验素材归属、任务状态与锁定指纹。不属于本任务/素材变更/状态不可分析都拒绝。"""
    if task["status"] == "pending_reconciliation":
        raise security.SecurityError("pending_reconciliation","真实请求待核账，禁止重发")
    if task["status"] not in ANALYZABLE_STATUSES:
        raise security.SecurityError("invalid_transition", f"任务状态 {task['status']} 不可分析")
    row = conn.execute(
        "SELECT fingerprint_at_lock FROM task_assets WHERE task_id=? AND asset_id=?",
        (task["task_id"], asset["asset_id"]),
    ).fetchone()
    if row is None:
        raise security.SecurityError("asset_not_in_task", f"素材 {asset['asset_id']} 不属于任务 {task['task_id']}")
    from ..security import sha256_file
    current = sha256_file(Path(asset["source_path"]))
    if current != row["fingerprint_at_lock"]:
        raise security.SecurityError("asset_mismatch", f"素材 {asset['asset_id']} 内容已改变，锁定指纹不一致")
    return current


def _insert_running(conn: sqlite3.Connection, run_id: str, task_id: str, asset_id: str, cache_key: str,
                    source: str, pref_ref: dict | None, cfg_ref: dict | None, sampling: dict,
                    execution_id: str | None = None) -> None:
    conn.execute(
        """INSERT INTO analysis_runs(run_id, task_id, asset_id, cache_key, execution_id, status, source,
             preference_ref_json, model_config_ref_json, sampling_json, annotations_json,
             selections_json, coverage_json, cost_id, created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (run_id, task_id, asset_id, cache_key, execution_id, "running", source,
         json.dumps(pref_ref, ensure_ascii=False) if pref_ref else None,
         json.dumps(cfg_ref, ensure_ascii=False) if cfg_ref else None,
         json.dumps(sampling, ensure_ascii=False), None, None, None, None, _now()),
    )


def analyze_asset(conn: sqlite3.Connection, task_id: str, asset_id: str, transport: Callable,
                  segment_start_ms: int, segment_end_ms: int, sampling: dict | None = None,
                  prompt_version: int = analysis.PROMPT_VERSION, execution_id: str | None = None) -> dict:
    """分析单素材的给定区间：缓存命中复用；失败可重试；运行中区分「执行中/中断待核查」；并发只有一个执行者。"""
    sampling = sampling or {"max_frames": 6, "frames_per_sec": 3.0, "resize_edge": 600}
    task = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if task is None:
        raise security.SecurityError("task_not_found", f"任务不存在：{task_id}")
    asset = conn.execute("SELECT * FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
    if asset is None:
        raise security.SecurityError("asset_not_found", f"素材未登记：{asset_id}")

    _assert_task_owns_asset(conn, dict(task), dict(asset))

    pref_ref = json.loads(task["preference_ref_json"]) if task["preference_ref_json"] else None
    cfg_ref = json.loads(task["model_config_ref_json"]) if task["model_config_ref_json"] else None
    source = "mock" if isinstance(transport, vlm.MockVlmTransport) else "real"
    cache_key = analysis.analysis_cache_key(asset["fingerprint"], cfg_ref, pref_ref, sampling,
                                            prompt_version, segment_start_ms, segment_end_ms, source)

    cached = _get_run(conn, task_id, asset_id, cache_key)
    if cached is not None:
        if cached["status"] == "pending_reconciliation":
            raise security.SecurityError("pending_reconciliation", "真实请求待核账，不可自动重发")
        if cached["status"] == "complete":
            _record_member(conn, execution_id, cached["run_id"], task_id, asset_id)
            return {"reused": True, "run_id": cached["run_id"],
                    "annotations": json.loads(cached["annotations_json"]),
                    "selection": json.loads(cached["selections_json"]),
                    "coverage": json.loads(cached["coverage_json"]), "source": cached["source"]}
        if cached["status"] == "running":
            if _is_stale(cached["created_at"]):
                # 进程中断：已完成成员恢复不丢；在途请求不盲目重发。
                # mock（离线确定性，无真实费用）→ 标失败后直接重试，使恢复闭环完成；
                # real（可能已付费）→ 标失败后返回结构化提示，待核对费用后由调用方决定重试。
                conn.execute("UPDATE analysis_runs SET status=?, coverage_json=? WHERE run_id=?",
                             ("failed" if source == "mock" else "pending_reconciliation", json.dumps({"error": "interrupted_stale", "retry": source == "mock"}, ensure_ascii=False), cached["run_id"]))
                conn.commit()
                if source == "mock":
                    run_id = cached["run_id"]
                    with atomic(conn):
                        conn.execute("UPDATE analysis_runs SET status='running', execution_id=?, created_at=? WHERE run_id=?",
                                     (execution_id, _now(), run_id))
                else:
                    return {"reused": False, "interrupted": True, "run_id": cached["run_id"],
                            "hint": "真实在途请求已隔离，须先核账后显式处置，不可自动重试"}
            else:
                return {"reused": False, "in_progress": True, "run_id": cached["run_id"]}
        else:
            # failed → 可重试：复用同一行（保持 UNIQUE 不冲突），重新执行
            run_id = cached["run_id"]
            with atomic(conn):
                conn.execute("UPDATE analysis_runs SET status='running', execution_id=?, created_at=? WHERE run_id=?",
                             (execution_id, _now(), run_id))
    else:
        run_id = new_id("run")
        try:
            with atomic(conn):
                _insert_running(conn, run_id, task_id, asset_id, cache_key, source, pref_ref, cfg_ref, sampling, execution_id)
        except sqlite3.IntegrityError:
            # 并发：已有执行者
            row = _get_run(conn, task_id, asset_id, cache_key)
            return {"reused": False, "in_progress": True, "run_id": row["run_id"] if row else None}

    try:
        from ..media.meta import read_video_metadata
        from ..media.sample import sample_frames
        md = read_video_metadata(Path(asset["source_path"])) if asset["media_type"] != "image" else None
        if asset["media_type"] == "image":
            from ..media.sample import image_as_frame
            frames = [image_as_frame(Path(asset["source_path"]), resize_edge=sampling.get("resize_edge", 600))]
        else:
            frames = sample_frames(Path(asset["source_path"]), segment_start_ms, segment_end_ms,
                                   max_frames=sampling["max_frames"], frames_per_sec=sampling["frames_per_sec"],
                                   resize_edge=sampling.get("resize_edge", 600),
                                   duration_ms=md["duration_ms"], rotation=md["rotation"])
        if sampling.get('source_frame_dedup'):
            # Dense targets can seek to the same actual PTS. Charge/observe that source frame once.
            frames=list({(f.get('source_pts'),f.get('time_base')):f for f in frames}.values())
        pref = preferences_get(conn, pref_ref)
        # 请求幂等键：每次执行 analyze_asset 都用一个新 scope（重试不会复用已结算请求）；
        # execution_id 保持外部传入值，用于把本执行的所有费用关联到同一全片执行
        previous = json.loads(cached.get("coverage_json") or "{}") if cached and cached["status"] == "response_available" else {}
        request_scope = previous.get("request_scope") or new_id("request")
        conn.execute("UPDATE analysis_runs SET coverage_json=? WHERE run_id=?", (json.dumps({"request_scope":request_scope}),run_id))
        scoped_rid = f"{task_id}-{asset_id}-{request_scope}"
        result = vlm.understand_frames(conn, task_id, frames, pref, transport, request_id=scoped_rid, execution_id=execution_id)
        selection = analysis.select_dual_path(result.annotations, pref)
        if asset["media_type"] == "image" and not selection["photo_frames"]:
            selection["photo_frames"] = result.annotations
            selection["video_segments"] = []
        # 统一选择入口：用户明确选定 > 应用规则后的推荐；模型原始评分保留在 annotations
        prefs = feedback.get_preferences(conn, task_id, asset_id)
        selection["top_photo_source"] = "rule"
        if prefs:
            chosen = feedback.resolve_top_photo(selection["photo_frames"], prefs)
            if chosen is not None:
                selection["top_photo"] = chosen
                selection["top_photo_source"] = "user"
        outcome = analysis.goal_check(selection, pref)
        conn.execute(
            "UPDATE analysis_runs SET status='complete', execution_id=?, annotations_json=?, selections_json=?, coverage_json=?, cost_id=? WHERE run_id=?",
            (execution_id, json.dumps(result.annotations, ensure_ascii=False), json.dumps(selection, ensure_ascii=False),
             json.dumps(result.coverage, ensure_ascii=False), result.cost_id, run_id),
        )
        conn.commit()
        _record_member(conn, execution_id, run_id, task_id, asset_id)
        return {"reused": False, "run_id": run_id, "annotations": result.annotations,
                "selection": selection, "coverage": result.coverage, "outcome": outcome,
                "source": result.source, "cost_id": result.cost_id}
    except Exception as e:
        conn.execute("UPDATE analysis_runs SET status=?, coverage_json=? WHERE run_id=?",
                     ("pending_reconciliation" if getattr(e,"code",None) == "pending_reconciliation" else "failed", json.dumps({"error": str(e)[:200], "request_scope": locals().get("request_scope")}, ensure_ascii=False), run_id))
        conn.commit()
        raise


def preferences_get(conn: sqlite3.Connection, pref_ref: dict | None) -> dict:
    if not pref_ref:
        return {"quality_floor": {"required": ["基本可见", "无严重模糊/遮挡"]}}
    from .preferences import get_preference
    return get_preference(conn, pref_ref.get("preference_id"), pref_ref.get("version"))

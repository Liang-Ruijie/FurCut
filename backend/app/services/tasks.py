"""任务创建/查询/取消 + 素材锁定清单。创建自动绑定当前默认配置与偏好版本到快照。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

from .. import security
from ..db import atomic
from . import model_config, preferences, selection_v2
from .assets import new_id

ACTIVE_STATUSES = {"created", "assets_locked", "analyzing", "selecting", "exporting", "ready_for_review", "pending_reconciliation"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def display_title(task: dict) -> str:
    business=json.loads(task.get('business_ref_json') or '{}')
    return str(business.get('title') or '、'.join(Path(a['source_path']).stem for a in task.get('asset_refs',[])[:2]) or '精选成果')


def _resolve_ref(conn: sqlite3.Connection, kind: str, ref: dict | None) -> tuple[dict | None, bool]:
    """解析并校验偏好/配置引用。未传则取默认最新版本；传了必须存在。
    返回 (锁定引用 or None, 是否可用)。"""
    if ref is not None:
        if not isinstance(ref, dict):
            raise security.SecurityError("invalid_ref", f"{kind}_ref 必须是对象")
        if kind == "preference":
            locked = preferences.get_preference_ref(conn, ref.get("preference_id"), ref.get("version"))
        else:
            locked = model_config.get_config_ref(conn, ref.get("model_config_id"), ref.get("version"))
        return locked, True
    # 未传：自动取默认最新版本
    try:
        if kind == "preference":
            locked = preferences.get_preference_ref(conn, None, None)
        else:
            locked = model_config.get_config_ref(conn, None, None)
        return locked, True
    except security.SecurityError:
        return None, False


def _request_digest(asset_ids: list[str], preference_ref, model_config_ref, budget, business_ref, requirement) -> str:
    """创建请求的规范化参数摘要：素材去重排序 + 显式参数（含自然语言需求），不含自动解析的默认版本。"""
    canonical = {
        "asset_ids": sorted(set(asset_ids)),
        "preference_ref": preference_ref,
        "model_config_ref": model_config_ref,
        "budget": budget,
        "business_ref": business_ref,
        "requirement": requirement,
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _existing_task(conn: sqlite3.Connection, client_request_id: str, digest: str) -> str | None:
    """幂等检查统一规则：同 client_request_id 且摘要一致 → 返回原任务；不一致 → 明确冲突。"""
    row = conn.execute(
        "SELECT task_id, request_digest FROM tasks WHERE client_request_id=?", (client_request_id,)
    ).fetchone()
    if row is None:
        return None
    if row["request_digest"] == digest:
        return row["task_id"]
    raise security.SecurityError("idempotent_conflict", "相同 client_request_id 但请求参数不一致，拒绝重复创建")


def create_task(conn: sqlite3.Connection, asset_ids: list[str], client_request_id: str | None = None,
                preference_ref=None, model_config_ref=None, budget=None, business_ref=None,
                requirement: str | None = None) -> dict:
    unique_ids = list(dict.fromkeys(asset_ids))
    digest = _request_digest(unique_ids, preference_ref, model_config_ref, budget, business_ref, requirement)

    # 幂等：重复提交（参数一致）返回原任务；参数不一致明确冲突（事务内外同一规则）
    if client_request_id:
        existing = _existing_task(conn, client_request_id, digest)
        if existing is not None:
            return get_task(conn, existing)

    for aid in unique_ids:
        r = conn.execute("SELECT asset_id FROM assets WHERE asset_id=?", (aid,)).fetchone()
        if r is None:
            raise security.SecurityError("asset_not_found", f"素材未登记：{aid}")

    pref_ref, pref_ok = _resolve_ref(conn, "preference", preference_ref)
    cfg_ref, cfg_ok = _resolve_ref(conn, "model_config", model_config_ref)

    task_id = new_id("task")
    limit = budget.get("limit") if budget else None
    status = "assets_locked"
    now = _now()
    from . import analysis as analysis_mod
    # 记录实际采样参数与提示词版本（可核验实际生效，不写空 includes）
    try:
        _cfg_full = model_config.get_config(conn, cfg_ref["model_config_id"], cfg_ref["version"]) if cfg_ref else None
        _sampling = (_cfg_full or {}).get("sampling", {})
    except Exception:
        _sampling = {}
    snapshot = {
        "created_at": now,
        "includes": ["preference_version", "model_config_version", "sampling_params", "prompt_version"],
        "preference_ref": pref_ref,
        "model_config_ref": cfg_ref,
        "preference_available": pref_ok,
        "model_config_available": cfg_ok,
        "sampling_params": _sampling,
        "prompt_version": analysis_mod.PROMPT_VERSION,
        "coverage_plan_version": 1,
        "coarse_batch_version": 1,
        "quality_policy_version": _sampling.get('quality_contract_version',1),
        "selection_pipeline_version": selection_v2.default_pipeline_version(),
        "video_input_version": 1,
        "quality_context": {"requirement": requirement or "", "preference_ref": pref_ref},
        "default_call_parameters": {"temperature":0.3,"max_output_tokens":8000,"reasoning":"disabled"},
        # 全片路径按公开配置解析后的实际采样参数，固定进任务快照。
        "fullscan_sampling": __import__("app.services.fullscan", fromlist=["effective_sampling"]).effective_sampling(_sampling),
    }
    with atomic(conn):
        if client_request_id:
            existing = _existing_task(conn, client_request_id, digest)
            if existing is not None:
                return get_task(conn, existing)
        conn.execute(
            """INSERT INTO tasks(task_id, client_request_id, request_digest, requirement, status, outcome, error_json, resume_point,
                 preference_ref_json, model_config_ref_json, snapshot_ref_json, business_ref_json,
                 budget_limit_minor, budget_currency, budget_status, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (task_id, client_request_id, digest, requirement, status, None, None, "assets_locked",
             json.dumps(pref_ref, ensure_ascii=False) if pref_ref else None,
             json.dumps(cfg_ref, ensure_ascii=False) if cfg_ref else None,
             json.dumps(snapshot, ensure_ascii=False),
             json.dumps(business_ref, ensure_ascii=False) if business_ref else None,
             limit, budget.get("currency", "CNY") if budget else "CNY",
             "open" if limit is not None else "unconfigured",
             now, now),
        )
        for aid in unique_ids:
            fpr = conn.execute("SELECT fingerprint FROM assets WHERE asset_id=?", (aid,)).fetchone()["fingerprint"]
            conn.execute(
                "INSERT INTO task_assets(task_id, asset_id, role, fingerprint_at_lock) VALUES(?,?,?,?)",
                (task_id, aid, "primary", fpr),
            )
    return get_task(conn, task_id)


def get_task(conn: sqlite3.Connection, task_id: str) -> dict:
    row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if row is None:
        raise security.SecurityError("task_not_found", f"任务不存在：{task_id}")
    task = dict(row)
    asset_rows = conn.execute(
        "SELECT ta.asset_id, ta.role, ta.fingerprint_at_lock, a.fingerprint, a.media_type, a.source_path "
        "FROM task_assets ta JOIN assets a ON a.asset_id=ta.asset_id WHERE ta.task_id=?",
        (task_id,),
    ).fetchall()
    task["asset_refs"] = [dict(r) for r in asset_rows]
    return task


def cancel_task(conn: sqlite3.Connection, task_id: str) -> dict:
    with atomic(conn):
        row = conn.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise security.SecurityError("task_not_found", f"任务不存在：{task_id}")
        if row["status"] == "cancelled":
            return get_task(conn, task_id)
        if row["status"] not in ACTIVE_STATUSES:
            raise security.SecurityError("invalid_transition", f"当前状态 {row['status']} 不可取消")
        conn.execute(
            "UPDATE tasks SET status='cancelled', updated_at=? WHERE task_id=?",
            (_now(), task_id),
        )
        conn.execute("UPDATE analysis_jobs SET status='cancelled',stage='已取消',updated_at=? WHERE task_id=? AND status IN ('running','pending_reconciliation')",(_now(),task_id))
    return get_task(conn, task_id)


def confirm_review(conn: sqlite3.Connection, task_id: str) -> dict:
    """审核确认完成：ready_for_review → completed（终态，不可再取消/分析）。"""
    with atomic(conn):
        row = conn.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise security.SecurityError("task_not_found", f"任务不存在：{task_id}")
        if row["status"] == "completed":
            return get_task(conn, task_id)
        if row["status"] != "ready_for_review":
            raise security.SecurityError("invalid_transition", f"当前状态 {row['status']} 不可确认完成")
        conn.execute(
            "UPDATE tasks SET status='completed', updated_at=? WHERE task_id=?",
            (_now(), task_id),
        )
    return get_task(conn, task_id)


def reopen_review(conn: sqlite3.Connection, task_id: str) -> dict:
    with atomic(conn):
        row = conn.execute("SELECT status FROM tasks WHERE task_id=?",(task_id,)).fetchone()
        if row is None or row["status"] != "completed":
            raise security.SecurityError("invalid_transition","仅已确认审核的任务可继续修改")
        conn.execute("UPDATE tasks SET status='ready_for_review',updated_at=? WHERE task_id=?",(_now(),task_id))
    return get_task(conn,task_id)


def can_edit_settings(conn, task):
    """Settings are mutable only before any processing or billing evidence exists."""
    if task['status'] != 'assets_locked' or task.get('outcome') or any(task.get(k,0) for k in ('budget_settled_minor','budget_reserved_minor','budget_unknown_minor')):
        return False
    return not any(conn.execute(f'SELECT 1 FROM {table} WHERE task_id=? LIMIT 1',(task['task_id'],)).fetchone() for table in ('analysis_jobs','analysis_runs','executions','model_requests','cost_records'))


def settings_revision(task):
    value=[task.get('requirement'),task.get('preference_ref_json'),
           sorted((a['asset_id'],a['fingerprint_at_lock']) for a in task['asset_refs'])]
    return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()


def update_settings(conn, task_id, preference_ref, requirement, best_frame_enabled, best_frame_hold_ms,
                    asset_ids=None, expected_revision=None):
    with atomic(conn):
        task=get_task(conn,task_id)
        if not can_edit_settings(conn,task):
            raise security.SecurityError('task_settings_locked','任务已开始过处理，原标准不可修改；请按此设置新建任务')
        if expected_revision is not None and expected_revision != settings_revision(task):
            raise security.SecurityError('task_settings_changed','其他页面已修改此任务，请重新读取最新设置后再修改')
        ids=list(dict.fromkeys(asset_ids if asset_ids is not None else [a['asset_id'] for a in task['asset_refs']]))
        if not ids:
            raise security.SecurityError('no_assets','至少保留一个素材后才能保存')
        from . import assets
        for aid in ids:
            assets.check_export_asset(conn,None,aid)
        ref=preferences.get_preference_ref(conn,preference_ref.get('preference_id'),preference_ref.get('version'))
        pref=preferences.get_preference(conn,ref['preference_id'],ref['version'])
        exports={**pref['export_defaults'],'best_frame_enabled':best_frame_enabled,'best_frame_hold_ms':best_frame_hold_ms}
        if exports != pref['export_defaults']:
            # Task-specific output settings never create a common preference version.
            pid=new_id('task-options')
            conn.execute('INSERT INTO preferences(preference_id,version,data_json,created_at) VALUES(?,?,?,?)',(pid,1,json.dumps({'quality_floor':pref['quality_floor'],'visual_rules':pref['visual_rules'],'export_defaults':exports},ensure_ascii=False),_now()))
            ref={'preference_id':pid,'version':1}
        snapshot=json.loads(task.get('snapshot_ref_json') or '{}')
        snapshot.update(preference_ref=ref,preference_available=True,quality_context={'requirement':requirement,'preference_ref':ref})
        conn.execute('DELETE FROM task_assets WHERE task_id=?',(task_id,))
        for aid in ids:
            fingerprint=conn.execute('SELECT fingerprint FROM assets WHERE asset_id=?',(aid,)).fetchone()[0]
            conn.execute('INSERT INTO task_assets(task_id,asset_id,role,fingerprint_at_lock) VALUES(?,?,?,?)',(task_id,aid,'primary',fingerprint))
        conn.execute('UPDATE tasks SET requirement=?,preference_ref_json=?,snapshot_ref_json=?,updated_at=? WHERE task_id=?',(requirement,json.dumps(ref),json.dumps(snapshot,ensure_ascii=False),_now(),task_id))
    return get_task(conn,task_id)

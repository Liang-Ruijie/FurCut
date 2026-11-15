"""费用护栏：预算未配置禁付费；调用前原子预留；取消后禁新增；unknown 保守计不按零；幂等键带任务/参数校验。"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from .. import security
from ..db import atomic
from .assets import new_id

# 金额单位：微元（1 元 = 1,000,000 微元），可表达低于 1 分的调用成本（如 0.05 元 = 50,000 微元）
MICRO_PER_CNY = 1_000_000
SUPPORTED_CURRENCIES = {"CNY"}
# 允许新增付费预留的任务状态（取消/失败/完成等终态一律禁止）
RESERVABLE_STATUSES = {"created", "assets_locked", "analyzing", "selecting", "exporting"}


@dataclass
class TokenPricing:
    """token 计价（美元/每百万 token）；默认取 DeepSeek deepseek-flash 官方峰值价作保守估算。"""
    input_per_1m_usd: float = 0.3          # 输入（缓存未命中）峰值
    output_per_1m_usd: float = 1.2         # 输出峰值
    cache_hit_input_per_1m_usd: float = 0.006
    cny_per_usd: float = 7.2               # 结算汇率（可配置）


# 各服务商统一计价（美元/百万 token，cny_per_usd=7.2）。主分析与对照/质检共用，避免两套价格表。
PRICING_BY_PROVIDER = {
    "deepseek": TokenPricing(input_per_1m_usd=0.3, output_per_1m_usd=1.2, cache_hit_input_per_1m_usd=0.006, cny_per_usd=7.2),
    # qwen3.8-max：输入 ¥12/1M、输出 ¥36/1M、缓存命中输入 ¥1.5/1M → USD 等价 1.667/5.0/0.208
    "dashscope": TokenPricing(input_per_1m_usd=1.667, output_per_1m_usd=5.0, cache_hit_input_per_1m_usd=0.208, cny_per_usd=7.2),
}


def pricing_for_provider(provider: str | None) -> TokenPricing:
    """按服务商取计价；未知 provider 不得静默回退 DeepSeek 价。"""
    p = (provider or "deepseek").lower()
    if p not in PRICING_BY_PROVIDER:
        raise security.SecurityError("unknown_pricing", f"未知 provider 无价格依据：{p!r}，不得静默回退 DeepSeek 价")
    return PRICING_BY_PROVIDER[p]


def token_cost_micro(pricing: TokenPricing, prompt_tokens: int, completion_tokens: int,
                     cache_hit_tokens: int = 0) -> int:
    """按官方 token 计价折算微元（人民币）。
    prompt_tokens 为输入总量，cache_hit_tokens 是其命中缓存的子集，
    缓存命中部分按更低单价计，避免把命中量再重复算一遍。"""
    cache_miss = max(0, int(prompt_tokens) - int(cache_hit_tokens))
    usd = (
        (cache_miss / 1e6) * pricing.input_per_1m_usd
        + (int(cache_hit_tokens) / 1e6) * pricing.cache_hit_input_per_1m_usd
        + (int(completion_tokens) / 1e6) * pricing.output_per_1m_usd
    )
    return round(usd * pricing.cny_per_usd * MICRO_PER_CNY)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _budget_status(limit: int | None, effective: int) -> str:
    if limit is None:
        return "unconfigured"
    if effective >= limit:
        return "exceeded"
    return "open"


def _budget_state(conn: sqlite3.Connection, task_id: str) -> dict:
    row = conn.execute(
        "SELECT budget_limit_minor, budget_settled_minor, budget_reserved_minor, budget_unknown_minor, budget_currency, budget_status, status FROM tasks WHERE task_id=?",
        (task_id,),
    ).fetchone()
    if row is None:
        raise security.SecurityError("task_not_found", f"任务不存在：{task_id}")
    limit = row["budget_limit_minor"]
    settled = row["budget_settled_minor"]
    reserved = row["budget_reserved_minor"]
    unknown = row["budget_unknown_minor"]
    return {
        "currency": row["budget_currency"],
        "limit": limit,
        "settled": settled,
        "reserved": reserved,
        "unknown": unknown,
        "effective": settled + reserved + unknown,
        "status": row["budget_status"],
        "task_status": row["status"],
    }


def get_budget(conn: sqlite3.Connection, task_id: str) -> dict:
    s = _budget_state(conn, task_id)
    s["status"] = _budget_status(s["limit"], s["effective"])
    return s


def archived_workspace_ledger(conn: sqlite3.Connection) -> dict:
    """Retain lifetime spending and source holds when old tasks leave a workspace."""
    row = conn.execute("SELECT value FROM meta WHERE key='archived_workspace_ledger'").fetchone()
    if row is None:
        return {"used_minor": 0, "pending_fingerprints": []}
    try:
        value = json.loads(row[0])
        amount = value["used_minor"]
        holds = value["pending_fingerprints"]
        if type(amount) is not int or amount < 0 or not isinstance(holds, list):
            raise ValueError("invalid archived totals")
        if any(not isinstance(h, str) or len(h) != 64 or any(c not in "0123456789abcdef" for c in h) for h in holds):
            raise ValueError("invalid source fingerprint")
        return value
    except (ValueError, TypeError, KeyError):
        raise security.SecurityError("archived_ledger_invalid", "归档账本摘要无效，请恢复核对后再操作；不会按零计算")


def workspace_budget(conn: sqlite3.Connection) -> dict:
    from ..config import settings
    used=conn.execute("SELECT COALESCE(SUM(amount_minor),0) FROM cost_records WHERE call_source!='mock' AND status IN ('reserved','unknown','settled')").fetchone()[0]
    archived=archived_workspace_ledger(conn)['used_minor']
    used+=archived
    limit=settings.app_workspace_budget_limit_minor
    return {'archived_used':archived,'limit':limit,'used':used,'remaining':max(0,limit-used) if limit is not None else None,
            'basis':'lifetime ledger; mock excluded; legacy/unknown retained'}


def _validate_currency(currency: str) -> None:
    if currency not in SUPPORTED_CURRENCIES:
        raise security.SecurityError("unsupported_currency", f"不支持的币种：{currency}")


def reserve_cost(conn: sqlite3.Connection, task_id: str, amount_minor: int, request_id: str,
                 provider: str = "", model: str = "", operation: str = "",
                 retry_of: str | None = None, cache_hit: bool = False,
                 execution_id: str | None = None, call_source: str = "external") -> dict:
    with atomic(conn):
        s = _budget_state(conn, task_id)
        _validate_currency(s["currency"])
        # 取消/终态后禁止新增付费调用（与取消在同一写锁内检查，无竞态）
        if s["task_status"] not in RESERVABLE_STATUSES:
            raise security.SecurityError("invalid_transition", f"任务状态 {s['task_status']} 不允许新增付费调用")

        # 幂等：同 request_id 且任务/金额/操作一致 → 返回原记录；不一致 → 明确拒绝
        existing = conn.execute("SELECT * FROM cost_records WHERE request_id=?", (request_id,)).fetchone()
        if existing is not None:
            if (existing["task_id"] == task_id and (existing["reservation_minor"] if existing["reservation_minor"] is not None else existing["amount_minor"]) == amount_minor
                    and (existing["operation"] or "") == operation):
                return dict(existing)
            raise security.SecurityError("idempotent_conflict", "相同 request_id 但任务/金额/操作不一致，拒绝重复使用")

        if s["limit"] is None:
            raise security.SecurityError("budget_unconfigured", "预算未配置，禁止发起付费调用")
        if amount_minor <= 0:
            raise security.SecurityError("invalid_amount", "预留金额必须为正数")
        if s["effective"] + amount_minor > s["limit"]:
            raise security.SecurityError("budget_exceeded", "预算不足，已停止新付费调用")
        if call_source!='mock':
            whole=workspace_budget(conn)
            if whole['limit'] is not None and whole['used']+amount_minor>whole['limit']:
                raise security.SecurityError('workspace_budget_exceeded','已达到管理员设置的工作空间费用上限；提高单个任务预算不能绕过此限制')

        new_reserved = s["reserved"] + amount_minor
        conn.execute(
            "UPDATE tasks SET budget_reserved_minor=?, budget_status=?, updated_at=? WHERE task_id=?",
            (new_reserved, _budget_status(s["limit"], s["settled"] + new_reserved + s["unknown"]), _now(), task_id),
        )
        cost_id = new_id("cost")
        now = _now()
        conn.execute(
            """INSERT INTO cost_records(cost_id, task_id, execution_id, provider, model, operation, usage_json, status,
                 currency, amount_minor, estimate_basis, request_id, retry_of, cache_hit, created_at, updated_at, reservation_minor, call_source)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cost_id, task_id, execution_id, provider, model, operation, "{}", "reserved",
             s["currency"], amount_minor, "reserved", request_id, retry_of, 1 if cache_hit else 0, now, now, amount_minor, call_source),
        )
        return dict(conn.execute("SELECT * FROM cost_records WHERE cost_id=?", (cost_id,)).fetchone())


def settle_cost(conn: sqlite3.Connection, cost_id: str, actual_amount_minor: int,
                usage: dict | None = None, estimate_basis: str = "invoice") -> dict:
    """结算（须显式给实际金额，不拿预留估算冒充）。支持 reserved 与 unknown 两种来源。
    estimate_basis：invoice=有实际账单；usage=按返回用量×单价折算（仍是估算，非正式账单）。"""
    if actual_amount_minor < 0:
        raise security.SecurityError("invalid_amount", "结算金额不能为负数")
    with atomic(conn):
        row = conn.execute("SELECT * FROM cost_records WHERE cost_id=?", (cost_id,)).fetchone()
        if row is None:
            raise security.SecurityError("cost_not_found", f"费用记录不存在：{cost_id}")
        if row["status"] == "settled":
            return dict(row)  # 幂等：已结算不再重复入账
        if row["status"] not in {"reserved", "unknown"}:
            raise security.SecurityError("invalid_transition", f"费用记录状态 {row['status']} 不可结算")

        s = _budget_state(conn, row["task_id"])
        _validate_currency(s["currency"])
        if row["status"] == "reserved":
            new_reserved = s["reserved"] - row["amount_minor"]
            new_unknown = s["unknown"]
        else:  # unknown：核实为实际收费（含确认未收费 actual=0），释放保守预留
            new_reserved = s["reserved"]
            new_unknown = s["unknown"] - row["amount_minor"]
        new_settled = s["settled"] + actual_amount_minor
        conn.execute(
            "UPDATE tasks SET budget_reserved_minor=?, budget_settled_minor=?, budget_unknown_minor=?, budget_status=?, updated_at=? WHERE task_id=?",
            (new_reserved, new_settled, new_unknown,
             _budget_status(s["limit"], new_settled + new_reserved + new_unknown), _now(), row["task_id"]),
        )
        conn.execute(
            "UPDATE cost_records SET status='settled', amount_minor=?, estimate_basis=?, usage_json=?, updated_at=? WHERE cost_id=?",
            (actual_amount_minor, estimate_basis, json.dumps(usage or {}, ensure_ascii=False), _now(), cost_id),
        )
        return dict(conn.execute("SELECT * FROM cost_records WHERE cost_id=?", (cost_id,)).fetchone())


def mark_unknown(conn: sqlite3.Connection, cost_id: str, note: str = "") -> dict:
    """超时/账单不明：保留原预留额作保守估算并继续计入护栏，绝不按零计、不允许无依据下调。"""
    with atomic(conn):
        row = conn.execute("SELECT * FROM cost_records WHERE cost_id=?", (cost_id,)).fetchone()
        if row is None:
            raise security.SecurityError("cost_not_found", f"费用记录不存在：{cost_id}")
        if row["status"] == "unknown":
            return dict(row)  # 幂等
        if row["status"] != "reserved":
            raise security.SecurityError("invalid_transition", f"费用记录状态 {row['status']} 不可标为未知")

        original = row["amount_minor"]
        conservative = original  # 不提供下调入口；下调/释放必须走 settle(actual) 核销流程
        s = _budget_state(conn, row["task_id"])
        conn.execute(
            "UPDATE tasks SET budget_reserved_minor=?, budget_unknown_minor=?, budget_status=?, updated_at=? WHERE task_id=?",
            (s["reserved"] - original, s["unknown"] + conservative,
             _budget_status(s["limit"], s["settled"] + (s["reserved"] - original) + (s["unknown"] + conservative)),
             _now(), row["task_id"]),
        )
        conn.execute(
            "UPDATE cost_records SET status='unknown', amount_minor=?, estimate_basis='unknown', usage_json=?, updated_at=? WHERE cost_id=?",
            (conservative, json.dumps({"note": note}, ensure_ascii=False), _now(), cost_id),
        )
        return dict(conn.execute("SELECT * FROM cost_records WHERE cost_id=?", (cost_id,)).fetchone())


def record_direct_cost(conn: sqlite3.Connection, task_id: str, provider: str, model: str,
                       operation: str, usage: dict, amount_minor: int, request_id: str,
                       execution_id: str | None = None) -> dict:
    """补记一笔已发生的直接结算费用（如绕过预留的探测），幂等：同 request_id 不重复入账。"""
    existing = conn.execute("SELECT * FROM cost_records WHERE request_id=?", (request_id,)).fetchone()
    if existing is not None:
        return dict(existing)
    with atomic(conn):
        s = _budget_state(conn, task_id)
        _validate_currency(s["currency"])
        conn.execute(
            "UPDATE tasks SET budget_settled_minor=?, budget_status=?, updated_at=? WHERE task_id=?",
            (s["settled"] + amount_minor,
             _budget_status(s["limit"], (s["settled"] + amount_minor) + s["reserved"] + s["unknown"]),
             _now(), task_id),
        )
        cost_id = new_id("cost")
        now = _now()
        conn.execute(
            """INSERT INTO cost_records(cost_id, task_id, execution_id, provider, model, operation, usage_json, status,
                 currency, amount_minor, estimate_basis, request_id, retry_of, cache_hit, created_at, updated_at, reservation_minor, call_source)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cost_id, task_id, execution_id, provider, model, operation,
             json.dumps(usage, ensure_ascii=False), "settled", s["currency"], amount_minor,
             "usage", request_id, None, 0, now, now),
        )
        return dict(conn.execute("SELECT * FROM cost_records WHERE cost_id=?", (cost_id,)).fetchone())


def adjust_budget(conn: sqlite3.Connection, task_id: str, limit_minor: int | None) -> dict:
    with atomic(conn):
        row = conn.execute("SELECT status, outcome, budget_currency FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise security.SecurityError("task_not_found", f"任务不存在：{task_id}")
        _validate_currency(row["budget_currency"])
        allow = row["status"] in {"created", "assets_locked"} or (row["status"] == "failed" and row["outcome"] == "incomplete_budget")
        if not allow:
            raise security.SecurityError("invalid_transition", f"当前状态 {row['status']}/{row['outcome']} 不可调整预算")
        s = _budget_state(conn, task_id)
        if limit_minor is not None and limit_minor < s['effective']:
            raise security.SecurityError('budget_below_committed','预算不能低于已发生、在途和待核账费用的合计')
        conn.execute(
            "UPDATE tasks SET budget_limit_minor=?, budget_status=?, updated_at=? WHERE task_id=?",
            (limit_minor, _budget_status(limit_minor, s["effective"]), _now(), task_id),
        )
    return get_budget(conn, task_id)


def release_unsent(conn, cost_id, request_id):
    """Known new internal reservation only. Never touch unknown or legacy fees."""
    with atomic(conn):
        row=conn.execute("SELECT * FROM cost_records WHERE cost_id=? AND request_id=?",(cost_id,request_id)).fetchone()
        if not row or row["status"]!="reserved" or row["call_source"] not in ("mock","real"):
            return
        if conn.execute("SELECT 1 FROM model_requests WHERE request_id=?",(request_id,)).fetchone():
            return
        state=_budget_state(conn,row["task_id"])
        remaining=state["reserved"]-row["amount_minor"]
        if remaining<0:raise security.SecurityError("ledger_mismatch","预留账本不一致")
        conn.execute("UPDATE cost_records SET status='settled',amount_minor=0,estimate_basis='local_not_sent',usage_json=?,updated_at=? WHERE cost_id=?",(json.dumps({"not_dispatched":True}),_now(),cost_id))
        conn.execute("UPDATE tasks SET budget_reserved_minor=?,budget_status=?,updated_at=? WHERE task_id=?",(remaining,_budget_status(state["limit"],state["settled"]+remaining+state["unknown"]),_now(),row["task_id"]))

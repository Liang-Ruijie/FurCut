"""实验身份与执行领取：身份绑定输入内容/提示词/模型配置/分辨率/模式；原子领取防并发；尝试编号可追溯。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from ..db import atomic
from .assets import new_id

STALE_SECONDS = 600  # running 超过此时长视为中断


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stale(updated_at: str | None) -> bool:
    if not updated_at:
        return True
    try:
        return datetime.now(timezone.utc) - datetime.fromisoformat(updated_at) > timedelta(seconds=STALE_SECONDS)
    except ValueError:
        return True


def identity(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def acquire(conn: sqlite3.Connection, ident: str, task_id: str, mode: str, experiment: str) -> dict:
    """原子领取：complete→复用；running→处理中/中断；failed/interrupted→新尝试。"""
    with atomic(conn):
        row = conn.execute("SELECT * FROM experiments WHERE identity=?", (ident,)).fetchone()
        if row is None:
            eid = new_id("exp")
            conn.execute(
                "INSERT INTO experiments(experiment_id, identity, task_id, mode, experiment, status, attempt, "
                "result_json, error_json, cost_json, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (eid, ident, task_id, mode, experiment, "running", 1, None, None, None, _now(), _now()),
            )
            return {"status": "acquired", "experiment_id": eid, "attempt": 1, "reused": False, "retry_of": None, "result": None}
        d = dict(row)
        if d["status"] == "complete" and d["result_json"]:
            return {"status": "complete", "experiment_id": d["experiment_id"], "attempt": d["attempt"],
                    "reused": True, "retry_of": None, "result": json.loads(d["result_json"])}
        if d["status"] == "running":
            if _stale(d["updated_at"]):
                conn.execute("UPDATE experiments SET status='interrupted', updated_at=? WHERE experiment_id=?",
                             (_now(), d["experiment_id"]))
                return {"status": "interrupted", "experiment_id": d["experiment_id"], "attempt": d["attempt"],
                        "reused": True, "retry_of": None, "result": None}
            return {"status": "in_progress", "experiment_id": d["experiment_id"], "attempt": d["attempt"],
                    "reused": True, "retry_of": None, "result": None}
        # failed / interrupted → 新尝试（新费用），关联上次尝试
        new_attempt = (d["attempt"] or 0) + 1
        conn.execute("UPDATE experiments SET status='running', attempt=?, error_json=NULL, cost_json=NULL, updated_at=? WHERE experiment_id=?",
                     (new_attempt, _now(), d["experiment_id"]))
        return {"status": "acquired", "experiment_id": d["experiment_id"], "attempt": new_attempt,
                "reused": False, "retry_of": d["attempt"], "result": None}


def complete(conn: sqlite3.Connection, experiment_id: str, result: dict, cost_ids: list[str] | None = None) -> None:
    conn.execute("UPDATE experiments SET status='complete', result_json=?, cost_json=?, updated_at=? WHERE experiment_id=?",
                 (json.dumps(result, ensure_ascii=False), json.dumps(cost_ids or [], ensure_ascii=False), _now(), experiment_id))
    conn.commit()


def fail(conn: sqlite3.Connection, experiment_id: str, error: str, cost_ids: list[str] | None = None) -> None:
    conn.execute("UPDATE experiments SET status='failed', error_json=?, cost_json=?, updated_at=? WHERE experiment_id=?",
                 (json.dumps({"error": error[:200]}, ensure_ascii=False), json.dumps(cost_ids or [], ensure_ascii=False), _now(), experiment_id))
    conn.commit()

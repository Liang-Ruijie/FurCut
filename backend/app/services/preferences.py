"""偏好持久化与版本化：追加式，历史不覆盖；改偏好产生新版本（缓存失效依据）。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .. import security
from ..db import atomic

DEFAULT_PREFERENCE_ID = "default"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_preference(conn: sqlite3.Connection, preference_id: str | None,
                    quality_floor: dict, visual_rules: dict, export_defaults: dict, display_name: str = "") -> dict:
    display_name = _validate_name(display_name)
    if "best_frame_enabled" in export_defaults and not isinstance(export_defaults["best_frame_enabled"],bool):
        raise security.SecurityError("invalid_preference","最佳帧启用项必须是布尔值")
    hold=export_defaults.get("best_frame_hold_ms",300)
    if isinstance(hold,bool) or not isinstance(hold,int) or not 0<=hold<=5000:
        raise security.SecurityError("invalid_preference","最佳帧停留必须为0–5000整数毫秒")
    pid = preference_id or DEFAULT_PREFERENCE_ID
    with atomic(conn):
        row = conn.execute("SELECT MAX(version) AS v FROM preferences WHERE preference_id=?", (pid,)).fetchone()
        next_version = (row["v"] or 0) + 1
        data = {"quality_floor": quality_floor, "visual_rules": visual_rules, "export_defaults": export_defaults}
        conn.execute("INSERT INTO preferences(preference_id, version, data_json, created_at) VALUES(?,?,?,?)",
                     (pid, next_version, json.dumps(data, ensure_ascii=False), _now()))
        if display_name:
            _write_name(conn, pid, next_version, display_name)
    return {"preference_id": pid, "version": next_version}


def _name_key(pid, version):
    return "preference_display_name:" + json.dumps([pid, version], ensure_ascii=False, separators=(",", ":"))


def _validate_name(value):
    if not isinstance(value, str):
        raise security.SecurityError("invalid_preference_name", "版本名称须为文字")
    value = value.strip()
    if len(value) > 12 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise security.SecurityError("invalid_preference_name", "版本名称最多12个字符，不能包含换行")
    return value


def _read_name(conn, pid, version):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (_name_key(pid, version),)).fetchone()
    return row[0] if row else ""


def _write_name(conn, pid, version, name):
    # Display metadata is separate from immutable rules, snapshots and cache identity.
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (_name_key(pid, version), name))


def rename_preference(conn, version, display_name, preference_id=None):
    name = _validate_name(display_name)
    pid = preference_id or DEFAULT_PREFERENCE_ID
    with atomic(conn):
        get_preference_ref(conn, pid, version)
        _write_name(conn, pid, version, name)
    return get_preference(conn, pid, version, include_name=True)


def list_labels(conn, preference_id=None):
    pid = preference_id or DEFAULT_PREFERENCE_ID
    return [{"version": v, "display_name": _read_name(conn, pid, v)} for v in list_versions(conn, pid)]


def get_preference(conn: sqlite3.Connection, preference_id: str | None = None, version: int | None = None, *, include_name: bool = False) -> dict:
    pid = preference_id or DEFAULT_PREFERENCE_ID
    if version is not None:
        row = conn.execute(
            "SELECT * FROM preferences WHERE preference_id=? AND version=?", (pid, version)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM preferences WHERE preference_id=? ORDER BY version DESC LIMIT 1", (pid,)
        ).fetchone()
    if row is None:
        raise security.SecurityError("preference_not_found", f"偏好不存在：{pid} v{version if version else 'latest'}")
    data = json.loads(row["data_json"])
    result = {
        "preference_id": row["preference_id"],
        "version": row["version"],
        "quality_floor": data.get("quality_floor", {}),
        "visual_rules": data.get("visual_rules", {}),
        "export_defaults": data.get("export_defaults", {}),
        "created_at": row["created_at"],
    }
    if include_name:
        result["display_name"] = _read_name(conn, pid, row["version"])
    return result


def list_versions(conn: sqlite3.Connection, preference_id: str | None = None) -> list[int]:
    pid = preference_id or DEFAULT_PREFERENCE_ID
    rows = conn.execute(
        "SELECT version FROM preferences WHERE preference_id=? ORDER BY version", (pid,)
    ).fetchall()
    return [r["version"] for r in rows]


def get_preference_ref(conn: sqlite3.Connection, preference_id: str | None = None, version: int | None = None) -> dict:
    """返回锁定的引用（id + version + 存在性校验），供任务快照使用。"""
    pid = preference_id or DEFAULT_PREFERENCE_ID
    if version is not None:
        row = conn.execute(
            "SELECT preference_id, version FROM preferences WHERE preference_id=? AND version=?", (pid, version)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT preference_id, version FROM preferences WHERE preference_id=? ORDER BY version DESC LIMIT 1", (pid,)
        ).fetchone()
    if row is None:
        raise security.SecurityError("preference_not_found", f"偏好不存在：{pid} v{version if version else 'latest'}")
    return {"preference_id": row["preference_id"], "version": row["version"]}

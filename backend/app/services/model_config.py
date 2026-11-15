"""模型配置持久化：密钥引用（非明文）服务端保留，响应层只给引用名与是否已配置。"""
from __future__ import annotations

import json
import sqlite3
from urllib.parse import urlparse
from datetime import datetime, timezone

from .. import security

DEFAULT_CONFIG_ID = "default"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize_endpoint(ep: dict) -> dict:
    ref = (ep.get("api_key_ref") or "").strip()
    if security.is_secret_like(ref):
        raise security.SecurityError(
            "invalid_api_key_ref", "api_key_ref 应是密钥引用名（如环境变量名），不要直接传真实密钥")
    provider = ep.get("provider", "")
    if provider in {"deepseek", "dashscope"}:
        model = (ep.get("model") or "").strip()
        base = (ep.get("base_url") or "").strip()
        url = urlparse(base)
        other_host = "deepseek" if provider == "dashscope" else "dashscope"
        wrong_model = model.lower().startswith("deepseek-") if provider == "dashscope" else model.lower().startswith("qwen")
        wrong_ref = "DEEPSEEK_API_KEY" if provider == "dashscope" else "DASHSCOPE_API_KEY"
        if other_host in (url.hostname or "").split(".") or wrong_model or ref == wrong_ref:
            raise security.SecurityError("provider_endpoint_mismatch", "服务商与模型、地址或密钥引用不一致，请检查后保存")
        ep = {**ep, "model": model, "base_url": base}
    return {**ep, "api_key_ref": ref}


def save_config(conn: sqlite3.Connection, model_config_id: str | None, llm: dict,
                vlm: dict, sampling: dict) -> dict:
    cid = model_config_id or DEFAULT_CONFIG_ID
    row = conn.execute(
        "SELECT MAX(version) AS v FROM model_configs WHERE model_config_id=?", (cid,)
    ).fetchone()
    next_version = (row["v"] or 0) + 1
    data = {
        "llm": _sanitize_endpoint(llm),
        "vlm": _sanitize_endpoint(vlm),
        "sampling": sampling,
    }
    conn.execute(
        "INSERT INTO model_configs(model_config_id, version, data_json, created_at) VALUES(?,?,?,?)",
        (cid, next_version, json.dumps(data, ensure_ascii=False), _now()),
    )
    conn.commit()
    return {"model_config_id": cid, "version": next_version}


def _to_view(cid: str, version: int, data: dict, created_at: str) -> dict:
    llm = data.get("llm", {})
    vlm = data.get("vlm", {})
    return {
        "model_config_id": cid,
        "version": version,
        "llm": {**llm, "key_configured": bool(security.resolve_secret(llm.get("api_key_ref")))},
        "vlm": {**vlm, "key_configured": bool(security.resolve_secret(vlm.get("api_key_ref")))},
        "sampling": data.get("sampling", {}),
        "created_at": created_at,
    }


def get_config(conn: sqlite3.Connection, model_config_id: str | None = None, version: int | None = None) -> dict:
    cid = model_config_id or DEFAULT_CONFIG_ID
    if version is not None:
        row = conn.execute(
            "SELECT * FROM model_configs WHERE model_config_id=? AND version=?", (cid, version)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM model_configs WHERE model_config_id=? ORDER BY version DESC LIMIT 1", (cid,)
        ).fetchone()
    if row is None:
        raise security.SecurityError("config_not_found", f"配置不存在：{cid} v{version if version else 'latest'}")
    return _to_view(row["model_config_id"], row["version"], json.loads(row["data_json"]), row["created_at"])


def get_config_ref(conn: sqlite3.Connection, model_config_id: str | None = None, version: int | None = None) -> dict:
    """返回锁定的引用（id + version + 存在性校验），供任务快照使用。"""
    cid = model_config_id or DEFAULT_CONFIG_ID
    if version is not None:
        row = conn.execute(
            "SELECT model_config_id, version FROM model_configs WHERE model_config_id=? AND version=?", (cid, version)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT model_config_id, version FROM model_configs WHERE model_config_id=? ORDER BY version DESC LIMIT 1", (cid,)
        ).fetchone()
    if row is None:
        raise security.SecurityError("config_not_found", f"配置不存在：{cid} v{version if version else 'latest'}")
    return {"model_config_id": row["model_config_id"], "version": row["version"]}


def get_vlm_runtime(conn: sqlite3.Connection, model_config_ref: dict | None) -> dict:
    """按任务锁定的 id/version 读取完整 ModelConfig，返回给客户端的 VLM 运行时配置。
    不得用最新配置替代旧任务锁定版本。"""
    if not model_config_ref:
        raise security.SecurityError("config_not_found", "任务未绑定模型配置")
    cid = model_config_ref.get("model_config_id") or DEFAULT_CONFIG_ID
    ver = model_config_ref.get("version")
    cfg = get_config(conn, cid, ver)
    vlm = cfg["vlm"]
    return {
        "provider": vlm.get("provider", ""),
        "model": vlm.get("model", ""),
        "base_url": vlm.get("base_url", ""),
        "api_key_ref": vlm.get("api_key_ref", ""),
    }


def resolve_key(ref: str | None) -> str:
    """服务端专用：按引用名解析真实密钥（绝不返回给接口层）。"""
    return security.resolve_secret(ref)


def list_all_configs(conn: sqlite3.Connection) -> list[dict]:
    """列出所有模型配置（id + 最新版本 + 模型/接口），供前端选择。"""
    rows = conn.execute(
        "SELECT model_config_id, MAX(version) AS v FROM model_configs GROUP BY model_config_id"
    ).fetchall()
    out = []
    for r in rows:
        c = get_config(conn, r["model_config_id"], r["v"])
        out.append({"model_config_id": c["model_config_id"], "version": c["version"],
                    "vlm_model": c["vlm"].get("model", ""), "vlm_base_url": c["vlm"].get("base_url", ""),
                    "llm_model": c["llm"].get("model", ""), "provider": c["vlm"].get("provider", "")})
    return out


def provider_profiles(conn: sqlite3.Connection) -> list[dict]:
    """Read the latest saved endpoint per supplier across named configurations, never a secret value."""
    seen = set()
    profiles = []
    for row in conn.execute("SELECT * FROM model_configs ORDER BY created_at DESC, version DESC"):
        data = json.loads(row["data_json"])
        provider = data.get("vlm", {}).get("provider")
        if provider not in {"deepseek", "dashscope"} or provider in seen:
            continue
        seen.add(provider)
        profiles.append(_to_view(row["model_config_id"], row["version"], data, row["created_at"]))
    return profiles

"""素材登记与任务隔离：稳定 asset_id + 内容指纹；浏览器上传校验。"""
from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .. import security
from ..config import settings

MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2GB

_ALLOWED_SUFFIXES = {".mov", ".mp4", ".avi", ".mkv", ".m4v", ".webm", ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".heic"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".mov", ".mp4", ".avi", ".mkv", ".m4v", ".webm"}:
        return "video"
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".heic"}:
        return "image"
    raise security.SecurityError("asset_unsupported", f"不支持的文件类型：{suffix or '无扩展名'}")


def register_assets(conn: sqlite3.Connection, paths: list[str], *, allow_source_repair: bool = False) -> list[dict]:
    """登记素材：路径限界 + 存在性 + 类型 + 内容指纹 -> 稳定 asset_id。"""
    refs: list[dict] = []
    seen: set[str] = set()
    for p in paths:
        resolved = security.resolve_within_allowed(p, settings.allowed_asset_dirs)
        if not resolved.is_file():
            raise security.SecurityError("asset_not_found", f"文件不存在：{resolved}")
        digest = security.sha256_file(resolved)
        asset_id = security.derive_asset_id(digest)
        mtype = _media_type(resolved)
        size = resolved.stat().st_size
        conn.execute(
            """INSERT INTO assets(asset_id, source_path, fingerprint, media_type, size_bytes, analysis_version, license_json, created_at)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(asset_id) DO NOTHING""",
            (asset_id, str(resolved), digest, mtype, size, 1, "{}", _now()),
        )
        stored=conn.execute('SELECT source_path,fingerprint FROM assets WHERE asset_id=?',(asset_id,)).fetchone()
        if stored['fingerprint'] != digest:
            raise security.SecurityError('asset_identity_conflict','素材身份冲突，拒绝替换已有内容')
        canonical=Path(stored['source_path']);repaired=False
        if canonical!=resolved and allow_source_repair:
            valid=canonical.is_file() and security.sha256_file(canonical)==digest
            if not valid:
                from ..db import atomic
                with atomic(conn):
                    canonical=Path(conn.execute('SELECT source_path FROM assets WHERE asset_id=?',(asset_id,)).fetchone()[0])
                    if not canonical.is_file() or security.sha256_file(canonical)!=digest:
                        conn.execute('UPDATE assets SET source_path=? WHERE asset_id=?',(str(resolved),asset_id))
                        conn.execute('INSERT INTO asset_location_events VALUES(?,?,?,?,?,?,?)',
                                     (new_id('location'),asset_id,str(canonical),str(resolved),digest,'verified same-content reupload',_now()))
                        canonical=resolved;repaired=True
        if asset_id not in seen:
            refs.append({
                "asset_id": asset_id,
                "fingerprint": digest,
                "media_type": mtype,
                "size_bytes": size,
                "source_path": str(canonical),
                "source_repaired": repaired,
            })
            seen.add(asset_id)
    conn.commit()
    return refs


def get_asset(conn: sqlite3.Connection, asset_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
    return dict(row) if row else None


def verify_asset_integrity(conn: sqlite3.Connection, task_id: str) -> list[dict]:
    """重算任务锁定素材的当前指纹，与锁定时的指纹比对。"""
    rows = conn.execute(
        "SELECT ta.asset_id, ta.fingerprint_at_lock, a.source_path, a.fingerprint "
        "FROM task_assets ta JOIN assets a ON a.asset_id=ta.asset_id WHERE ta.task_id=?",
        (task_id,),
    ).fetchall()
    mismatches: list[dict] = []
    for r in rows:
        path = Path(r["source_path"])
        if not path.is_file():
            mismatches.append({"asset_id": r["asset_id"], "reason": "missing", "locked": r["fingerprint_at_lock"], "current": None})
            continue
        current = security.sha256_file(path)
        if current != r["fingerprint_at_lock"]:
            mismatches.append({"asset_id": r["asset_id"], "reason": "changed", "locked": r["fingerprint_at_lock"], "current": current})
    return mismatches


def check_export_asset(conn: sqlite3.Connection, task_id: str | None, asset_id: str) -> str:
    """导出前统一校验：素材存在、属于任务（若给定 task_id）、源文件存在且指纹与锁定一致。
    返回当前源文件路径；任何不一致抛 SecurityError（asset_mismatch / asset_not_found / asset_not_in_task）。"""
    asset = conn.execute("SELECT source_path, fingerprint FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
    if asset is None:
        raise security.SecurityError("asset_not_found", f"素材不存在：{asset_id}")
    locked = asset["fingerprint"]
    if task_id:
        row = conn.execute(
            "SELECT fingerprint_at_lock FROM task_assets WHERE task_id=? AND asset_id=?", (task_id, asset_id)
        ).fetchone()
        if row is None:
            raise security.SecurityError("asset_not_in_task", f"素材不属于该任务：{asset_id}")
        locked = row["fingerprint_at_lock"]
    path = Path(asset["source_path"])
    if not path.is_file():
        raise security.SecurityError("asset_mismatch", f"素材 {asset_id} 源文件不存在")
    if locked is not None:
        current = security.sha256_file(path)
        if current != locked:
            raise security.SecurityError("asset_mismatch", f"素材 {asset_id} 内容已改变，锁定指纹不一致")
    return str(path)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def safe_filename(name: str) -> str:
    """安全文件名：只取 basename，替换危险字符，避免路径穿越。"""
    base = Path(name).name
    base = re.sub(r"[^\w.\- ]", "_", base)
    base = base.strip().strip(".")
    return base or "upload.bin"


def validate_real_type(path: Path, media_type: str) -> None:
    """真实类型校验：视频须含视频流，图片须可解码（防改扩展名）。"""
    if media_type == "video":
        try:
            import av
            c = av.open(str(path))
            has_video = any(s.type == "video" for s in c.streams)
            c.close()
            if not has_video:
                raise security.SecurityError("asset_unsupported", "不是有效的视频文件（无视频流）")
        except security.SecurityError:
            raise
        except Exception as e:
            raise security.SecurityError("asset_unsupported", "无法读取视频（格式不支持或文件已损坏），请换一个文件重试") from e
    else:
        try:
            from PIL import Image
            img = Image.open(path)
            img.verify()
        except Exception as e:
            raise security.SecurityError("asset_unsupported", "不是有效的图片文件（无法解码或格式不受支持），请换一个文件重试") from e

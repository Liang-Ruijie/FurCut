"""安全相关：内容指纹、asset_id 派生、密钥脱敏、路径边界校验。"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from . import config


class SecurityError(Exception):
    """业务安全校验失败。携带错误码，供 API 层转为统一错误结构。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def derive_asset_id(digest_hex: str) -> str:
    """asset_id 由内容指纹派生，与路径/顺序无关。"""
    return "asset_" + digest_hex[:32]


def mask_secret(value: str | None) -> str:
    if not value:
        return ""
    return "***"  # 永不回显真实值


_SECRET_LIKE = re.compile(r'sk-[A-Za-z0-9]{20,}|AKLT[A-Za-z0-9]{10,}|AKIA[0-9A-Z]{16}', re.IGNORECASE)


def is_secret_like(value: str | None) -> bool:
    """判断传入值是否像真实密钥（而不是密钥引用名）。"""
    if not value:
        return False
    return bool(_SECRET_LIKE.search(value)) or len(value) > 64


def resolve_secret(ref: str | None) -> str:
    """按引用名从服务端环境解析真实密钥（仅在服务端使用，绝不进响应/日志/仓库）。"""
    if not ref:
        return ""
    return os.environ.get(ref, "")


def resolve_within_allowed(path: str | Path, allowed_dirs) -> Path:
    """限定素材导入目录，避免任意路径读取 / 路径穿越。"""
    raw = Path(path)
    candidate = raw.resolve()
    for d in allowed_dirs:
        base = Path(d).resolve()
        try:
            candidate.relative_to(base)
            return candidate
        except ValueError:
            continue
    raise SecurityError("asset_out_of_scope", "素材路径不在允许目录内")

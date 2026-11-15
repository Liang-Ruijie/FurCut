"""应用配置。仅监听本机；金额用微元（1元=1e6）；.env 从 backend/ 加载到进程环境。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent


def load_env(env_path: Path | str | None = None) -> None:
    """把 .env 内容加载进进程环境（不覆盖已有环境变量），使 resolve_secret 能按引用名解析。"""
    p = Path(env_path) if env_path else BACKEND_DIR / ".env"
    if p.exists():
        load_dotenv(p, override=False)


def _abs(p: str) -> str:
    """相对路径统一相对于项目根目录解析，避免从不同目录启动生成不同数据库。"""
    path = Path(p).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return str(path.resolve())


# 模块导入即加载 backend/.env（无此文件时静默跳过）
load_env()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(BACKEND_DIR / ".env"), extra="ignore")

    # 单用户本地服务默认仅监听本机
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    # One private workspace, two roles; no tenant/account platform implied.
    app_access_mode: str = "local"
    app_user_token: str = ""
    app_admin_token: str = ""
    app_public_origin: str = ""
    app_cookie_secure: bool = True
    # Lifetime ledger ceiling across tasks; increase the ceiling rather than resetting costs.
    app_workspace_budget_limit_minor: int | None = None

    # SQLite 数据文件（相对值按项目根解析）
    app_db_path: str = str(PROJECT_ROOT / "data" / "app.db")
    app_output_dir: str = str(PROJECT_ROOT / "data" / "review" / "导出")

    @property
    def output_dir(self) -> Path:
        return Path(_abs(self.app_output_dir))

    # 素材导入限定目录（逗号分隔；相对值按项目根解析）
    app_allowed_asset_dirs: str = str(PROJECT_ROOT / "data" / "assets")

    @property
    def db_path(self) -> Path:
        return Path(_abs(self.app_db_path))

    @property
    def allowed_asset_dirs(self) -> List[Path]:
        return [Path(_abs(p)) for p in self.app_allowed_asset_dirs.split(",") if p.strip()]


settings = Settings()

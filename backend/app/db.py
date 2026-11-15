"""SQLite 连接与初始化。WAL + busy_timeout + 外键。"""
from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = "6"

_SCHEMA_SQL: str | None = None


def _schema_text() -> str:
    global _SCHEMA_SQL
    if _SCHEMA_SQL is None:
        _SCHEMA_SQL = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
    return _SCHEMA_SQL


def get_readonly_conn(db_path: str | Path) -> sqlite3.Connection:
    """Open existing DB without changing journal mode, schema or creating directories."""
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True,
                           isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def get_conn(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def atomic(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE 抢占写锁，串行化读改写；异常回滚。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db(db_path: str | Path) -> None:
    # Snapshot the consistent old DB before any schema-version upgrade. Raw file copying
    # is unsafe with WAL; SQLite backup includes committed WAL pages.
    path = Path(db_path)
    if path.is_file() and path.stat().st_size:
        source = get_readonly_conn(path)
        try:
            old = source.execute("SELECT 1 FROM sqlite_master WHERE name='model_requests'").fetchone()
            version = source.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() if source.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone() else None
            if not old or not version or version[0] != SCHEMA_VERSION:
                from datetime import datetime, timezone
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                backup_dir = path.parent / "migration-backups"
                backup_dir.mkdir(exist_ok=True)
                backup = sqlite3.connect(backup_dir / (path.name + f".before-v{SCHEMA_VERSION}-" + stamp + ".sqlite"))
                try:
                    source.backup(backup)
                    if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("迁移前备份完整性检查失败")
                finally:
                    backup.close()
        finally:
            source.close()
    conn = get_conn(db_path)
    try:
        conn.executescript(_schema_text())
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """轻量增量迁移：为旧库补齐新列。无数据时建表已包含新列，这里只做兜底。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    execution_cols={r['name'] for r in conn.execute('PRAGMA table_info(executions)')}
    if 'coverage_plan_json' not in execution_cols:
        conn.execute('ALTER TABLE executions ADD COLUMN coverage_plan_json TEXT')
    if "client_request_id" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN client_request_id TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_client_request_id ON tasks(client_request_id)")
    if "request_digest" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN request_digest TEXT")
    if "requirement" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN requirement TEXT")
    cols_ar = {r["name"] for r in conn.execute("PRAGMA table_info(analysis_runs)").fetchall()}
    if "execution_id" not in cols_ar:
        conn.execute("ALTER TABLE analysis_runs ADD COLUMN execution_id TEXT")
    cols_cr = {r["name"] for r in conn.execute("PRAGMA table_info(cost_records)").fetchall()}
    if "execution_id" not in cols_cr:
        conn.execute("ALTER TABLE cost_records ADD COLUMN execution_id TEXT")
    if "reservation_minor" not in cols_cr:
        conn.execute("ALTER TABLE cost_records ADD COLUMN reservation_minor INTEGER")
    if "call_source" not in cols_cr:
        conn.execute("ALTER TABLE cost_records ADD COLUMN call_source TEXT NOT NULL DEFAULT 'legacy'")
    # export_jobs/export_items 旧结构（无 batch_digest）→ 保留数据迁移，不删除历史
    has_ej = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='export_jobs'").fetchone()
    if has_ej:
        cols_ej = {r["name"] for r in conn.execute("PRAGMA table_info(export_jobs)").fetchall()}
        if "batch_digest" not in cols_ej:
            _migrate_export_tables_preserving(conn)
    # analysis_jobs 新增 mode 列（默认 mock，真实须显式选择）
    has_aj = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='analysis_jobs'").fetchone()
    if has_aj:
        cols_aj = {r["name"] for r in conn.execute("PRAGMA table_info(analysis_jobs)").fetchall()}
        if "mode" not in cols_aj:
            conn.execute("ALTER TABLE analysis_jobs ADD COLUMN mode TEXT NOT NULL DEFAULT 'mock'")
    # export_items 新增 meta_json（来源/版本/边界提示）
    has_ei = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='export_items'").fetchone()
    if has_ei:
        cols_ei = {r["name"] for r in conn.execute("PRAGMA table_info(export_items)").fetchall()}
        if "meta_json" not in cols_ei:
            conn.execute("ALTER TABLE export_items ADD COLUMN meta_json TEXT")
    # photo_checks 结构变更（旧结构无 check_version/status）：保留历史，新增列并标 legacy，不 DROP
    has_pc = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='photo_checks'").fetchone()
    if has_pc:
        cols_pc = {r["name"] for r in conn.execute("PRAGMA table_info(photo_checks)").fetchall()}
        if "check_version" not in cols_pc:
            conn.execute("ALTER TABLE photo_checks ADD COLUMN check_version TEXT NOT NULL DEFAULT 'legacy'")
            conn.execute("ALTER TABLE photo_checks ADD COLUMN status TEXT NOT NULL DEFAULT 'pass'")
            conn.execute("UPDATE photo_checks SET status = CASE WHEN quality_pass=1 THEN 'pass' WHEN quality_pass=0 THEN 'fail' ELSE 'error' END")
        if "retry_count" not in cols_pc:
            conn.execute("ALTER TABLE photo_checks ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0")
        if "check_kind" not in cols_pc:
            conn.execute("ALTER TABLE photo_checks ADD COLUMN check_kind TEXT NOT NULL DEFAULT 'photo'")
        if "tier" not in cols_pc:
            conn.execute("ALTER TABLE photo_checks ADD COLUMN tier TEXT NOT NULL DEFAULT 'feature'")
            # 历史 fail 记录（旧结构）归为 fail 档
            conn.execute("UPDATE photo_checks SET tier='fail' WHERE status='fail' AND quality_pass=0")
    # manual_reviews 批次绑定：受审候选所属分析批次；历史未绑定（NULL）→ 待关联，不自动套用
    has_mr = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='manual_reviews'").fetchone()
    if has_mr:
        cols_mr = {r["name"] for r in conn.execute("PRAGMA table_info(manual_reviews)").fetchall()}
        if "execution_id" not in cols_mr:
            conn.execute("ALTER TABLE manual_reviews ADD COLUMN execution_id TEXT")
            # 历史评价保持未绑定；必须以源帧证据显式关联，禁止按最近批次猜测。
    if "evidence_json" not in {r["name"] for r in conn.execute("PRAGMA table_info(photo_checks)")}:
        conn.execute("ALTER TABLE photo_checks ADD COLUMN evidence_json TEXT")
    if "request_meta_json" not in {r["name"] for r in conn.execute("PRAGMA table_info(model_requests)")}:
        conn.execute("ALTER TABLE model_requests ADD COLUMN request_meta_json TEXT")
    for column, definition in {
        "judgment_version": "TEXT NOT NULL DEFAULT 'legacy'",
        "input_digest": "TEXT", "status": "TEXT NOT NULL DEFAULT 'complete'",
        "error": "TEXT", "input_json": "TEXT", "assessment_json": "TEXT"
    }.items():
        if column not in {r["name"] for r in conn.execute("PRAGMA table_info(video_evals)")}:
            conn.execute(f"ALTER TABLE video_evals ADD COLUMN {column} {definition}")
    conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (SCHEMA_VERSION,))
    conn.commit()


def _migrate_export_tables_preserving(conn: sqlite3.Connection) -> None:
    """旧 export_jobs/export_items → 新结构，保留全部历史数据与文件关联。"""
    conn.execute("ALTER TABLE export_jobs RENAME TO export_jobs_old")
    conn.execute("ALTER TABLE export_items RENAME TO export_items_old")
    conn.executescript(_schema_text())  # 重建新表（IF NOT EXISTS 幂等）

    def _key(kind: str, asset_id: str, t_ms, start_ms, end_ms) -> str:
        if kind == "photo":
            return f"photo:{asset_id}:{t_ms}"
        return f"video:{asset_id}:{start_ms}:{end_ms}"

    for j in conn.execute("SELECT * FROM export_jobs_old").fetchall():
        items = conn.execute("SELECT * FROM export_items_old WHERE export_job_id=?", (j["export_job_id"],)).fetchall()
        keys = sorted(_key(r["kind"], r["asset_id"], r["t_ms"], r["start_ms"], r["end_ms"]) for r in items)
        digest = hashlib.sha256((j["task_id"] + "\n" + "\n".join(keys)).encode()).hexdigest()[:16]
        conn.execute(
            "INSERT INTO export_jobs(export_job_id,task_id,batch_digest,status,total_items,done_items,failed_items,manifest_file,error_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (j["export_job_id"], j["task_id"], digest, j["status"], j["total_items"], j["done_items"], j["failed_items"],
             j["manifest_file"], j["error_json"], j["created_at"], j["updated_at"]),
        )
        seen: set[str] = set()
        for r in items:
            key = _key(r["kind"], r["asset_id"], r["t_ms"], r["start_ms"], r["end_ms"])
            new_id = f"{j['export_job_id']}:{key}"
            if new_id in seen:  # 同参数不同来源的历史条目：加候选后缀避免丢失
                new_id = f"{new_id}:{r['candidate_id'][:20]}"
            seen.add(new_id)
            conn.execute(
                "INSERT INTO export_items(export_item_id,export_job_id,task_id,kind,asset_id,candidate_id,t_ms,start_ms,end_ms,status,file_name,error,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_id, j["export_job_id"], j["task_id"], r["kind"], r["asset_id"], r["candidate_id"],
                 r["t_ms"], r["start_ms"], r["end_ms"], r["status"], r["file_name"], r["error"], r["updated_at"]),
            )
    conn.execute("DROP TABLE export_items_old")
    conn.execute("DROP TABLE export_jobs_old")

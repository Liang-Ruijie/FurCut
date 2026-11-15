-- AI Video 猫舍批量选片与选图 · schema v1
-- 金额一律用微元（1 元 = 1,000,000 微元）INTEGER，可表达低于 1 分的成本；时间用 ISO-8601 文本。

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assets (
    asset_id         TEXT PRIMARY KEY,
    source_path      TEXT NOT NULL,
    fingerprint      TEXT NOT NULL,
    media_type       TEXT NOT NULL,           -- video | image
    size_bytes       INTEGER NOT NULL,
    analysis_version INTEGER NOT NULL DEFAULT 1,
    license_json     TEXT NOT NULL DEFAULT '{}',
    created_at       TEXT NOT NULL
);

-- Append-only storage recovery audit; the content identity and locked task hash never change.
CREATE TABLE IF NOT EXISTS asset_location_events (
    event_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES assets(asset_id),
    previous_path TEXT NOT NULL,
    restored_path TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifact_receipts (
    file_name TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id              TEXT PRIMARY KEY,
    client_request_id    TEXT UNIQUE,          -- 任务创建幂等键（可选）
    request_digest       TEXT,                 -- 创建请求的规范化参数摘要（幂等校验用）
    requirement          TEXT,                 -- 自然语言需求（可选，影响后续分析与筛选）
    status               TEXT NOT NULL,       -- created|assets_locked|analyzing|selecting|exporting|ready_for_review|completed|failed|cancelled
    outcome              TEXT,                -- complete|partial|none_qualified|incomplete_error|incomplete_budget|null
    error_json           TEXT,
    resume_point         TEXT,
    preference_ref_json  TEXT,
    model_config_ref_json TEXT,
    snapshot_ref_json    TEXT,
    business_ref_json    TEXT,
    budget_limit_minor   INTEGER,             -- NULL = 未配置（禁止付费调用）
    budget_settled_minor INTEGER NOT NULL DEFAULT 0,
    budget_reserved_minor INTEGER NOT NULL DEFAULT 0,
    budget_unknown_minor INTEGER NOT NULL DEFAULT 0,
    budget_currency      TEXT NOT NULL DEFAULT 'CNY',
    budget_status        TEXT NOT NULL DEFAULT 'unconfigured',  -- unconfigured|open|exceeded|ok
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_assets (
    task_id             TEXT NOT NULL,
    asset_id            TEXT NOT NULL,
    role                TEXT NOT NULL DEFAULT 'primary',
    fingerprint_at_lock TEXT NOT NULL,
    PRIMARY KEY (task_id, asset_id),
    FOREIGN KEY (task_id)  REFERENCES tasks(task_id),
    FOREIGN KEY (asset_id) REFERENCES assets(asset_id)
);

CREATE TABLE IF NOT EXISTS model_configs (
    model_config_id TEXT NOT NULL,
    version         INTEGER NOT NULL,
    data_json       TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (model_config_id, version)
);

CREATE TABLE IF NOT EXISTS preferences (
    preference_id TEXT NOT NULL,
    version       INTEGER NOT NULL,
    data_json     TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (preference_id, version)
);

CREATE TABLE IF NOT EXISTS cost_records (
    cost_id         TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL,
    execution_id    TEXT,                     -- 同一全片执行的所有费用关联到同一 execution
    provider        TEXT,
    model           TEXT,
    operation       TEXT,
    usage_json      TEXT NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL,            -- reserved|settled|unknown
    currency        TEXT NOT NULL DEFAULT 'CNY',
    amount_minor    INTEGER NOT NULL,         -- 微元
    estimate_basis  TEXT NOT NULL DEFAULT 'reserved',  -- reserved|invoice|unknown
    request_id      TEXT NOT NULL UNIQUE,     -- 幂等键
    retry_of        TEXT,
    cache_hit       INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);

CREATE TABLE IF NOT EXISTS analysis_runs (
    run_id               TEXT PRIMARY KEY,
    task_id              TEXT NOT NULL,
    asset_id             TEXT NOT NULL,
    cache_key            TEXT NOT NULL,
    execution_id         TEXT,                 -- 同一全片执行的所有 run 关联到同一 execution
    status               TEXT NOT NULL,       -- complete|failed|running
    source               TEXT NOT NULL,       -- mock|real
    preference_ref_json  TEXT,
    model_config_ref_json TEXT,
    sampling_json        TEXT,
    annotations_json     TEXT,
    selections_json      TEXT,
    coverage_json        TEXT,
    cost_id              TEXT,
    created_at           TEXT NOT NULL,
    UNIQUE(task_id, asset_id, cache_key),
    FOREIGN KEY (task_id) REFERENCES tasks(task_id),
    FOREIGN KEY (asset_id) REFERENCES assets(asset_id)
);

CREATE TABLE IF NOT EXISTS executions (
    execution_id  TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL,
    asset_id      TEXT NOT NULL,
    status        TEXT NOT NULL,               -- running|complete|failed
    coverage_plan_json TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);

CREATE TABLE IF NOT EXISTS batch_members (
    execution_id  TEXT NOT NULL,
    run_id        TEXT NOT NULL,
    PRIMARY KEY (execution_id, run_id),
    FOREIGN KEY (execution_id) REFERENCES executions(execution_id),
    FOREIGN KEY (run_id) REFERENCES analysis_runs(run_id)
);

CREATE TABLE IF NOT EXISTS feedback_events (
    feedback_id  TEXT PRIMARY KEY,
    task_id      TEXT,
    asset_id     TEXT,
    run_id       TEXT,
    kind         TEXT NOT NULL,               -- relative_preference|video_rating|note
    payload_json TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id),
    FOREIGN KEY (asset_id) REFERENCES assets(asset_id)
);

CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    identity      TEXT NOT NULL UNIQUE,       -- 输入(含图片指纹)+提示词+模型配置/参数+分辨率+模式
    task_id       TEXT,
    mode          TEXT,                       -- photo|video
    experiment    TEXT,                       -- A|B
    status        TEXT NOT NULL,              -- running|complete|failed|interrupted
    attempt       INTEGER NOT NULL DEFAULT 0, -- 执行尝试编号
    result_json   TEXT,
    error_json    TEXT,
    cost_json     TEXT,                       -- 本次已产生费用 cost_id 列表
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS export_jobs (
    export_job_id  TEXT PRIMARY KEY,
    task_id        TEXT NOT NULL,
    batch_digest   TEXT NOT NULL,             -- 本批导出参数摘要（任务+素材+有效边界/时点）
    status         TEXT NOT NULL,             -- running|partial|completed|failed
    total_items    INTEGER NOT NULL DEFAULT 0,
    done_items     INTEGER NOT NULL DEFAULT 0,
    failed_items   INTEGER NOT NULL DEFAULT 0,
    manifest_file  TEXT,
    error_json     TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);
CREATE INDEX IF NOT EXISTS idx_export_jobs_task ON export_jobs(task_id, created_at);

CREATE TABLE IF NOT EXISTS export_items (
    export_item_id  TEXT PRIMARY KEY,         -- job:kind:asset:params（导出参数身份）
    export_job_id   TEXT NOT NULL,
    task_id         TEXT NOT NULL,
    kind            TEXT NOT NULL,            -- photo|video
    asset_id        TEXT NOT NULL,
    candidate_id    TEXT NOT NULL,            -- 引用来源候选（展示/溯源）
    t_ms            INTEGER,
    start_ms        INTEGER,
    end_ms          INTEGER,
    status          TEXT NOT NULL,            -- pending|done|failed
    file_name       TEXT,
    error           TEXT,
    meta_json       TEXT,                     -- 来源/版本/边界提示等
    updated_at      TEXT NOT NULL,
    FOREIGN KEY (export_job_id) REFERENCES export_jobs(export_job_id),
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);
CREATE INDEX IF NOT EXISTS idx_export_items_job ON export_items(export_job_id);

CREATE TABLE IF NOT EXISTS analysis_jobs (
    analysis_job_id  TEXT PRIMARY KEY,
    task_id          TEXT NOT NULL,
    status           TEXT NOT NULL,           -- running|completed|failed
    mode             TEXT NOT NULL DEFAULT 'mock',  -- mock|real（真实调用须显式选择）
    stage            TEXT,                    -- 粗筛|细查|加密|完成
    asset_index      INTEGER NOT NULL DEFAULT 0,
    total_assets     INTEGER NOT NULL DEFAULT 0,
    current_asset_id TEXT,
    error            TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE(task_id),
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);

CREATE TABLE IF NOT EXISTS photo_checks (
    check_id      TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL,
    asset_id      TEXT NOT NULL,
    t_ms          INTEGER NOT NULL,
    check_version TEXT NOT NULL,              -- 质检提示词+模型配置版本摘要
    status        TEXT NOT NULL,              -- pass|fail|error(解析/调用/预算失败)
    quality_pass  INTEGER,                    -- 1 通过 / 0 不合格 / NULL=未知(error)
    issues_json   TEXT NOT NULL DEFAULT '[]',
    retry_count   INTEGER NOT NULL DEFAULT 0,  -- error 重试次数
    check_kind    TEXT NOT NULL DEFAULT 'photo',  -- photo=参考图相对比较（照片降级用）；video=单图质检（视频段修剪用）
    tier          TEXT NOT NULL DEFAULT 'feature',  -- 照片三档：feature(值得精选)/usable(普通可用)/fail(不合格)；视频单图无 tier 语义
    created_at    TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id),
    FOREIGN KEY (asset_id) REFERENCES assets(asset_id)
);

CREATE TABLE IF NOT EXISTS manual_reviews (
    review_id      TEXT PRIMARY KEY,
    task_id        TEXT NOT NULL,
    asset_id       TEXT NOT NULL,
    kind           TEXT NOT NULL,           -- photo|video
    t_ms           INTEGER,                 -- photo 时间点
    start_ms       INTEGER,                 -- video 区间
    end_ms         INTEGER,
    score          INTEGER,                 -- 用户评分 0-100
    decision       TEXT NOT NULL,           -- keep|reject|trim
    reason         TEXT,
    model_original TEXT,                    -- 模型原判断 JSON（promo/aes/harmony/clarity）
    review_version TEXT NOT NULL,           -- 本次审核版本
    execution_id   TEXT,                    -- 受审候选所属分析批次；NULL=待关联（历史未绑定批次）
    created_at     TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id),
    FOREIGN KEY (asset_id) REFERENCES assets(asset_id)
);

CREATE TABLE IF NOT EXISTS video_evals (
    eval_id        TEXT PRIMARY KEY,
    task_id        TEXT NOT NULL,
    asset_id       TEXT NOT NULL,
    execution_id   TEXT,
    start_ms       INTEGER NOT NULL,        -- 被评估的自然段原始起止
    end_ms         INTEGER NOT NULL,
    segment_usable INTEGER,                 -- 1 可用 / 0 不可用 / NULL=未知
    usable_start_ms INTEGER,                -- 连续判断后的自然起止
    usable_end_ms  INTEGER,
    reason         TEXT,
    cost_id        TEXT,
    created_at     TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id),
    FOREIGN KEY (asset_id) REFERENCES assets(asset_id)
);

-- Every real dispatch is durable before network I/O; unresolved requests block retries.
CREATE TABLE IF NOT EXISTS model_requests (
 request_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, execution_id TEXT,
 cost_id TEXT NOT NULL, status TEXT NOT NULL, input_digest TEXT NOT NULL,
 input_json TEXT NOT NULL, response_json TEXT, error TEXT,
 reconciliation_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

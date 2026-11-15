"""API 请求/响应 Pydantic 模型。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Literal

from pydantic import BaseModel, Field


class ErrorBody(BaseModel):
    code: str
    message: str


# ---------- assets ----------
class AssetRegisterRequest(BaseModel):
    paths: List[str] = Field(min_length=1)


class AssetRef(BaseModel):
    asset_id: str
    fingerprint: str
    media_type: str
    size_bytes: int
    source_path: str


class AssetRegisterResponse(BaseModel):
    asset_refs: List[AssetRef]


# ---------- tasks ----------
class TaskCreateRequest(BaseModel):
    asset_ids: List[str] = Field(min_length=1)
    client_request_id: Optional[str] = None   # 幂等键（可选）；重复提交返回原任务
    requirement: Optional[str] = None         # 自然语言需求（可选，影响分析与筛选）
    preference_ref: Optional[Dict[str, Any]] = None
    model_config_ref: Optional[Dict[str, Any]] = None
    budget: Optional[Dict[str, Any]] = None
    business_ref: Optional[Dict[str, Any]] = None


class BudgetView(BaseModel):
    currency: str
    limit: Optional[int] = None
    settled: int
    reserved_inflight: int
    unknown: int
    status: str


class TaskView(BaseModel):
    task_id: str
    status: str
    outcome: Optional[str] = None
    error: Optional[Dict[str, Any]] = None
    resume_point: Optional[str] = None
    asset_refs: List[Dict[str, Any]]
    preference_ref: Optional[Dict[str, Any]] = None
    model_config_ref: Optional[Dict[str, Any]] = None
    snapshot_ref: Optional[Dict[str, Any]] = None
    budget: BudgetView
    business_ref: Optional[Dict[str, Any]] = None
    created_at: str
    updated_at: str


# ---------- model config ----------
class ModelEndpoint(BaseModel):
    provider: str = ""
    model: str = ""
    base_url: str = ""
    api_key_ref: str = ""          # 服务端密钥引用名（如环境变量名），不是真实密钥
    key_configured: bool = False   # 响应层派生：该引用能否在服务端解析到密钥


class SamplingConfig(BaseModel):
    model_config = {"extra":"forbid"}
    max_frames: int = Field(default=0, ge=0, le=120)
    frames_per_sec: float = Field(default=0.0, ge=0, le=30)
    resize_edge: int = Field(default=0, ge=0, le=4096)
    coarse_max_frames: int = Field(default=120, ge=1, le=2000)
    coarse_fps: float = Field(default=0.5, gt=0, le=30)
    coarse_resize_edge: int = Field(default=600, ge=64, le=4096)
    fine_fps: Optional[float] = Field(default=None, gt=0, le=30)
    fine_max_frames: Optional[int] = Field(default=None, ge=1, le=120)
    photo_peak_max_windows: int = Field(default=4, ge=0, le=12)
    photo_peak_fps: float = Field(default=30, gt=0, le=60)
    photo_peak_max_frames: int = Field(default=16, ge=1, le=32)
    quality_contract_version: Literal[1, 3] = 1  # 3 is experimental, not quality-accepted.
    densify_fps: Optional[float] = Field(default=None, gt=0, le=30)
    densify_max_frames: Optional[int] = Field(default=None, ge=1, le=120)
    boundary_max_windows: int = Field(default=8, ge=0, le=100)
    uncertain_max_windows: int = Field(default=4, ge=0, le=100)
    discard_audit_max_windows: int = Field(default=3, ge=0, le=100)


class ModelConfigSaveRequest(BaseModel):
    model_config_id: Optional[str] = None
    llm: ModelEndpoint = ModelEndpoint()
    vlm: ModelEndpoint = ModelEndpoint()
    sampling: SamplingConfig = SamplingConfig()


class ModelConfigView(BaseModel):
    model_config_id: str
    version: int
    llm: ModelEndpoint
    vlm: ModelEndpoint
    sampling: SamplingConfig
    created_at: str


# ---------- preferences ----------
class PreferenceNameRequest(BaseModel):
    display_name: str = Field(default="", max_length=12)


class PreferenceSaveRequest(BaseModel):
    display_name: str = Field(default="", max_length=12)
    preference_id: Optional[str] = None
    quality_floor: Dict[str, Any] = Field(default_factory=lambda: {"required": ["基本可见", "无严重模糊/遮挡"]})
    visual_rules: Dict[str, Any] = Field(default_factory=dict)
    export_defaults: Dict[str, Any] = Field(default_factory=lambda: {
        "best_frame_hold_ms": 300,
        "best_frame_enabled": True,
        "prefer_continuous": True,
        "typical_segment_count": "2-3（非硬限）",
    })


class PreferenceView(BaseModel):
    display_name: str = ""
    preference_id: str
    version: int
    quality_floor: Dict[str, Any]
    visual_rules: Dict[str, Any]
    export_defaults: Dict[str, Any]
    created_at: str


# ---------- cost ----------
class ReserveRequest(BaseModel):
    task_id: str
    amount_minor: int = Field(gt=0, description="人民币微元，1元=1,000,000微元；此处为预留上界")
    provider: str = ""
    model: str = ""
    operation: str = ""
    request_id: str
    retry_of: Optional[str] = None
    cache_hit: bool = False


class SettleRequest(BaseModel):
    actual_amount_minor: int = Field(ge=0, description="人民币微元，1元=1,000,000微元；按已核对账单填写，不得拿预留额冒充")
    usage: Dict[str, Any] = Field(default_factory=dict)


class UnknownRequest(BaseModel):
    note: str = ""


class CostView(BaseModel):
    cost_id: str
    task_id: str
    provider: str
    model: str
    operation: str
    status: str
    currency: str
    amount_minor: int = Field(description="人民币微元，1元=1,000,000微元")
    estimate_basis: str
    request_id: str
    cache_hit: bool
    created_at: str


# ---------- budget adjust ----------
class BudgetAdjustRequest(BaseModel):
    limit_minor: Optional[int] = Field(default=None, gt=0)


# ---------- export ----------
class ExportVideoRequest(BaseModel):
    candidate_id: Optional[str] = None
    task_id: str
    asset_id: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)


class ExportPhotoRequest(BaseModel):
    candidate_id: Optional[str] = None
    task_id: str
    asset_id: str
    t_ms: int = Field(ge=0)
    source_pts: Optional[int] = Field(default=None,ge=0,strict=True)
    time_base: Optional[str] = None


class ExportManifestRequest(BaseModel):
    task_id: str
    items: List[Dict[str, Any]] = Field(default_factory=list)


class BatchExportRequest(BaseModel):
    task_id: str
    items: List[Dict[str, Any]] = Field(default_factory=list)


class TaskSettingsRequest(BaseModel):
    preference_ref: Dict[str, Any]
    requirement: str = Field(default='', max_length=4000)
    best_frame_enabled: bool
    best_frame_hold_ms: int = Field(ge=0, le=5000)
    asset_ids: Optional[List[str]] = Field(default=None, min_length=1)
    expected_revision: Optional[str] = None

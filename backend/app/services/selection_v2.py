"""新选片主线 v3（可切换，先 mock 验证，不替换旧路径）。

一条独立选片主线：
    采样抽帧 → 事实/价值两步评分 → 好看程度曲线 → 双识别（尖峰+平台）
    → 边界（动作连贯 + 边界外上下文 + 内部坏画面/坏尾）→ 精选优先展示（1–3 + 备选）。

设计约束（对应改造方案与锁定口径）：
- 本文件是新增路径，不删除、不 import 旧选片决策代码（analysis/fullscan/compare/…）。
- 每个任务通过 snapshot 里的 `selection_pipeline_version == 3` 锁定本路径；
  旧任务版本 1/2 走旧路径，新旧不混用。
- `SELECTION_V2=1` 只控制「新建任务默认走哪条路径」，不影响历史任务。
- mock 只用于离线跑通与可回放验证，不冒充真实质量；real 两步调用在阶段④单独接线。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PIPELINE_VERSION = 3
EXPERIMENTAL = True  # 收口：v3 仅作实验能力；旧路径仍是默认与回退
DEFAULT_CURATED_PHOTOS = 3
DEFAULT_CURATED_VIDEOS = 3

# 事实/价值两步提示词（real 阶段接线用；本阶段不发起网络调用）。
FACT_PROMPT = (
    "只描述图像中实际可见的事实，不评价可爱、好看或宣传价值。\n"
    "对每个 frame_id 输出观察：face_visible(full|side|cropped|occluded|unclear)、"
    "eye_state(open|half|closed|blinking|unclear)、clarity(sharp|soft|blurry)、"
    "posture_quality(自然|别扭|其他|unclear)、expression_natural(true|false)。\n"
    "不知道就写 unclear，不得编造。"
)
VALUE_PROMPT = (
    "在上一步事实观察的基础上，判断单帧宣传价值。技术可用不等于值得精选。\n"
    "对每个 frame_id 输出 aes_score(0-1)、promo_worthy(true|false)、promo_reason(≤80字)。\n"
    "promo_worthy=true 只给明显好看、能独立交付的瞬间；基本可用但不够出彩标 false。\n"
    "静态、完整侧脸可以优秀；不把正脸、圆眼、抬爪当作自动加分的捷径。"
)


def default_pipeline_version() -> int:
    """新任务默认算法版本：SELECTION_V2=1 时锁 v3，否则保持旧路径 v2。"""
    return PIPELINE_VERSION if os.environ.get("SELECTION_V2") == "1" else 2


def is_enabled() -> bool:
    return default_pipeline_version() == PIPELINE_VERSION


@dataclass
class SelectParams:
    """v3 选片阈值。仅本路径使用，与旧 analysis.SelectParams 相互独立。"""
    spike_floor: float = 0.85          # 尖峰绝对分数下限
    spike_delta: float = 0.15          # 尖峰须明显高于邻近基线
    spike_window_ms: int = 1500        # 计算局部基线用的邻域半径
    plateau_floor: float = 0.70        # 平台（持续好看）分数下限
    plateau_min_ms: int = 1200         # 平台最短持续时间
    bridge_ms: int = 1500              # 平台内允许桥接的短弱帧（非硬坏）
    gap_ms: int = 2000                 # 片段内部最大时间间隙
    min_segment_ms: int = 700          # 短但惊艳瞬间的保护下限
    photo_hard_floor: float = 0.70     # 照片候选门槛
    curated_photos: int = DEFAULT_CURATED_PHOTOS
    curated_videos: int = DEFAULT_CURATED_VIDEOS
    dedup_window_ms: int = 500         # 照片同窗去重；配合画面签名，避免把不同精彩瞬间合并


def _obs(f: dict, key: str) -> str | None:
    v = (f.get("observations") or {}).get(key)
    return str(v).strip() if v not in (None, "") else None


def _face(f: dict) -> str | None:
    v = _obs(f, "face_visible")
    if v in ("full", "front", "正脸"):
        return "full"
    if v in ("side", "partial", "侧脸", "profile"):
        return "side"
    if v in ("cropped", "cut", "裁切"):
        return "cropped"
    if v in ("occluded", "face_blocked", "blocked", "遮挡"):
        return "occluded"
    return None


def is_hard_bad(f: dict) -> bool:
    """硬坏：脸部裁切/遮挡、严重模糊、姿态别扭。这些是客观问题，不因整体高分桥接。"""
    face = _face(f)
    if face in ("cropped", "occluded"):
        return True
    if _obs(f, "clarity") in ("blurry", "模糊"):
        return True
    if (f.get("posture_quality") == "别扭"):
        return True
    return False


def _aes(f: dict) -> float:
    try:
        return float(f.get("aes_score", 0.0))
    except (TypeError, ValueError):
        return 0.0


def build_curve(annotations: list[dict]) -> list[dict]:
    """按时间排序的「好看程度」曲线。"""
    out = []
    for f in sorted(annotations, key=lambda x: x.get("t_ms", 0)):
        out.append({"t_ms": f.get("t_ms", 0), "score": _aes(f), "frame_id": f.get("frame_id")})
    return out


def detect_spikes(annotations: list[dict], params: SelectParams | None = None) -> list[dict]:
    """尖峰：分数突然拔高、明显高于邻近基线的瞬间。

    保护精彩短瞬间：尖峰本身独立成候选，不因短而删；短惊艳会在边界阶段单独保护。
    """
    params = params or SelectParams()
    ann = sorted(annotations, key=lambda f: f.get("t_ms", 0))
    if not ann:
        return []
    scores = [_aes(f) for f in ann]
    times = [f.get("t_ms", 0) for f in ann]
    out: list[dict] = []
    for i, f in enumerate(ann):
        s = scores[i]
        if s < params.spike_floor:
            continue
        # 邻域基线：忽略当前点，取窗口内分数的中位数，体现「相对周围突然拔高」
        neighbors = [scores[j] for j in range(len(ann))
                     if j != i and abs(times[j] - times[i]) <= params.spike_window_ms]
        baseline = sorted(neighbors)[len(neighbors) // 2] if neighbors else 0.0
        strength = round(s - baseline, 4)
        if s >= params.spike_floor and strength >= params.spike_delta:
            out.append({**f, "spike_strength": strength, "spike_baseline": baseline, "kind": "spike"})
    return out


def detect_plateaus(annotations: list[dict], params: SelectParams | None = None) -> list[dict]:
    """平台：分数持续较高、值得连续观看的区间。

    规则：硬坏必须打断；短弱帧（非硬坏）在 bridge_ms 内可桥接；
    只由「持续好看」形成平台，不把孤立高分冒充平台。
    """
    params = params or SelectParams()
    ann = sorted(annotations, key=lambda f: f.get("t_ms", 0))
    if not ann:
        return []
    roles: list[str] = []
    for f in ann:
        if is_hard_bad(f):
            roles.append("bad")
        elif _aes(f) >= params.plateau_floor:
            roles.append("good")
        else:
            roles.append("weak")

    # 桥接短弱帧（眨眼/一瞬不自然）；硬坏不桥接
    n = len(ann)
    bridged = list(roles)
    i = 0
    while i < n:
        if bridged[i] == "weak":
            j = i
            while j < n and bridged[j] == "weak":
                j += 1
            prev_t = ann[i - 1]["t_ms"] if i > 0 else ann[i]["t_ms"]
            next_t = ann[j]["t_ms"] if j < n else ann[j - 1]["t_ms"]
            if next_t - prev_t <= params.bridge_ms:
                for k in range(i, j):
                    bridged[k] = "good"
            i = j
        else:
            i += 1

    out: list[dict] = []
    i = 0
    while i < n:
        if bridged[i] != "good":
            i += 1
            continue
        j = i
        while j < n and bridged[j] == "good":
            if j > i and ann[j]["t_ms"] - ann[j - 1]["t_ms"] > params.gap_ms:
                break
            j += 1
        run = ann[i:j]
        dur = run[-1]["t_ms"] - run[0]["t_ms"]
        if dur >= params.plateau_min_ms and any(_aes(f) >= params.plateau_floor for f in run):
            out.append({"start_ms": run[0]["t_ms"], "end_ms": run[-1]["t_ms"],
                        "frames": run, "path": "plateau", "status": "candidate",
                        "reason": "持续好看区间"})
        i = j
    return out


def _merge_intervals(intervals: list[dict], gap_ms: int) -> list[dict]:
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda s: s["start_ms"])
    merged: list[dict] = []
    for s in ordered:
        if merged and s["start_ms"] <= merged[-1]["end_ms"] + gap_ms:
            merged[-1]["end_ms"] = max(merged[-1]["end_ms"], s["end_ms"])
            merged[-1]["frames"].extend(s["frames"])
            merged[-1]["paths"] = sorted(set(merged[-1].get("paths", []) + [s["path"]]))
        else:
            merged.append({**s, "paths": [s["path"]]})
    return merged


def build_video_segments(annotations: list[dict], params: SelectParams | None = None) -> list[dict]:
    """把尖峰与平台合并成候选视频段，并按边界证据关闭坏头/坏尾。

    边界不只看分数低谷：内部硬坏必须剔除；候选段两侧的 ok 过渡帧允许保留，
    但跨越 gap 或硬坏不得拼接。短惊艳瞬间按 min_segment_ms 保护。
    """
    params = params or SelectParams()
    ann = sorted(annotations, key=lambda f: f.get("t_ms", 0))
    if not ann:
        return []
    spikes = detect_spikes(ann, params)
    plateaus = detect_plateaus(ann, params)

    candidates: list[dict] = []
    for s in spikes:
        candidates.append({"start_ms": s["t_ms"], "end_ms": s["t_ms"], "frames": [s],
                           "path": "spike", "status": "candidate", "reason": "突出瞬间"})
    candidates.extend(plateaus)

    merged = _merge_intervals(candidates, params.gap_ms)

    segments: list[dict] = []
    for m in merged:
        # 内部硬坏：必须在坏帧处切开，绝不拼坏画面
        frames = sorted(m["frames"], key=lambda f: f["t_ms"])
        pieces: list[list[dict]] = []
        cur: list[dict] = []
        for f in frames:
            if is_hard_bad(f):
                if cur:
                    pieces.append(cur)
                    cur = []
                continue
            if cur and f["t_ms"] - cur[-1]["t_ms"] > params.gap_ms:
                pieces.append(cur)
                cur = []
            cur.append(f)
        if cur:
            pieces.append(cur)

        for piece in pieces:
            if not piece:
                continue
            start, end = piece[0]["t_ms"], piece[-1]["t_ms"]
            good = [f for f in piece if _aes(f) >= params.plateau_floor]
            has_spike = any(f.get("kind") == "spike" for f in piece)
            # 短但惊艳：有尖峰即可保留；否则平台须有持续好核心
            dur = end - start
            if not has_spike and not good:
                continue
            if not has_spike and dur < params.plateau_min_ms:
                continue
            if dur < params.min_segment_ms and not has_spike:
                continue

            # 边界证据：候选段两侧的邻近帧角色
            idx_by_t = {f["t_ms"]: i for i, f in enumerate(ann)}
            si = idx_by_t.get(start)
            ei = idx_by_t.get(end)
            notes = []
            if si is not None and si > 0:
                prev = ann[si - 1]
                if not is_hard_bad(prev):
                    notes.append("前边界外为可过渡帧")
                else:
                    notes.append("前边界外为坏帧，已关闭")
            if ei is not None and ei < len(ann) - 1:
                nxt = ann[ei + 1]
                if not is_hard_bad(nxt):
                    notes.append("后边界外为可过渡帧")
                else:
                    notes.append("后边界外为坏帧，已关闭")
            segments.append({
                "asset_id": piece[0].get("asset_id") if piece else None,
                "start_ms": start,
                "end_ms": end,
                "frames": piece,
                "path": "spike+plateau" if (has_spike and len(piece) > 1) else ("spike" if has_spike else "plateau"),
                "status": "deliverable",
                "reason": "尖峰+持续区间" if (has_spike and len(piece) > 1) else ("突出瞬间" if has_spike else "持续好看区间"),
                "boundary_evidence": notes,
            })
    segments.sort(key=lambda s: s["start_ms"])
    return segments


def _photo_quality_v2(f: dict) -> float:
    """v2 照片质量：清晰度 × 脸部完整 × 姿态自然 × 综合分。硬坏直接 0。"""
    if is_hard_bad(f):
        return 0.0
    obs = f.get("observations") or {}
    clarity = {"sharp": 1.0, "soft": 0.4, "blurry": 0.0, "模糊": 0.0}.get(obs.get("clarity"), 0.6)
    face = {"full": 1.0, "side": 0.85, "cropped": 0.3, "occluded": 0.0}.get(obs.get("face_visible"), 0.6)
    posture = {"自然": 1.0, "其他": 0.6, "别扭": 0.0}.get(f.get("posture_quality"), 0.7)
    return clarity * face * posture * _aes(f)


def _photo_rank_key(f: dict):
    return (-_photo_quality_v2(f), f.get("t_ms", 0))


def _dedup_signature(f: dict) -> tuple:
    """画面签名：只取结构化事实字段，避免同一时刻不同措辞被当成不同帧。"""
    obs = f.get("observations") or {}
    return (obs.get("face_visible") or "", obs.get("eye_state") or "", obs.get("clarity") or "")


def dedup_similar_photos(photo_list: list[dict], window_ms: int | None = None) -> list[dict]:
    """同窗近似去重：固定时间桶 + 画面签名；同窗同签名只留最佳，不同姿态/表情/清晰度保留。"""
    window_ms = window_ms or SelectParams().dedup_window_ms
    buckets: dict[int, list[dict]] = {}
    for f in photo_list:
        b = int(f.get("t_ms", 0) or 0) // window_ms
        buckets.setdefault(b, []).append(f)
    out: list[dict] = []
    for b in sorted(buckets):
        by_sig: dict[tuple, dict] = {}
        for f in buckets[b]:
            sig = _dedup_signature(f)
            if sig not in by_sig or _photo_rank_key(f) < _photo_rank_key(by_sig[sig]):
                by_sig[sig] = f
        out.extend(by_sig.values())
    return out


def _video_rank_key(s: dict):
    aes = [_aes(f) for f in s.get("frames", [])]
    avg = sum(aes) / len(aes) if aes else 0.0
    return (avg, len(s.get("frames", [])), -(s["end_ms"] - s["start_ms"]))


def select_asset(annotations: list[dict], params: SelectParams) -> dict:
    """单素材独立选片：曲线、双识别、视频段、照片候选。不跨素材合并时间线。"""
    ann = sorted(annotations, key=lambda f: f.get("t_ms", 0))
    curve = build_curve(ann)
    spikes = detect_spikes(ann, params)
    plateaus = detect_plateaus(ann, params)
    video_segments = build_video_segments(ann, params)
    photo_candidates = [f for f in ann
                        if not is_hard_bad(f) and _obs(f, "clarity") in ("sharp", "清晰")
                        and _aes(f) >= params.photo_hard_floor]
    photo_candidates = dedup_similar_photos(photo_candidates, params.dedup_window_ms)
    photo_candidates.sort(key=_photo_rank_key)
    return {"curve": curve, "spikes": spikes, "plateaus": plateaus,
            "video_segments": video_segments, "photo_candidates": photo_candidates}


def select(annotations: list[dict], preference: dict | None = None,
           params: SelectParams | None = None) -> dict:
    """新路径统一选片入口：每素材独立选片 → 任务级精选排序（照片/视频分别 Top 1–3）。"""
    params = params or SelectParams()
    groups: dict[str, list[dict]] = {}
    for f in annotations:
        aid = f.get("asset_id") or "_"
        groups.setdefault(aid, []).append(f)

    all_photos: list[dict] = []
    all_videos: list[dict] = []
    curve: list[dict] = []
    spikes: list[dict] = []
    plateaus: list[dict] = []
    for aid, group in groups.items():
        r = select_asset(group, params)
        curve.extend(r["curve"])
        spikes.extend(r["spikes"])
        plateaus.extend(r["plateaus"])
        for p in r["photo_candidates"]:
            p2 = dict(p)
            p2["asset_id"] = aid
            all_photos.append(p2)
        for s in r["video_segments"]:
            s2 = dict(s)
            s2["asset_id"] = aid
            all_videos.append(s2)

    curve.sort(key=lambda c: c["t_ms"])
    spikes.sort(key=lambda s: s["t_ms"])
    plateaus.sort(key=lambda p: p["start_ms"])
    all_photos.sort(key=_photo_rank_key)
    all_videos.sort(key=_video_rank_key, reverse=True)

    # 照片精选：显式 promo_worthy=true 且明显好；基本可用进备选，不凑数
    curated_photos = [p for p in all_photos if p.get("promo_worthy") is True][:params.curated_photos]
    curated_pids = {(p["asset_id"], p.get("t_ms")) for p in curated_photos}
    alternative_photos = [p for p in all_photos if (p["asset_id"], p.get("t_ms")) not in curated_pids]

    curated_videos = all_videos[:params.curated_videos]
    alternative_videos = all_videos[params.curated_videos:]

    return {
        "pipeline_version": PIPELINE_VERSION,
        "curve": curve,
        "spikes": spikes,
        "plateaus": plateaus,
        "video_segments": all_videos,
        "curated_videos": curated_videos,
        "alternative_videos": alternative_videos,
        "photos": all_photos,
        "curated_photos": curated_photos,
        "alternative_photos": alternative_photos,
    }


# ---------------------------------------------------------------------------
# mock 两步评分：事实 → 价值。只用于离线跑通/可回放，不冒充真实质量。
# ---------------------------------------------------------------------------

def mock_annotate(frames: list[dict], scenario: dict | None = None) -> list[dict]:
    """确定性 mock 两步评分。

    scenario 可选字段：
      base: 默认分数（缺省 0.55）
      spikes: [t_ms, ...]         尖峰时刻
      plateaus: [[start,end], ...] 持续好看区间
      bad: [[start,end], ...]      硬坏区间（脸部遮挡/模糊）
      plateau_score: 平台分（缺省 0.80）
      spike_score: 尖峰分（缺省 0.95）

    无随机数，同一输入永远同一输出。
    """
    scenario = scenario or {}
    base = float(scenario.get("base", 0.55))
    spike_score = float(scenario.get("spike_score", 0.95))
    plateau_score = float(scenario.get("plateau_score", 0.80))
    spikes = scenario.get("spikes", [])
    plateaus = scenario.get("plateaus", [])
    bad = scenario.get("bad", [])

    def _in_ranges(t, ranges):
        return any(a <= t <= b for a, b in ranges)

    out: list[dict] = []
    for f in sorted(frames, key=lambda x: x.get("t_ms", 0)):
        t = f.get("t_ms", 0)
        if _in_ranges(t, bad):
            fact = {"face_visible": "occluded", "eye_state": "unclear", "clarity": "blurry",
                    "posture_quality": "其他", "expression_natural": False}
            value = {"aes_score": 0.30, "promo_worthy": False, "promo_reason": "mock 硬坏：脸部遮挡/模糊"}
        elif _in_ranges(t, plateaus):
            fact = {"face_visible": "side", "eye_state": "open", "clarity": "sharp",
                    "posture_quality": "自然", "expression_natural": True}
            value = {"aes_score": plateau_score, "promo_worthy": True, "promo_reason": "mock 持续好看区间"}
        elif any(abs(t - s) <= 300 for s in spikes):
            fact = {"face_visible": "full", "eye_state": "open", "clarity": "sharp",
                    "posture_quality": "自然", "expression_natural": True}
            value = {"aes_score": spike_score, "promo_worthy": True, "promo_reason": "mock 突出瞬间"}
        else:
            fact = {"face_visible": "side", "eye_state": "open", "clarity": "sharp",
                    "posture_quality": "自然", "expression_natural": True}
            value = {"aes_score": base, "promo_worthy": False, "promo_reason": "mock 基本可用"}

        out.append({
            "t_ms": t,
            "frame_id": f.get("frame_id"),
            "observations": fact,
            "posture_quality": fact["posture_quality"],
            "aes_score": value["aes_score"],
            "promo_worthy": value["promo_worthy"],
            "promo_reason": value["promo_reason"],
            "fact": fact,
            "value": value,
        })
    return out


def run_mock(source_path: str | Path, *, media_type: str | None = None,
             sampling: dict | None = None, scenario: dict | None = None,
             preference: dict | None = None) -> dict:
    """离线端到端跑通：真实抽帧 + 确定性 mock 两步评分 + v3 选片。

    不访问数据库、不发起网络调用、不产生费用；结果可回放。
    """
    source = Path(source_path)
    if media_type is None:
        media_type = "image" if source.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".bmp"} else "video"
    sampling = sampling or {"max_frames": 24, "frames_per_sec": 1.0, "resize_edge": 480}

    from ..media.meta import read_video_metadata
    from ..media.sample import image_as_frame, sample_frames

    if media_type == "image":
        frames = [image_as_frame(source, resize_edge=sampling.get("resize_edge", 480))]
    else:
        md = read_video_metadata(source)
        frames = sample_frames(source, 0, md["duration_ms"],
                               max_frames=sampling.get("max_frames", 24),
                               frames_per_sec=sampling.get("frames_per_sec", 1.0),
                               resize_edge=sampling.get("resize_edge", 480),
                               duration_ms=md["duration_ms"], rotation=md.get("rotation", 0))
    annotations = mock_annotate(frames, scenario)
    return select(annotations, preference, SelectParams())


# ---------------------------------------------------------------------------
# 生产接线：v3 任务锁定 selection_pipeline_version=3 后的持久化运行。
# 采样→事实/价值两步评分→双识别→边界→精选/备选；mock 离线、real 受预算护栏。
# ---------------------------------------------------------------------------

COARSE_FPS = 0.5
COARSE_MAX_FRAMES = 120
BATCH_FRAMES = 16
DENSIFY_FPS = 4.0
DENSIFY_PAD_MS = 1500
DENSIFY_MAX_WINDOWS = 8
DENSIFY_MAX_FRAMES = 12
PHOTO_PEAK_FPS = 30
PHOTO_PEAK_PAD_MS = 250
PHOTO_PEAK_MAX_FRAMES = 16
PHOTO_PEAK_MAX_WINDOWS = 4


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def cache_key_v3(asset_fingerprint: str, task: dict, sampling: dict,
                 segment_start_ms: int, segment_end_ms: int, source: str) -> str:
    """v3 缓存身份：与旧 analysis.analysis_cache_key 独立，绝不与旧缓存混用。"""
    import hashlib
    canonical = {
        "pipeline_version": PIPELINE_VERSION,
        "asset_fingerprint": asset_fingerprint,
        "model_config_ref_json": task.get("model_config_ref_json"),
        "preference_ref_json": task.get("preference_ref_json"),
        "sampling": sampling,
        "segment": [segment_start_ms, segment_end_ms],
        "source": source,
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _v3_sampling_from_task(conn, task_id: str) -> dict:
    row = conn.execute("SELECT snapshot_ref_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    snapshot = json.loads(row[0] or "{}") if row else {}
    return snapshot.get("selection_v2_sampling") or {
        "coarse_fps": COARSE_FPS, "coarse_max_frames": COARSE_MAX_FRAMES, "resize_edge": 480,
        "densify_fps": DENSIFY_FPS, "densify_max_frames": DENSIFY_MAX_FRAMES,
        "densify_max_windows": DENSIFY_MAX_WINDOWS,
        "photo_peak_fps": PHOTO_PEAK_FPS, "photo_peak_max_frames": PHOTO_PEAK_MAX_FRAMES,
        "photo_peak_max_windows": PHOTO_PEAK_MAX_WINDOWS,
    }


# --- 真实两步评分（阶段④接线） ---

def _build_fact_messages(frames: list[dict]) -> list[dict]:
    system = FACT_PROMPT + (
        '\n输出严格 JSON：{"frames":[{"frame_id":"f0","observations":{"face_visible":"full|side|cropped|occluded|unclear",'
        '"eye_state":"open|half|closed|blinking|unclear","clarity":"sharp|soft|blurry",'
        '"posture_quality":"自然|别扭|其他|unclear","expression_natural":true或false}}]}。每个 frame_id 恰好出现一次。'
    )
    content = [{"type": "text", "text": "请逐帧输出事实观察（每张图上方标注 frame_id 与时间点）。"}]
    for i, f in enumerate(frames):
        fid = f"f{i}"
        content.append({"type": "text", "text": f"frame_id={fid} 时间点={f['t_ms']}ms"})
        content.append({"type": "image_url", "image_url": {"url": f["data_url"]}})
    return [{"role": "system", "content": system}, {"role": "user", "content": content}]


def _build_value_messages(frames: list[dict], facts: dict) -> list[dict]:
    system = VALUE_PROMPT + (
        '\n输出严格 JSON：{"frames":[{"frame_id":"f0","aes_score":0-1,"promo_worthy":true或false,"promo_reason":"≤80字"}]}。每个 frame_id 恰好出现一次。'
    )
    content = [{"type": "text", "text": "已观察到的画面事实如下，请基于事实判断单帧宣传价值（每张图上方标注 frame_id 与时间点）。"}]
    for i, f in enumerate(frames):
        fid = f"f{i}"
        content.append({"type": "text", "text": f"frame_id={fid} 时间点={f['t_ms']}ms 事实={json.dumps(facts.get(fid, {}), ensure_ascii=False)}"})
        content.append({"type": "image_url", "image_url": {"url": f["data_url"]}})
    return [{"role": "system", "content": system}, {"role": "user", "content": content}]


def _parse_fact(text: str, frames: list[dict]) -> dict:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`").removeprefix("json").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"fact 不是 JSON: {e}") from e
    items = data.get("frames") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("fact 缺 frames 数组")
    expected = {f"f{i}" for i in range(len(frames))}
    seen: set[str] = set()
    out: dict[str, dict] = {}
    for item in items:
        fid = item.get("frame_id") if isinstance(item, dict) else None
        obs = item.get("observations") if isinstance(item, dict) else None
        if fid not in expected or fid in seen or not isinstance(obs, dict):
            raise ValueError("fact frame_id 非法/重复/缺 observations")
        enums = {
            "face_visible": {"full", "side", "cropped", "occluded", "unclear"},
            "eye_state": {"open", "half", "closed", "blinking", "unclear"},
            "clarity": {"sharp", "soft", "blurry"},
            "posture_quality": {"自然", "别扭", "其他", "unclear"},
        }
        for k, vals in enums.items():
            if obs.get(k) not in vals:
                raise ValueError(f"fact {fid} {k} 非法")
        if not isinstance(obs.get("expression_natural"), bool):
            raise ValueError(f"fact {fid} expression_natural 非布尔")
        seen.add(fid)
        out[fid] = dict(obs)
    if seen != expected:
        raise ValueError(f"fact frame_id 遗漏：{sorted(expected - seen)}")
    return out


def _parse_value(text: str, frames: list[dict]) -> dict:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`").removeprefix("json").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"value 不是 JSON: {e}") from e
    items = data.get("frames") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("value 缺 frames 数组")
    expected = {f"f{i}" for i in range(len(frames))}
    seen: set[str] = set()
    out: dict[str, dict] = {}
    for item in items:
        fid = item.get("frame_id") if isinstance(item, dict) else None
        if fid not in expected or fid in seen:
            raise ValueError("value frame_id 非法/重复")
        try:
            aes = float(item.get("aes_score"))
        except (TypeError, ValueError):
            raise ValueError(f"value {fid} aes_score 非法")
        if not (0.0 <= aes <= 1.0):
            raise ValueError(f"value {fid} aes_score 越界")
        if not isinstance(item.get("promo_worthy"), bool):
            raise ValueError(f"value {fid} promo_worthy 非布尔")
        reason = item.get("promo_reason", "")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"value {fid} promo_reason 非法")
        seen.add(fid)
        out[fid] = {"aes_score": aes, "promo_worthy": item["promo_worthy"], "promo_reason": reason[:100]}
    if seen != expected:
        raise ValueError(f"value frame_id 遗漏：{sorted(expected - seen)}")
    return out


def _score_frames(conn, task_id: str, frames: list[dict], transport, execution_id: str,
                  rid_prefix: str, scenario: dict | None = None) -> tuple[list[dict], int]:
    """对一批帧做事实/价值两步评分。mock 返回 (annotations, 0)；real 返回 (annotations, 真实调用次数)。"""
    from .vlm import MockVlmTransport
    if isinstance(transport, MockVlmTransport):
        return mock_annotate(frames, scenario), 0

    from .compare import _guarded_call
    annotations: list[dict] = []
    calls = 0
    for bi in range(0, len(frames), BATCH_FRAMES):
        batch = frames[bi:bi + BATCH_FRAMES]
        rid = f"{rid_prefix}-b{bi}"
        resp, _ = _guarded_call(conn, task_id, _build_fact_messages(batch), transport,
                                f"{rid}-fact", execution_id, max_retries=1)
        calls += 1
        facts = _parse_fact(resp["text"], batch)
        resp2, _ = _guarded_call(conn, task_id, _build_value_messages(batch, facts), transport,
                                 f"{rid}-value", execution_id, max_retries=1)
        calls += 1
        values = _parse_value(resp2["text"], batch)
        for i, f in enumerate(batch):
            fid = f"f{i}"
            fact = facts[fid]
            value = values[fid]
            annotations.append({
                "t_ms": f["t_ms"],
                "frame_id": f.get("frame_id") or fid,
                "source_pts": f.get("source_pts"),
                "time_base": f.get("time_base"),
                "pts_ms": f.get("pts_ms"),
                "observations": fact,
                "posture_quality": fact.get("posture_quality"),
                "aes_score": value["aes_score"],
                "promo_worthy": value["promo_worthy"],
                "promo_reason": value["promo_reason"],
                "fact": fact,
                "value": value,
            })
    return annotations, calls


def _merge_annotations(groups: list[list[dict]]) -> list[dict]:
    best: dict[int, dict] = {}
    for ann in groups:
        for f in ann:
            t = f.get("t_ms", 0)
            if t not in best or _aes(f) > _aes(best[t]):
                best[t] = f
    return sorted(best.values(), key=lambda f: f.get("t_ms", 0))


def _merge_windows(wins: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for s, e in sorted(wins):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(int(a), int(b)) for a, b in out]


def _densify_windows(sel: dict, duration_ms: int, limit: int) -> list[tuple[int, int]]:
    wins: list[tuple[int, int]] = []
    for sp in sel.get("spikes", []):
        s = max(0, sp["t_ms"] - DENSIFY_PAD_MS)
        e = min(duration_ms, sp["t_ms"] + DENSIFY_PAD_MS)
        if e > s:
            wins.append((s, e))
    for pl in sel.get("plateaus", []):
        s = max(0, pl["start_ms"] - DENSIFY_PAD_MS)
        e = min(duration_ms, pl["end_ms"] + DENSIFY_PAD_MS)
        if e > s:
            wins.append((s, e))
    return _merge_windows(wins)[:limit]


def _photo_peak_windows(sel: dict, duration_ms: int, limit: int) -> list[tuple[int, int]]:
    wins: list[tuple[int, int]] = []
    for p in sel.get("curated_photos", [])[:limit]:
        t = p["t_ms"]
        s = max(0, t - PHOTO_PEAK_PAD_MS)
        e = min(duration_ms, t + PHOTO_PEAK_PAD_MS)
        if e > s:
            wins.append((s, e))
    return wins


def analyze_asset_v3(conn, task_id: str, asset_id: str, transport,
                     execution_id: str | None = None, sampling: dict | None = None,
                     scenario: dict | None = None) -> dict:
    """v3 单素材运行：粗筛→候选加密→照片峰值加密→ select→持久化。

    mock：离线确定性；real：DeepSeekTransport + 费用护栏，仅任务预算已配置时允许。
    """
    from .. import security
    from .assets import new_id
    from .vlm import DeepSeekTransport, MockVlmTransport

    if not isinstance(transport, (MockVlmTransport, DeepSeekTransport)):
        raise security.SecurityError("invalid_transport", "v3 仅支持 mock 或 real DeepSeekTransport")

    task = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if task is None:
        raise security.SecurityError("task_not_found", f"任务不存在：{task_id}")
    asset = conn.execute("SELECT * FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
    if asset is None:
        raise security.SecurityError("asset_not_found", f"素材未登记：{asset_id}")
    if isinstance(transport, DeepSeekTransport) and task["budget_limit_minor"] is None:
        raise security.SecurityError("budget_unconfigured", "预算未配置，禁止 real 调用")

    from .pipeline import _assert_task_owns_asset
    _assert_task_owns_asset(conn, dict(task), dict(asset))

    sampling = sampling or _v3_sampling_from_task(conn, task_id)
    execution_id = execution_id or new_id("exec")
    source = "mock" if isinstance(transport, MockVlmTransport) else "real"

    from ..media.meta import read_video_metadata
    from ..media.sample import image_as_frame, sample_frames
    src = Path(asset["source_path"])
    resize = sampling.get("resize_edge", 480)
    if asset["media_type"] == "image":
        coarse_frames = [image_as_frame(src, resize_edge=resize)]
        duration_ms = 1
    else:
        md = read_video_metadata(src)
        duration_ms = md["duration_ms"]
        coarse_frames = sample_frames(src, 0, duration_ms,
                                      max_frames=sampling.get("coarse_max_frames", COARSE_MAX_FRAMES),
                                      frames_per_sec=sampling.get("coarse_fps", COARSE_FPS),
                                      resize_edge=resize, duration_ms=duration_ms,
                                      rotation=md.get("rotation", 0))

    cache_key = cache_key_v3(asset["fingerprint"], dict(task), sampling, 0, duration_ms, source)
    cached = conn.execute(
        "SELECT * FROM analysis_runs WHERE task_id=? AND asset_id=? AND cache_key=?",
        (task_id, asset_id, cache_key),
    ).fetchone()
    if cached is not None and cached["status"] == "complete":
        return {"reused": True, "run_id": cached["run_id"], "execution_id": cached["execution_id"],
                "annotations": json.loads(cached["annotations_json"]),
                "selection": json.loads(cached["selections_json"]),
                "coverage": json.loads(cached["coverage_json"]), "source": cached["source"]}

    run_id = new_id("run")
    total_calls = 0

    coarse_ann, calls = _score_frames(conn, task_id, coarse_frames, transport, execution_id, f"{execution_id}-coarse", scenario)
    total_calls += calls
    sel = select(coarse_ann, None, SelectParams())

    extra_groups: list[list[dict]] = [coarse_ann]
    densify = _densify_windows(sel, duration_ms, sampling.get("densify_max_windows", DENSIFY_MAX_WINDOWS))
    for s, e in densify:
        if asset["media_type"] == "image":
            continue
        fs = sample_frames(src, s, e, max_frames=sampling.get("densify_max_frames", DENSIFY_MAX_FRAMES),
                           frames_per_sec=sampling.get("densify_fps", DENSIFY_FPS), resize_edge=resize,
                           duration_ms=duration_ms, rotation=read_video_metadata(src).get("rotation", 0))
        ann, calls = _score_frames(conn, task_id, fs, transport, execution_id, f"{execution_id}-densify-{s}-{e}", scenario)
        total_calls += calls
        extra_groups.append(ann)

    peak = _photo_peak_windows(sel, duration_ms, sampling.get("photo_peak_max_windows", PHOTO_PEAK_MAX_WINDOWS))
    for s, e in peak:
        if asset["media_type"] == "image":
            continue
        fs = sample_frames(src, s, e, max_frames=sampling.get("photo_peak_max_frames", PHOTO_PEAK_MAX_FRAMES),
                           frames_per_sec=sampling.get("photo_peak_fps", PHOTO_PEAK_FPS), resize_edge=resize,
                           duration_ms=duration_ms, rotation=read_video_metadata(src).get("rotation", 0))
        ann, calls = _score_frames(conn, task_id, fs, transport, execution_id, f"{execution_id}-photo-peak-{s}-{e}", scenario)
        total_calls += calls
        extra_groups.append(ann)

    annotations = _merge_annotations(extra_groups)
    selection = select(annotations, None, SelectParams())
    coverage = {"pipeline_version": PIPELINE_VERSION, "source": source,
                "sampling": sampling, "frames_analyzed": len(annotations),
                "calls": total_calls, "densify_windows": densify, "photo_peak_windows": peak,
                "start_ms": 0, "end_ms": duration_ms}

    from ..db import atomic
    with atomic(conn):
        conn.execute(
            "INSERT OR REPLACE INTO executions(execution_id,task_id,asset_id,status,created_at,updated_at,coverage_plan_json) VALUES(?,?,?,?,?,?,?)",
            (execution_id, task_id, asset_id, "complete", _now(), _now(), json.dumps(coverage, ensure_ascii=False)),
        )
        conn.execute(
            """INSERT OR REPLACE INTO analysis_runs(run_id,task_id,asset_id,cache_key,execution_id,status,source,
                 preference_ref_json,model_config_ref_json,sampling_json,annotations_json,selections_json,coverage_json,cost_id,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, task_id, asset_id, cache_key, execution_id, "complete", source,
             task["preference_ref_json"], task["model_config_ref_json"],
             json.dumps(sampling, ensure_ascii=False),
             json.dumps(annotations, ensure_ascii=False),
             json.dumps(selection, ensure_ascii=False),
             json.dumps(coverage, ensure_ascii=False), None, _now()),
        )
        conn.execute("INSERT OR IGNORE INTO batch_members(execution_id,run_id) VALUES(?,?)", (execution_id, run_id))
    return {"reused": False, "run_id": run_id, "execution_id": execution_id,
            "annotations": annotations, "selection": selection, "coverage": coverage, "source": source}

"""双路径选片 + 目标闭环：确定性逻辑（模型只给标注，选片规则由代码执行）。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


@dataclass
class SelectParams:
    """试验阈值，可配置；非已验证最佳值（本轮用正反例校准）。"""
    video_threshold: float = 0.6          # 「ok/上下文」帧的合格线
    strong_aes: float = 0.75              # 「强好」核心帧（正脸/圆眼且自然）整体分门槛（校准自接受片段）
    exceptional_threshold: float = 0.85   # 局部惊艳瞬间
    photo_sharpness_threshold: float = 0.85
    gap_ms: int = 2000
    min_duration_ms: int = 1000      # 视频成品最短时长：低于此值按碎片处理（避免零时长/瞬时片段）


def _obs(f: dict, key: str) -> str | None:
    o = f.get("observations") or {}
    v = o.get(key)
    return str(v).strip() if v not in (None, "") else None


def _is_hard_bad(f: dict) -> bool:
    """硬坏（客观问题）：脸部裁切/遮挡、字段冲突、严重模糊；这些不应被桥接。"""
    return _face(f) in ("cropped", "occluded") or _face_conflict(f) or _obs(f, "clarity") in ("blurry", "模糊") or f.get("posture_quality") == "别扭"


def _face(f: dict) -> str | None:
    """统一脸部可见程度：full 正脸 / side 完整侧脸 / cropped 被裁切 / occluded 被遮挡。
    兼容旧字段值：partial → side；face_blocked → occluded。"""
    v = _obs(f, "face_visible") or _obs(f, "occlusion")
    if v in ("full", "front", "正脸"):
        return "full"
    if v in ("side", "partial", "侧脸", "profile"):
        return "side"
    if v in ("cropped", "cut", "裁切"):
        return "cropped"
    if v in ("occluded", "face_blocked", "blocked", "遮挡"):
        return "occluded"
    return None


def _expr_ok(f: dict) -> bool:
    v = _obs(f, "expression_natural")
    if v is None:
        return True  # 旧标注无此字段时按通过（保守：由 aes/eye/face 兜底）
    return v in ("true", "True", "True") or v is True


def _face_conflict(f: dict) -> bool:
    """字段冲突：face_visible 说 full/side，但 occlusion 说被遮挡 → 不一致，待核查。"""
    fv = _obs(f, "face_visible")
    oc = _obs(f, "occlusion")
    return fv in ("full", "side") and oc in ("face_blocked", "occluded", "blocked", "遮挡")


def _classify_roles(ann: list[dict], p: SelectParams) -> list[str]:
    """上下文感知的帧角色：bad（硬坏）/ strong（好核心）/ ok（上下文/过渡）。
    硬坏：脸部被裁切/遮挡、字段冲突、严重模糊；这些是客观问题，不被整体高分抵消。
    弱信号（质量不合格/闭眼/不自然表情/低分）：孤立一帧 → 自然过渡，连续 → 持续不佳打断。"""
    weaks = ("weak_qf", "weak_closed", "weak_expr", "weak_aes")
    n = len(ann)
    base: list[str] = []
    for f in ann:
        face = _face(f)
        if _is_hard_bad(f):
            base.append("bad"); continue
        eye = _obs(f, "eye_state")
        expr_ok = _expr_ok(f)
        aes = float(f.get("aes_score", 0.0))
        qf = bool(f.get("quality_floor_pass"))
        if not qf:
            base.append("weak_qf")
        elif eye == "closed":
            base.append("weak_closed")
        elif expr_ok is False:
            base.append("weak_expr")
        elif aes < p.video_threshold:
            base.append("weak_aes")
        elif face in ("full", "side") and eye == "open" and expr_ok and aes >= p.strong_aes:
            base.append("strong")
        else:
            base.append("ok")
    roles: list[str] = []
    for i, r in enumerate(base):
        if r in weaks:
            prev = base[i - 1] if i > 0 else None
            nxt = base[i + 1] if i < n - 1 else None
            if prev in weaks + ("bad",) or nxt in weaks + ("bad",):
                roles.append("bad")  # 持续不佳
            else:
                roles.append("ok")   # 孤立 → 自然过渡
        else:
            roles.append(r)
    return roles


def _frame_is_photo_candidate(f: dict, p: SelectParams) -> bool:
    """照片候选：单帧宣传价值。脸部完整可见（正脸或完整侧脸）、眼睛睁开、表情自然、清晰、整体分达标。"""
    scores = f.get("scores", {})
    if _is_hard_bad(f) or f.get("posture_quality") == "别扭" or (f.get("overall_harmony") is not None and f["overall_harmony"] <= 0.05):
        return False
    if not (bool(f.get("quality_floor_pass")) and float(f.get("aes_score", 0.0)) >= p.video_threshold):
        return False
    if float(scores.get("sharpness", 0.0) or 0.0) < p.photo_sharpness_threshold:
        return False
    if _face(f) not in ("full", "side"):  # 完整可见：正脸或完整侧脸；被裁切/被遮挡不算
        return False
    if _obs(f, "eye_state") != "open":
        return False
    if not _expr_ok(f):
        return False
    if _obs(f, "clarity") not in ("sharp", "清晰"):
        return False
    return True


def _frame_is_exceptional(f: dict, preference: dict, p: SelectParams) -> bool:
    # 抬爪/歪头只是加分项，不直接等同「特别精彩」；惊艳以整体分达标为准
    return float(f.get("aes_score", 0.0)) >= p.exceptional_threshold


def _photo_quality_score(f: dict) -> float:
    """照片综合质量分（0-1，乘性，任一硬缺陷即大幅拉低）：清晰度 × 脸部完整 × 姿态自然 × (整体协调/整体分/眼神加权)。
    正脸/抬爪不自动好看；静态、完整侧脸可优秀。"""
    obs = f.get("observations") or {}
    clarity = {"sharp": 1.0, "soft": 0.4, "blurry": 0.0, "模糊": 0.0}.get(obs.get("clarity"), 0.6)
    face = {"full": 1.0, "side": 0.85, "cropped": 0.3, "occluded": 0.0}.get(obs.get("face_visible"), 0.6)
    posture = {"自然": 1.0, "其他": 0.6, "别扭": 0.0}.get(f.get("posture_quality"), 0.7)
    sc = f.get("scores") or {}
    eye = float(sc.get("eye") if sc.get("eye") is not None else 0.5)
    face_sc = float(sc.get("face") if sc.get("face") is not None else 0.5)
    harmony = float(f.get("overall_harmony") if f.get("overall_harmony") is not None else 0.5)
    aes = float(f.get("aes_score") if f.get("aes_score") is not None else 0.5)
    return clarity * face * posture * (0.3 * harmony + 0.3 * aes + 0.2 * eye + 0.2 * face_sc)


def _photo_rank_key(f: dict, action_bonus: set):
    """照片相对排序：综合质量分 → 动作加分 → 时间。不简单把脸/眼/爪分数相加。"""
    hits = len(set(f.get("preference_hits", []) or []) & action_bonus)
    return (-_photo_quality_score(f), -hits, f.get("t_ms", 0))


def _dedup_signature(f: dict) -> tuple:
    """画面相似性签名：只取结构化观察字段（不取自由文本 caption/expression，避免近同帧因措辞不同而漏去重）。"""
    obs = f.get("observations") or {}
    return (
        obs.get("pose", "") or "",
        obs.get("angle", "") or "",
        obs.get("eye_state", "") or "",
        obs.get("face_visible", "") or "",
    )


def dedup_similar_photos(photo_list: list[dict], action_bonus: set, window_ms: int = 1500) -> list[dict]:
    """相似候选择优去重：固定时间桶（非链式）+ 画面签名，同窗同签名只留最佳，不同姿态/表情保留。"""
    if not photo_list:
        return []
    buckets: dict[int, list[dict]] = {}
    for f in photo_list:
        b = int(f.get("t_ms", 0) or 0) // window_ms
        buckets.setdefault(b, []).append(f)
    result: list[dict] = []
    for b in sorted(buckets):
        by_sig: dict[tuple, dict] = {}
        for f in buckets[b]:
            sig = _dedup_signature(f)
            if sig not in by_sig or _photo_rank_key(f, action_bonus) < _photo_rank_key(by_sig[sig], action_bonus):
                by_sig[sig] = f
        result.extend(by_sig.values())
    return result


def _bridge_short_bad(ann: list[dict], roles: list[str], max_bridge_ms: int = 2000) -> list[str]:
    """只桥接「非硬坏」的短弱坏帧（眨眼/一瞬不自然），按持续时间判断（不按固定帧数，适配不同采样密度）。
    硬坏（遮挡/裁切/模糊/字段冲突）不桥接；持续弱坏超时长仍断。"""
    out = list(roles)
    n = len(ann)
    i = 0
    while i < n:
        if out[i] == "bad" and not _is_hard_bad(ann[i]):
            j = i
            while j < n and out[j] == "bad" and not _is_hard_bad(ann[j]):
                j += 1
            # 用相邻非坏帧的间隔估算真实过渡时长（含两侧采样间隙），稀疏采样不低估
            prev_t = ann[i - 1]["t_ms"] if i > 0 else ann[i]["t_ms"]
            next_t = ann[j]["t_ms"] if j < n else ann[j - 1]["t_ms"]
            gap = next_t - prev_t
            if gap <= max_bridge_ms:
                for k in range(i, j):
                    out[k] = "ok"
            i = j
        else:
            i += 1
    return out


def merge_natural_segments(frames: list[dict], params: "SelectParams | None" = None) -> list[dict]:
    """从全量帧（含候选之外的前后画面）自动切自然连续段。
    不设固定时长、不机械并集——按帧角色（strong/ok/bad）找连续好核心，孤立弱帧作自然过渡。"""
    params = params or SelectParams()
    ann = sorted(frames, key=lambda f: f.get("t_ms", 0))
    if not ann:
        return []
    roles = _classify_roles(ann, params)
    roles = _bridge_short_bad(ann, roles, max_bridge_ms=2000)  # 桥接 ≤2s 的非硬坏弱帧；硬坏不桥接
    segments: list[dict] = []
    i, n = 0, len(ann)
    while i < n:
        if roles[i] == "bad":
            i += 1
            continue
        j = i
        strong_count = 0
        while j < n and roles[j] != "bad":
            if roles[j] == "strong":
                strong_count += 1
            if j > i and ann[j]["t_ms"] - ann[j - 1]["t_ms"] > params.gap_ms:
                break
            j += 1
        run = ann[i:j]
        run_roles = roles[i:j]
        s, e = 0, len(run) - 1
        while s <= e and run_roles[s] == "ok":
            s += 1
        while e >= s and run_roles[e] == "ok":
            e -= 1
        core = run[s:e + 1]
        strong_in_core = sum(1 for r in run_roles[s:e + 1] if r == "strong")
        if strong_in_core >= 2 and core:
            seg = {"start_ms": core[0]["t_ms"], "end_ms": core[-1]["t_ms"],
                   "frames": core, "path": "natural", "source": "auto"}
            # 边界证据标记：核心前一/后一帧是 ok（过渡）或 run 被间隙切断 → 证据不足
            notes = []
            if s > 0 and run_roles[s - 1] == "ok":
                notes.append("前边界证据不足")
            if e < len(run) - 1 and run_roles[e + 1] == "ok":
                notes.append("后边界证据不足")
            if j < n and roles[j] != "bad":  # 间隙切断（无 hard bad 确认）
                notes.append("后边界为间隙切断")
            if i > 0 and roles[i - 1] != "bad":
                notes.append("前边界为间隙切断")
            if notes:
                seg["boundary_note"] = "；".join(notes)
            segments.append(seg)
        # strong_in_core == 1：单帧精彩瞬间属照片，不进视频段
        i = j
    return segments


def boundary_gaps(frames: list[dict], params: "SelectParams | None" = None) -> list[tuple[int, int]]:
    """返回全量帧中超过 gap_ms 的间隙区间（边界证据不足，需补查）。"""
    params = params or SelectParams()
    ann = sorted(frames, key=lambda f: f.get("t_ms", 0))
    gaps: list[tuple[int, int]] = []
    for i in range(1, len(ann)):
        if ann[i]["t_ms"] - ann[i - 1]["t_ms"] > params.gap_ms:
            gaps.append((ann[i - 1]["t_ms"], ann[i]["t_ms"]))
    return gaps


def select_dual_path(annotations: list[dict], preference: dict, params: SelectParams | None = None) -> dict:
    """双路径选片 + 段级连续观看判断。
    视频段：在时间序列上找「无坏帧」的运行，要求有 ≥2 个强好核心帧（证据充分），并裁掉首尾过渡帧；
    自然眨眼/动作过渡/侧脸上下文允许存在，但不充当核心；证据不足（仅 1 强帧）→ 待细查。"""
    params = params or SelectParams()
    ann = sorted(annotations, key=lambda x: x["t_ms"])
    action_bonus = set(preference.get("visual_rules", {}).get("action_bonus", []) or [])

    photo_frames = sorted([f for f in ann if _frame_is_photo_candidate(f, params)],
                          key=lambda f: _photo_rank_key(f, action_bonus))
    # 分层：基本可用=photo_frames（全部合格）；精选推荐=promo_worthy 显式为 true；最佳候选=推荐首位（允许为空）
    # 缺失 promo_worthy 不能默认视为精选通过
    recommended_photos = [f for f in photo_frames if f.get("promo_worthy") is True]
    top_photo = recommended_photos[0] if recommended_photos else None

    roles = _classify_roles(ann, params)
    video_segments: list[dict] = []
    pending_candidates: list[dict] = []
    i, n = 0, len(ann)
    while i < n:
        if roles[i] == "bad":
            i += 1
            continue
        # 扩展一个无坏帧的运行
        j = i
        strong_count = 0
        while j < n and roles[j] != "bad":
            if roles[j] == "strong":
                strong_count += 1
            if j > i and ann[j]["t_ms"] - ann[j - 1]["t_ms"] > params.gap_ms:
                break
            j += 1
        run = ann[i:j]
        run_roles = roles[i:j]
        # 裁掉首尾过渡/上下文帧，边界落在好帧上
        s, e = 0, len(run) - 1
        while s <= e and run_roles[s] == "ok" and not (run_roles[s] == "strong"):
            s += 1
        while e >= s and run_roles[e] == "ok" and not (run_roles[e] == "strong"):
            e -= 1
        core = run[s:e + 1]
        strong_in_core = sum(1 for r in run_roles[s:e + 1] if r == "strong")
        if strong_in_core >= 2 and core:
            video_segments.append({"start_ms": core[0]["t_ms"], "end_ms": core[-1]["t_ms"], "frames": core,
                                   "path": "continuous", "status": "deliverable", "reason": "连续好看较密集"})
        elif strong_in_core == 1 and core:
            f = max(core, key=lambda x: float(x.get("aes_score", 0.0)))
            pending_candidates.append({"start_ms": f["t_ms"], "end_ms": f["t_ms"], "frames": [f],
                                       "path": "exceptional", "status": "pending_fine_inspection",
                                       "reason": "局部特别精彩（证据不足，待细查确定边界）"})
        # strong_in_core == 0：只有上下文帧，无强核心，不产出
        i = j

    video_segments.sort(key=lambda s: s["start_ms"])
    pending_candidates.sort(key=lambda s: s["start_ms"])
    return {
        "video_segments": video_segments,
        "pending_candidates": pending_candidates,
        "photo_frames": photo_frames,
        "recommended_photos": recommended_photos,
        "top_photo": top_photo,
    }


def goal_check(selection: dict, preference: dict) -> str:
    """outcome：complete / partial / none_qualified。待细查候选不算已交付片段。"""
    has_video = len(selection.get("video_segments", [])) > 0
    has_photo = len(selection.get("photo_frames", [])) > 0
    if has_video and has_photo:
        return "complete"
    if has_video or has_photo:
        return "partial"
    return "none_qualified"


SELECTION_RULE_VERSION = 9  # v8：照片排序先缺陷门+整体协调 harmony，再相对排序；自然段按帧角色+边界证据标记
PROMPT_VERSION = 3             # v3：understand 提示词增加 posture_quality/overall_harmony，侧脸/构图口径


def analysis_cache_key(asset_fingerprint: str, model_config_ref: dict | None,
                       preference_ref: dict | None, sampling: dict, prompt_version: int = 1,
                       segment_start_ms: int = 0, segment_end_ms: int = 0,
                       mode: str = "mock") -> str:
    """缓存身份：素材指纹 + 模型/偏好快照 + 采样 + 分析范围 + 运行模式 + 规则/提示词版本。"""
    canonical = {
        "asset_fingerprint": asset_fingerprint,
        "model_config_ref": model_config_ref,
        "preference_ref": preference_ref,
        "sampling": sampling,
        "prompt_version": prompt_version,
        "selection_rule_version": SELECTION_RULE_VERSION,
        "segment_start_ms": segment_start_ms,
        "segment_end_ms": segment_end_ms,
        "mode": mode,
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

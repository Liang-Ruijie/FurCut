"""对照评测：受预算控制的真实调用 + 严格解析（照片单帧宣传价值 / 视频有序上下文片段可用性）。"""
from __future__ import annotations

import json
import math
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Callable

from .. import security
from . import request_journal
from . import cost, model_config
from .cost import TokenPricing, token_cost_micro
from .vlm import VlmNetworkError, MockVlmTransport

# 每帧输出上界（新结构字段更多，比旧版大）
EST_INPUT_PER_FRAME = 2000
EST_OUTPUT_PER_FRAME = 300
DEFAULT_MAX_OUTPUT_TOKENS = 6000


class EvalParseError(Exception):
    pass


class EvalNetworkError(Exception):
    pass


PROMPT_PHOTO = (
    "你是猫舍素材评估器。目标：区分「可用于宣传的画面」与「仅技术合格的画面」。\n"
    "对每帧先记录客观观察，再给单帧宣传价值判断。输出严格 JSON：\n"
    '{"frames":[{"frame_id":"f0","observations":{'
    '"subject_coverage":"近景|中景|远景","pose":"静态端坐|静态站立|动态动作|其他",'
    '"angle":"正对|微侧|侧|背","face_detail":"清晰|一般|不足","interference":"无|轻微|明显",'
    '"expression_specific":"简短描述"},'
    '"promo_worthy":true或false,"promo_reason":"≤50字"}]}\n'
    "字段规则：observations 是事实；promo_worthy 是判断——这帧作为宣传画面是否足够吸引注意。\n"
    "promo_worthy=true 表示「值得精选保留」（精选推荐档），不是「基本可用」；基本可用但不够出彩的帧应标 false。\n"
    "照片是单帧独立使用（封面/首图）。核心：眼睛、脸部、表情和整体观感好即可，静态照片也可以；动作只是加分项，不是必须。\n"
    "侧脸允许，但不能侧到影响脸部和眼睛的完整展示；视线可以偏离但不能偏到失去交流感。不要用「必须正脸、必须直视」的硬规则。\n"
    "主体偏一侧是构图问题（可裁剪改善），不直接据此淘汰；主体审美与构图可修复性分别记入 promo_reason（如「主体好，构图偏左可裁」）。\n"
    "允许全部合格、部分合格或全部不合格，按实际判断，不人为拉开差异。每帧的 promo_reason 说明判断依据。\n"
)

PROMPT_VIDEO = (
    "你是猫舍素材评估器。目标：判断一段连续画面是否可作为成片片段，并给出可用起止边界。\n"
    "下面给出按时间顺序排列的连续帧（每帧标注 frame_id 与时间 ms）。\n"
    "请输出严格 JSON：\n"
    '{"segment_usable":true或false,"usable_start_ms":整数,"usable_end_ms":整数,"reason":"≤80字"}\n'
    "segment_usable=false 表示整段不适合交付；true 时给出自然起止边界（不机械截固定长度）。\n"
    "segment_usable=true 表示「值得精选保留」的片段，不是「基本可用」；基本可用但不够出彩的片段应判 false。\n"
    "交付价值看画面是否自然、清晰、表情生动、有细微变化（眨眼/视线/歪头等）；动作是加分项，不是必须。\n"
    "结合连续帧的状态变化、好画面分布、遮挡/不自然表情的持续情况判断；允许自然眨眼与动作过渡。\n"
    "按实际判断，不人为要求拉开差异。\n"
)


def _finite01(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v)) and 0.0 <= float(v) <= 1.0


_ENUMS = {
    "subject_coverage": {"近景", "中景", "远景"},
    "pose": {"静态端坐", "静态站立", "动态动作", "其他"},
    "angle": {"正对", "微侧", "侧", "背"},
    "face_detail": {"清晰", "一般", "不足"},
    "interference": {"无", "轻微", "明显"},
}


def parse_promo_frames(text: str, expected_ids: list[str]) -> list[dict]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`").removeprefix("json").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise EvalParseError(f"无法解析 JSON: {e}")
    frames = data.get("frames") if isinstance(data, dict) else None
    if not isinstance(frames, list):
        raise EvalParseError("缺少 frames 数组")
    exp = set(expected_ids)
    seen: set[str] = set()
    out = []
    for f in frames:
        if not isinstance(f, dict):
            raise EvalParseError("frames 含非对象")
        fid = f.get("frame_id")
        if not isinstance(fid, str) or fid not in exp:
            raise EvalParseError(f"frame_id 不存在或越界：{fid!r}")
        if fid in seen:
            raise EvalParseError(f"frame_id 重复：{fid!r}")
        seen.add(fid)
        if not isinstance(f.get("promo_worthy"), bool):
            raise EvalParseError(f"{fid} promo_worthy 不是布尔")
        obs = f.get("observations")
        if not isinstance(obs, dict):
            raise EvalParseError(f"{fid} 缺 observations")
        for k, allowed in _ENUMS.items():
            v = obs.get(k)
            if v not in allowed:
                raise EvalParseError(f"{fid} {k} 非法：{v!r}")
        expr = obs.get("expression_specific")
        if not isinstance(expr, str) or not expr.strip():
            raise EvalParseError(f"{fid} expression_specific 非法")
        out.append({"frame_id": fid, "observations": dict(obs),
                    "promo_worthy": f["promo_worthy"], "promo_reason": str(f.get("promo_reason", ""))[:100]})
    if seen != exp:
        raise EvalParseError(f"frame_id 遗漏：{sorted(exp - seen)}")
    return out


def parse_video_segment(text: str, t_min_ms: int, t_max_ms: int) -> dict:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`").removeprefix("json").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise EvalParseError(f"无法解析 JSON: {e}")
    if not isinstance(data, dict) or not isinstance(data.get("segment_usable"), bool):
        raise EvalParseError("segment_usable 缺失或非布尔")
    if data["segment_usable"]:
        s = data.get("usable_start_ms"); e = data.get("usable_end_ms")
        # 排除 bool（bool 是 int 子类）
        if isinstance(s, bool) or isinstance(e, bool):
            raise EvalParseError("边界不能是布尔值")
        if not isinstance(s, int) or not isinstance(e, int):
            raise EvalParseError("usable_start_ms/end_ms 非法")
        if s >= e:
            raise EvalParseError("边界 start>=end")
        if s < t_min_ms or e > t_max_ms:
            raise EvalParseError(f"边界超出输入范围 [{t_min_ms},{t_max_ms}]：{s}-{e}")
        return {"segment_usable": True, "usable_start_ms": s, "usable_end_ms": e,
                "reason": str(data.get("reason", ""))[:100],
                "boundary_precision": "近似（稀疏采样，非逐帧精确）"}
    return {"segment_usable": False, "usable_start_ms": None, "usable_end_ms": None,
            "reason": str(data.get("reason", ""))[:100], "boundary_precision": "N/A"}


def _build_photo_messages(frames: list[dict]) -> list[dict]:
    content = [{"type": "text", "text": "请逐帧评估（每张图上方标注 frame_id 与时间）。"}]
    for i, f in enumerate(frames):
        fid = f"f{i}"
        content.append({"type": "text", "text": f"frame_id={fid} 时间={f['t_ms']}ms"})
        content.append({"type": "image_url", "image_url": {"url": f["data_url"]}, "_frame_id": fid, "_t_ms": f["t_ms"]})
    return [{"role": "system", "content": PROMPT_PHOTO}, {"role": "user", "content": content}]


def _build_video_messages(frames: list[dict]) -> list[dict]:
    content = [{"type": "text", "text": "以下是按时间顺序的连续帧。"}]
    for i, f in enumerate(frames):
        fid = f"f{i}"
        content.append({"type": "text", "text": f"frame_id={fid} 时间={f['t_ms']}ms"})
        content.append({"type": "image_url", "image_url": {"url": f["data_url"]}, "_frame_id": fid, "_t_ms": f["t_ms"]})
    return [{"role": "system", "content": PROMPT_VIDEO}, {"role": "user", "content": content}]


# 各模型计价（美元/百万 token，cny_per_usd=7.2）。deepseek=官方峰值价；dashscope=qwen3.8-max 华北2(北京)官方标准价。
PRICING_BY_PROVIDER = {
    "deepseek": TokenPricing(input_per_1m_usd=0.3, output_per_1m_usd=1.2, cache_hit_input_per_1m_usd=0.006, cny_per_usd=7.2),
    # qwen3.8-max：输入 ¥12/1M、输出 ¥36/1M、缓存命中输入 ¥1.5/1M → USD 等价 1.667/5.0/0.208
    "dashscope": TokenPricing(input_per_1m_usd=1.667, output_per_1m_usd=5.0, cache_hit_input_per_1m_usd=0.208, cny_per_usd=7.2),
}

PRICE_BASIS = {
    "deepseek": {"source": "api-docs.deepseek.com/quick_start/pricing", "date": "2026-10-31", "status": "verified",
                  "note": "deepseek-flash 峰值输入 $0.3/1M、输出 $1.2/1M"},
    "dashscope": {"source": "help.aliyun.com/zh/model-studio/qwen3-8-max（华北2北京）", "date": "2026-11-01",
                  "status": "verified", "note": "qwen3.8-max 输入 ¥12/1M、输出 ¥36/1M、缓存命中输入 ¥1.5/1M"},
}


def _pricing_for(vlm_cfg: dict) -> TokenPricing:
    provider = (vlm_cfg or {}).get("provider", "").lower()
    try:
        return cost.pricing_for_provider(provider)
    except security.SecurityError as e:
        raise EvalParseError(str(e)) from e


def _guarded_call(conn, task_id, messages, transport, request_id, execution_id, max_retries=1, model_cfg=None):
    """受预算护栏的调用：预留(输入上界 + max_output_tokens)→调用→结算(usage)。
    明确错误(auth/bad_request)→请求被拒未计费，释放预留、不重试；超时/服务端错误→unknown 保守占用、有限重试。
    预留与结算使用对应模型价格。"""
    n_frames = sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "user"
                   for c in m.get("content", []) if isinstance(c, dict) and c.get("type") == "image_url")
    max_out = getattr(transport, "max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS)
    vlm_cfg = model_cfg or model_config.get_vlm_runtime(conn, _task_cfg_ref(conn, task_id))
    pricing = _pricing_for(vlm_cfg)
    estimate = token_cost_micro(pricing, EST_INPUT_PER_FRAME * n_frames, max_out, 0)
    last = None
    for attempt in range(max_retries + 1):
        rid = f"{request_id}-a{attempt}"
        request_journal.ensure_legacy_requests(conn,task_id)
        recorded = request_journal.recorded_cost(conn,task_id,rid)
        if not recorded:
            request_journal.assert_clear(conn, task_id)
        reserve = recorded or cost.reserve_cost(conn, task_id, max(1, estimate), rid,
                                    provider=vlm_cfg.get("provider") or "deepseek",
                                    model=vlm_cfg.get("model") or "deepseek-flash",
                                    operation="compare", retry_of=None if attempt == 0 else f"{request_id}-a{attempt-1}",
                                    execution_id=execution_id,call_source="mock" if isinstance(transport,MockVlmTransport) else "real")
        if not recorded and reserve["status"] != "reserved":
            raise security.SecurityError("request_already_closed","该请求费用已关闭，不得重发")
        try:
            resp = request_journal.dispatch(conn, task_id, execution_id, reserve["cost_id"], rid, vlm_cfg, messages, transport, 300)
        except security.SecurityError:
            cost.release_unsent(conn,reserve["cost_id"],rid)
            raise
        except VlmNetworkError as e:
            if not e.retryable:
                # 认证/权限/模型不存在/请求格式：请求被拒、未计费，释放预留，不重试
                cost.settle_cost(conn, reserve["cost_id"], 0, {}, estimate_basis="invoice")
                raise security.SecurityError(f"eval_{e.code}", str(e))
            cost.mark_unknown(conn, reserve["cost_id"], note=f"{e.code}:{n_frames}帧:{str(e)[:120]}")
            last = e
            if attempt >= max_retries:
                break
            time.sleep(10)  # 重试前等待，避免对可能已处理的超时请求立即重复
            continue
        except Exception as e:
            cost.mark_unknown(conn, reserve["cost_id"], note=f"unknown:{type(e).__name__}:{str(e)[:100]}")
            last = e
            if attempt >= max_retries:
                break
            continue
        usage = resp.get("usage", {}) if isinstance(resp, dict) else {}
        pt = int(usage.get("prompt_tokens", 0) or 0); ct = int(usage.get("completion_tokens", 0) or 0)
        ch = int(usage.get("prompt_cache_hit_tokens", 0) or 0)
        if not usage or (pt == 0 and ct == 0):
            cost.mark_unknown(conn, reserve["cost_id"], note="missing usage")
            request_journal.quarantine(conn,task_id)
            raise security.SecurityError("pending_reconciliation", "响应缺少 usage，保留 unknown 费用，禁止重发")
            last = VlmNetworkError("missing usage", code="network")
            if attempt >= max_retries:
                break
            continue
        actual = token_cost_micro(pricing, pt, ct, ch)
        cost.settle_cost(conn, reserve["cost_id"], actual, usage, estimate_basis="usage")
        return resp, reserve["cost_id"]
    raise security.SecurityError("eval_failed", f"多次重试失败：{last}")


def _task_cfg_ref(conn, task_id):
    row = conn.execute("SELECT model_config_ref_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if row is None:
        raise security.SecurityError("task_not_found", f"任务不存在：{task_id}")
    return json.loads(row["model_config_ref_json"]) if row["model_config_ref_json"] else None


def evaluate_photo_frames(conn, task_id, frames, transport, request_id, execution_id=None, model_cfg=None) -> dict:
    """单批照片评估（一次调用）。"""
    messages = _build_photo_messages(frames)
    resp, cost_id = _guarded_call(conn, task_id, messages, transport, request_id, execution_id, model_cfg=model_cfg)
    expected = [f"f{i}" for i in range(len(frames))]
    ann = parse_promo_frames(resp["text"], expected)
    t_map = {f"f{i}": frames[i]["t_ms"] for i in range(len(frames))}
    src_map = {f"f{i}": frames[i].get("source", "") for i in range(len(frames))}
    for a in ann:
        a["t_ms"] = t_map[a["frame_id"]]
        a["source"] = src_map.get(a["frame_id"], "")
    return {"annotations": ann, "cost_id": cost_id}


def evaluate_photo_batched(conn, task_id, frames, transport, request_id, execution_id,
                           batch_size=6, model_cfg=None) -> dict:
    """照片分批评估（不持久化批次，仅供一次性小批量探测）；正式对照用 eval_run.run_photo_batched。"""
    all_anns: list[dict] = []
    cost_ids: list[str] = []
    for i in range(0, len(frames), batch_size):
        batch = frames[i:i + batch_size]
        bid = f"{request_id}-b{i // batch_size}"
        messages = _build_photo_messages(batch)
        resp, cost_id = _guarded_call(conn, task_id, messages, transport, bid, execution_id, model_cfg=model_cfg)
        expected = [f"f{j}" for j in range(len(batch))]
        anns = parse_promo_frames(resp["text"], expected)
        t_map = {f"f{j}": batch[j]["t_ms"] for j in range(len(batch))}
        src_map = {f"f{j}": batch[j].get("source", "") for j in range(len(batch))}
        for a in anns:
            a["t_ms"] = t_map[a["frame_id"]]
            a["source"] = src_map.get(a["frame_id"], "")
        all_anns.extend(anns)
        cost_ids.append(cost_id)
    return {"annotations": all_anns, "cost_ids": cost_ids}


def evaluate_video_segment(conn, task_id, ordered_frames, transport, request_id, execution_id=None, model_cfg=None, candidate_bounds=None, context_bounds=None) -> dict:
    messages = _build_video_messages(ordered_frames)
    from . import quality_policy, continuous_quality
    detailed = quality_policy.version(conn,task_id)>=2
    locked_context = quality_policy.context(conn,task_id)
    if locked_context and not detailed:
        messages.append({'role':'user','content':locked_context})
    if detailed:
        messages[0]['content']=continuous_quality.PROMPT
        from .fullscan import _get_pref
        task=conn.execute('SELECT preference_ref_json,requirement FROM tasks WHERE task_id=?',(task_id,)).fetchone()
        pref=_get_pref(conn,json.loads(task['preference_ref_json'] or 'null'))
        messages.append({'role':'user','content':locked_context or ('任务锁定偏好：'+json.dumps(pref,ensure_ascii=False)+'\n本次要求：'+str(task['requirement'] or '按锁定偏好'))})
        if quality_policy.version(conn,task_id)>=3:
            messages.append({'role':'user','content':continuous_quality.input_manifest(ordered_frames,candidate_bounds or (ordered_frames[0]['t_ms'],ordered_frames[-1]['t_ms']))})
    if candidate_bounds:
        messages.append({"role":"user","content":f"受审区间为 {candidate_bounds[0]}—{candidate_bounds[1]} ms，外侧帧仅作上下文。完整合格就返回原边界；不得因采样中心点而缩边。边界应含自然动作起止，说明删去部分的可见缺陷依据。"})
    resp, cost_id = _guarded_call(conn, task_id, messages, transport, request_id, execution_id, model_cfg=model_cfg)
    t_min = min(f["t_ms"] for f in ordered_frames)
    t_max = max(f["t_ms"] for f in ordered_frames)
    seg = (continuous_quality.parse(resp['text'],ordered_frames,candidate_bounds or (t_min,t_max),context_bounds or (t_min,t_max))
           if detailed else parse_video_segment(resp["text"], *(context_bounds or (t_min,t_max))))
    seg["cost_id"] = cost_id
    seg["frames"] = ordered_frames
    return seg

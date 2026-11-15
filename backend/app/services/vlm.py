"""视觉模型调用：必须经过费用护栏；区分网络失败/解析失败；严格校验帧集合；真实 DeepSeek 客户端。

- transport 必须显式传入（生产缺 transport → 报错）；mock 只能由测试显式注入，结果标注 source。
- 金额按官方 token 计价 + 返回 usage 折算为微元；预留用保守上界，结算按 usage 但标注「usage 估算」而非正式账单。
- 缺少 usage 时不得按 0 结算并释放预留，保持 unknown（保守占用预算）。
- frame_id 由服务端映射源时间戳，模型只回 frame_id；发送前移除内部字段 _frame_id/_t_ms。
"""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from .. import security
from . import request_journal
from . import cost, model_config
from .cost import TokenPricing, pricing_for_provider, token_cost_micro

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
ESTIMATE_INPUT_TOKENS_PER_FRAME = 1500
ESTIMATE_OUTPUT_TOKENS_PER_FRAME = 200  # 每帧输出 token 上界（观测约 100-250）
DEFAULT_MAX_OUTPUT_TOKENS = 8000


class VlmNetworkError(Exception):
    """网络/超时/服务端错误。code：auth(401/403)、bad_request(400，格式/模型不存在)、
    rate_limited(429)、server_error(5xx)、timeout、network。auth/bad_request 请求被拒未计费；
    timeout/server_error 是否计费未知。"""

    def __init__(self, message: str, code: str = "network"):
        super().__init__(message)
        self.code = code

    @property
    def retryable(self) -> bool:
        return self.code not in ("auth", "bad_request")


class VlmParseError(Exception):
    """成功响应但内容不合法：调用已计费，按 usage 结算，解析错误单独处理。"""


@dataclass
class VlmResult:
    annotations: list[dict[str, Any]]
    cost_id: str | None
    cost_status: str
    coverage: dict[str, Any]
    source: str
    retries: int = 0


def _clean_messages(messages: list[dict]) -> list[dict]:
    """深拷贝并移除发给供应商前的内部字段（_frame_id/_t_ms 等）。"""
    import copy
    out = []
    for m in messages:
        m2 = {"role": m.get("role")}
        content = m.get("content")
        if isinstance(content, list):
            blocks = []
            for c in content:
                if isinstance(c, dict):
                    b = {k: v for k, v in c.items() if not k.startswith("_")}
                    blocks.append(b)
                else:
                    blocks.append(c)
            m2["content"] = blocks
        else:
            m2["content"] = content
        out.append(m2)
    return out


class DeepSeekTransport:
    """DeepSeek OpenAI 兼容 /chat/completions 客户端。使用任务锁定的模型配置与引用名解析密钥。"""

    def __init__(self, max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS, temperature: float = 0.3,
                 client: httpx.Client | None = None, enable_thinking: bool | None = None):
        self.max_output_tokens = max_output_tokens
        self.temperature = temperature
        self._client = client
        # None=按 provider 自动（deepseek 禁用思考、dashscope 禁用推理）；显式传入则覆盖
        self.enable_thinking = enable_thinking

    def call(self, model_cfg: dict, messages: list[dict], timeout_s: float) -> dict:
        model = (model_cfg or {}).get("model") or DEFAULT_MODEL
        base_url = ((model_cfg or {}).get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        ref = (model_cfg or {}).get("api_key_ref") or ""
        api_key = security.resolve_secret(ref)
        if not api_key:
            raise VlmNetworkError(f"密钥未配置：引用 {ref!r} 无法解析", code="auth")
        provider = ((model_cfg or {}).get("provider") or "deepseek").lower()
        payload = {
            "model": model,
            "messages": _clean_messages(messages),
            "max_tokens": self.max_output_tokens,
            "temperature": self.temperature,
        }
        # DeepSeek 专属：默认开启思考模式会耗尽输出，须显式禁用；其它供应商不发此参数
        if provider == "deepseek":
            payload["thinking"] = {"type": "disabled"}
        # 百炼千问：默认开推理（reasoning），需显式关闭，避免推理挤占输出
        if provider == "dashscope":
            payload["enable_thinking"] = self.enable_thinking if self.enable_thinking is not None else False
        headers = {"Authorization": f"Bearer {api_key}"}
        url = f"{base_url}/chat/completions"
        try:
            if self._client is not None:
                resp = self._client.post(url, json=payload, headers=headers)
            else:
                resp = httpx.post(url, json=payload, headers=headers, timeout=timeout_s)
        except httpx.TimeoutException as e:
            raise VlmNetworkError(f"timeout: {e}", code="timeout") from e
        except httpx.HTTPError as e:
            raise VlmNetworkError(f"http error: {e}", code="network") from e
        if resp.status_code in (401, 403):
            raise VlmNetworkError(f"认证/权限错误 {resp.status_code}", code="auth")
        if resp.status_code == 400:
            raise VlmNetworkError(f"请求/模型错误 {resp.status_code}", code="bad_request")
        if resp.status_code == 429:
            raise VlmNetworkError(f"限流 {resp.status_code}", code="rate_limited")
        if resp.status_code >= 500:
            raise VlmNetworkError(f"server error {resp.status_code}", code="server_error")
        if resp.status_code != 200:
            raise VlmNetworkError(f"status {resp.status_code}", code="network")
        data = resp.json()
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise VlmParseError(f"响应缺 choices/message/content: {e}") from e
        usage = data.get("usage") or {}
        supplier_request_id = (resp.headers.get("x-dashscope-request-id")
                               or resp.headers.get("x-request-id") or resp.headers.get("request-id") or None)
        return {
            "text": text,
            "usage": {
                "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
                "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
                "prompt_cache_hit_tokens": int(usage.get("prompt_cache_hit_tokens", 0) or 0),
            },
            "supplier_request_id": supplier_request_id,
            "completion_id": data.get("id"),
            "response_model": data.get("model"),
            "finish_reason": data["choices"][0].get("finish_reason"),
        }


class MockVlmTransport:
    """离线替身：按 frame_id 返回确定性结构化结果。仅测试显式注入，不冒充真实效果。"""

    def __init__(self, aes_score: float = 0.88, quality_pass: bool = True):
        self.aes_score = aes_score
        self.quality_pass = quality_pass

    def call(self, model_cfg: dict, messages: list[dict], timeout_s: float) -> dict:
        system = str(messages[0].get("content", ""))
        if 'quality_pass' in system and 'quality_floor_pass' not in system:
            if 'promo_reason' in system:
                return {'text':json.dumps({'quality_pass':self.quality_pass,'promo_worthy':self.quality_pass,
                        'promo_reason':'mock only, not actual quality','issues':[],
                        'observations':{'face':'full','eyes':'open','paws':'not_visible','clarity':'sharp','expression':'natural'},
                        'evidence':{'face':'mock face','eyes':'mock eyes','paws':'mock not visible'}}),
                        'usage':{'prompt_tokens':1500,'completion_tokens':200}}
            return {"text":json.dumps({"quality_pass":self.quality_pass,"issues":[]}),"usage":{"prompt_tokens":1500,"completion_tokens":100}}
        if 'segment_usable' in system:
            import re
            text = str(messages[-1].get("content",""))
            bounds = re.search(r"受审区间为 (\d+)—(\d+)",text)
            if bounds:
                if '"evidence"' in system:
                    ids=self._frame_ids(messages)
                    segment={'start_ms':int(bounds[1]),'end_ms':int(bounds[2]),'natural_start':True,'natural_end':True,
                             'start_reason':'mock outside context','end_reason':'mock outside context','reason':'mock only'}
                    return {'text':json.dumps({'segment_usable':self.quality_pass,'segments':[segment] if self.quality_pass else [],
                            'evidence':[{'frame_id':fid,'viewing':'good' if self.quality_pass else 'defect',
                                         'face':'full','eyes':'open','paws':'natural','clarity':'sharp','reason':'mock only'} for fid in ids],
                            'omissions':[],'reason':'mock only, not actual quality'}),
                            'usage':{'prompt_tokens':3000,'completion_tokens':400}}
                return {"text":json.dumps({"segment_usable":self.quality_pass,"usable_start_ms":int(bounds[1]),"usable_end_ms":int(bounds[2]),"reason":"mock context"}),"usage":{"prompt_tokens":3000,"completion_tokens":100}}
        frame_ids = self._frame_ids(messages)
        annotations = [
            {"frame_id": fid, "caption": "mock: 清晰可见的猫", "quality_floor_pass": self.quality_pass,
             "observations": {"face_visible": "full", "eye_state": "open",
                              "expression_natural": True, "clarity": "sharp"},
             "scores": {"sharpness": 0.9, "eye": 0.85, "face": 0.8},
             "promo_worthy": True, "preference_hits": [], "aes_score": self.aes_score}
            for fid in frame_ids
        ]
        return {
            "text": json.dumps({"frames": annotations}, ensure_ascii=False),
            "usage": {"prompt_tokens": 1500 * len(frame_ids), "completion_tokens": 200 * len(frame_ids),
                      "prompt_cache_hit_tokens": 0},
        }

    @staticmethod
    def _frame_ids(messages: list[dict]) -> list[str]:
        ids = []
        for m in messages:
            if isinstance(m, dict) and m.get("role") == "user":
                for c in m.get("content", []):
                    if isinstance(c, dict) and c.get("type") == "image_url":
                        ids.append(c.get("_frame_id", ""))
        return ids


def build_understand_messages(frames: list[dict], preference: dict, requirement: str | None = None) -> list[dict]:
    """把结构化偏好注入提示词；可选自然语言需求也加入，实际影响分析与筛选。"""
    """把结构化偏好注入提示词，并为每张图加 frame_id + 时间点文字；模型回 frame_id，服务端映射时间。"""
    qf = preference.get("quality_floor", {})
    vr = preference.get("visual_rules", {})
    system = (
        "你是猫舍选片视觉评估器。只依据画面内容，不编造看不到的细节。\n"
        "对每帧分别记录「观察（事实）」与「审美判断（好不好）」，输出严格 JSON：\n"
        '{"frames":[{"frame_id":"f0","caption":"≤100字",'
        '"observations":{"face_visible":"full|side|cropped|occluded",'
        '"eye_state":"open|half|closed|blinking","expression_natural":true或false,"clarity":"sharp|soft|blurry"},'
        '"quality_floor_pass":true或false,"scores":{"sharpness":0-1,"eye":0-1,"face":0-1},'
        '"posture_quality":"自然|别扭|其他","overall_harmony":0-1,'
        '"promo_worthy":true或false,"preference_hits":["命中项"],"aes_score":0-1}]}\n'
        "字段规则：face_visible 指脸部可见程度——full=正脸完整可见；side=完整侧脸（整个脸都可见，只是角度侧）；cropped=脸部被画面边缘裁切掉一部分；occluded=脸部被物体/毛发遮挡。\n"
        "observations 是事实描述；scores/aes_score 是审美判断；quality_floor_pass 是「基本可用」（技术合格）；promo_worthy 是「精选推荐」（值得选出交付，比基本可用更严）。\n"
        "先检查明显缺陷：肢体/爪子姿态别扭、明显遮挡、脸部不完整、表情不自然——有明显缺陷的照片不得进精选（promo_worthy=false）。\n"
        "再对合格照片做整体比较：overall_harmony=整体协调性（肢体自然、姿态舒展、位置观感协调）。不要简单把脸/眼/鼻/爪分数相加；正脸高分不能掩盖整体姿态问题。\n"
        "侧脸允许，但不能侧到影响脸部和眼睛完整展示；不要用「必须正脸、必须直视」硬规则。主体偏一侧是构图问题（可裁剪），不直接据此淘汰；主体审美与构图可修复性分别写进 caption。\n"
        "照片用途（单帧宣传）更严：脸部完整可见、眼睛睁开、表情自然、清晰，才算好照片；侧脸/眨眼/张嘴不直接一票否决，但要在 observations 写清。\n"
        "视频用途看连续观看效果：允许自然眨眼、侧脸过渡、张嘴动作；但明显遮挡或持续闭眼不算好。\n"
        "动作加分（抬爪/歪头等）不能抵消明显画面问题（严重模糊、严重遮挡、脸部不可见）。\n"
        "好帧参考：正脸、圆眼有神、脸部完整、表情自然、清晰。需避免：侧脸过多、闭眼/半闭、表情不自然、脸部被挡。\n"
        f"基础质量（不可放宽）：{json.dumps(qf.get('required', []), ensure_ascii=False)}\n"
        f"审美优先项：{json.dumps(vr.get('priority', []), ensure_ascii=False)}\n"
        f"审美加分项：{json.dumps(vr.get('bonus', []), ensure_ascii=False)}\n"
        f"动作加分（不可挡脸）：{json.dumps(vr.get('action_bonus', []), ensure_ascii=False)}\n"
        "缺少条件时按传入的放宽顺序处理；不得把相对最好说成宣传合格。"
        + (f"\n用户额外需求：{requirement}" if requirement else "")
    )
    content = [{"type": "text", "text": "请逐帧评估以下画面（每张图上方标注其 frame_id 与时间点）。"}]
    for i, f in enumerate(frames):
        fid = f"f{i}"
        content.append({"type": "text", "text": f"frame_id={fid} 时间点={f['t_ms']}ms"})
        content.append({"type": "image_url", "image_url": {"url": f["data_url"]}, "_frame_id": fid, "_t_ms": f["t_ms"]})
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": content},
    ]


def _finite_01(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v)) and 0.0 <= float(v) <= 1.0


def parse_annotation(text: str, expected: list[dict]) -> list[dict]:
    """严格校验：布尔类型、有限数值、评分范围、frame_id 集合完整（不存在/重复/遗漏/越界一律拒绝）。"""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`").removeprefix("json").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise VlmParseError(f"无法解析为 JSON: {e}") from e
    frames = data.get("frames") if isinstance(data, dict) else None
    if not isinstance(frames, list):
        raise VlmParseError("缺少 frames 数组")

    expected_ids = {e["frame_id"] for e in expected}
    t_ms_map = {e["frame_id"]: e["t_ms"] for e in expected}
    identity_map = {e["frame_id"]: {k: e[k] for k in ("source_pts", "time_base", "pts_ms") if k in e} for e in expected}
    seen: set[str] = set()
    out: list[dict] = []
    for f in frames:
        if not isinstance(f, dict):
            raise VlmParseError("frames 含非对象项")
        fid = f.get("frame_id")
        if not isinstance(fid, str) or fid not in expected_ids:
            raise VlmParseError(f"frame_id 不存在或越界：{fid!r}")
        if fid in seen:
            raise VlmParseError(f"frame_id 重复：{fid!r}")
        seen.add(fid)
        if not isinstance(f.get("quality_floor_pass"), bool):
            raise VlmParseError(f"frame_id {fid} 的 quality_floor_pass 不是布尔值")
        scores = f.get("scores")
        if not isinstance(scores, dict) or not all(_finite_01(v) for v in scores.values()):
            raise VlmParseError(f"frame_id {fid} 的 scores 缺失或越界")
        aes = f.get("aes_score")
        if not _finite_01(aes):
            raise VlmParseError(f"frame_id {fid} 的 aes_score 缺失或越界")
        hits = f.get("preference_hits", [])
        if not isinstance(hits, list) or not all(isinstance(h, str) for h in hits):
            raise VlmParseError(f"frame_id {fid} 的 preference_hits 非法")
        obs = f.get("observations")
        if obs is not None:
            if not isinstance(obs, dict):
                raise VlmParseError(f"frame_id {fid} 的 observations 非法")
            if "expression_natural" in obs and not isinstance(obs["expression_natural"], bool):
                raise VlmParseError(f"frame_id {fid} 的 expression_natural 不是布尔值")
        pw = f.get("promo_worthy")
        if pw is not None and not isinstance(pw, bool):
            raise VlmParseError(f"frame_id {fid} 的 promo_worthy 不是布尔值")
        harmony = f.get("overall_harmony")
        if harmony is not None and not _finite_01(harmony):
            raise VlmParseError(f"frame_id {fid} 的 overall_harmony 缺失或越界")
        pq = f.get("posture_quality")
        if pq is not None and pq not in ("自然", "别扭", "其他"):
            raise VlmParseError(f"frame_id {fid} 的 posture_quality 非法")
        out.append({
            **identity_map[fid],
            "t_ms": t_ms_map[fid],
            "frame_id": fid,
            "caption": str(f.get("caption", ""))[:200],
            "observations": dict(obs) if isinstance(obs, dict) else {},
            "quality_floor_pass": f["quality_floor_pass"],
            "scores": {k: float(v) for k, v in scores.items()},
            "preference_hits": hits,
            "aes_score": float(aes),
            "promo_worthy": pw,
            "posture_quality": pq,
            "overall_harmony": float(harmony) if harmony is not None else None,
        })
    missing = expected_ids - seen
    if missing:
        raise VlmParseError(f"frame_id 遗漏：{sorted(missing)}")
    return out


def understand_frames(conn: sqlite3.Connection, task_id: str, frames: list[dict], preference: dict,
                      transport: Callable, request_id: str | None = None,
                      pricing: TokenPricing | None = None,
                      max_retries: int = 2, execution_id: str | None = None) -> VlmResult:
    """护栏化看图调用：预留(保守上界)→调用→结算(按 usage 估算)；网络失败→unknown+重试；解析失败→结算+单独报错。"""
    if transport is None:
        raise security.SecurityError("transport_required", "必须提供模型 transport，禁止隐式回退到模拟替身")
    if not frames:
        raise security.SecurityError("no_frames", "没有可分析的帧")
    source = "mock" if isinstance(transport, MockVlmTransport) else "real"

    task = conn.execute("SELECT model_config_ref_json, requirement FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if task is None:
        raise security.SecurityError("task_not_found", f"任务不存在：{task_id}")
    cfg_ref = json.loads(task["model_config_ref_json"]) if task["model_config_ref_json"] else None
    requirement = task["requirement"]
    # 按任务锁定的 id/version 读取完整 VLM 配置（不是最新配置），传给客户端
    vlm_cfg = model_config.get_vlm_runtime(conn, cfg_ref)
    # 按服务商统一计价（与 compare/质检同价格表）；mock 用默认价不触发网络
    pricing = pricing or pricing_for_provider(vlm_cfg.get("provider"))

    expected = [{**f, "frame_id": f"f{i}"} for i, f in enumerate(frames)]
    messages = build_understand_messages(frames, preference, requirement)
    # 预留上界：输入按每帧 token 上界，输出按每帧输出上界（而非整段硬上限，避免预留过保守）
    max_out = getattr(transport, "max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS)
    out_est = min(max_out, ESTIMATE_OUTPUT_TOKENS_PER_FRAME * len(frames))
    estimate = token_cost_micro(pricing, ESTIMATE_INPUT_TOKENS_PER_FRAME * len(frames), out_est, 0)
    base_rid = request_id or f"understand-{task_id}-{frames[0]['t_ms']}-{frames[-1]['t_ms']}"

    last_net_err: Exception | None = None
    for attempt in range(max_retries + 1):
        attempt_rid = f"{base_rid}-a{attempt}"
        request_journal.ensure_legacy_requests(conn,task_id)
        recorded = request_journal.recorded_cost(conn,task_id,attempt_rid)
        if not recorded:
            request_journal.assert_clear(conn, task_id)
        reserve = recorded or cost.reserve_cost(conn, task_id, max(1, estimate), attempt_rid,
                                    provider=vlm_cfg.get("provider") or "deepseek",
                                    model=vlm_cfg.get("model") or DEFAULT_MODEL,
                                    operation="understand", retry_of=None if attempt == 0 else f"{base_rid}-a{attempt-1}",
                                    execution_id=execution_id,call_source=source)
        if not recorded and reserve["status"] != "reserved":
            raise security.SecurityError("request_already_closed","该请求费用已关闭，不得重发")
        try:
            resp = request_journal.dispatch(conn, task_id, execution_id, reserve["cost_id"], attempt_rid, vlm_cfg, messages, transport, 60)
        except security.SecurityError:
            cost.release_unsent(conn,reserve["cost_id"],attempt_rid)
            raise
        except VlmNetworkError as e:
            if not e.retryable:
                # 认证/权限/模型不存在/请求格式：请求被拒、未计费，释放预留，不重试
                cost.settle_cost(conn, reserve["cost_id"], 0, {}, estimate_basis="invoice")
                raise security.SecurityError(f"vlm_{e.code}", str(e)) from e
            # 网络失败：是否计费未知，保留保守预留标 unknown
            cost.mark_unknown(conn, reserve["cost_id"], note=f"{type(e).__name__}: {e}")
            last_net_err = e
            continue
        except VlmParseError as e:
            cost.mark_unknown(conn, reserve["cost_id"], note=f"{type(e).__name__}: {e}")
            last_net_err = e
            continue
        except Exception as e:
            cost.mark_unknown(conn, reserve["cost_id"], note=f"error {type(e).__name__}")
            last_net_err = e
            continue

        # 成功拿到响应（已计费）：按返回 usage 结算
        usage = resp.get("usage", {}) if isinstance(resp, dict) else {}
        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        cache_hit = int(usage.get("prompt_cache_hit_tokens", 0) or 0)
        if not usage or (prompt_tokens == 0 and completion_tokens == 0):
            # 缺 usage：不得按 0 结算并释放预留，保持 unknown 保守占用
            cost.mark_unknown(conn, reserve["cost_id"], note="missing usage")
            request_journal.quarantine(conn,task_id)
            raise security.SecurityError("pending_reconciliation", "响应缺少 usage，保留 unknown 费用，禁止重发")
            last_net_err = VlmNetworkError("missing usage")
            continue
        actual = token_cost_micro(pricing, prompt_tokens, completion_tokens, cache_hit)
        usage_out = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                     "prompt_cache_hit_tokens": cache_hit}
        if resp.get("supplier_request_id"):
            usage_out["supplier_request_id"] = resp["supplier_request_id"]
        try:
            ann = parse_annotation(resp.get("text", ""), expected)
        except VlmParseError as e:
            cost.settle_cost(conn, reserve["cost_id"], actual, usage_out, estimate_basis="usage")
            raise security.SecurityError("parse_failed", str(e)) from e

        settled = cost.settle_cost(conn, reserve["cost_id"], actual, usage_out, estimate_basis="usage")
        return VlmResult(
            annotations=ann,
            cost_id=settled["cost_id"],
            cost_status=settled["status"],
            coverage={"frames_analyzed": len(ann), "frame_ids": [a["frame_id"] for a in ann],
                      "start_ms": ann[0]["t_ms"], "end_ms": ann[-1]["t_ms"]},
            source=source,
            retries=attempt,
        )

    raise security.SecurityError("vlm_failed", f"看图调用多次重试仍失败：{last_net_err}")

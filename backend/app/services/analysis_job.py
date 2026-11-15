"""分析任务：显式启动、进度/失败、后台执行（本轮用模拟模型，源明确标注 mock）。"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone

from .. import db, security
from ..config import settings
from . import fullscan
from .vlm import DeepSeekTransport, MockVlmTransport
_running: set[str] = set()
_lock = threading.Lock()


def build_transport(mode: str):
    """真实模式用 DeepSeekTransport（任务锁定配置+预算护栏在 understand_frames 内）；默认模拟。"""
    return DeepSeekTransport() if mode == "real" else MockVlmTransport()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_id(task_id: str) -> str:
    return f"ana-{task_id[:24]}"


def _new_conn() -> sqlite3.Connection:
    return db.get_conn(settings.db_path)


def start_analysis(conn: sqlite3.Connection, task_id: str, mode: str = "mock") -> str:
    """显式启动任务分析：复用管线+任务配置+预算护栏；默认 mock，real 须显式选择。"""
    if mode not in ("mock", "real"):
        raise security.SecurityError("invalid_mode", "mode 只能是 mock 或 real")
    from . import tasks as tasks_svc
    task = tasks_svc.get_task(conn, task_id)
    if task.get("budget_limit_minor") is None:
        raise security.SecurityError("budget_unconfigured", "预算未配置，禁止启动分析")
    if task["status"] not in ("assets_locked", "failed", "ready_for_review"):
        raise security.SecurityError("invalid_transition", f"任务状态 {task['status']} 不可启动分析")

    from .request_journal import assert_clear, ensure_legacy_requests
    ensure_legacy_requests(conn,task_id)
    assert_clear(conn, task_id)
    if mode == 'real':
        from .request_journal import assert_sources_clear
        assert_sources_clear(conn,task_id)
        if not task.get('model_config_ref_json'):
            raise security.SecurityError('model_unconfigured','本任务没有锁定模型配置；请管理员完成设置后新建任务，不能临时套用另一版本')
    if conn.execute("SELECT 1 FROM analysis_runs WHERE task_id=? AND status='pending_reconciliation'", (task_id,)).fetchone():
        raise security.SecurityError("pending_reconciliation", "历史在途运行须核账后处置")
    assets_list = task["asset_refs"]
    if not assets_list:
        raise security.SecurityError("no_assets", "任务没有绑定素材，无法启动分析")

    jid = _job_id(task_id)
    now = _now()
    row = conn.execute("SELECT 1 FROM analysis_jobs WHERE analysis_job_id=?", (jid,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO analysis_jobs(analysis_job_id,task_id,status,mode,stage,asset_index,total_assets,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (jid, task_id, "running", mode, "粗筛", 0, len(assets_list), now, now),
        )
    else:
        conn.execute(
            "UPDATE analysis_jobs SET status='running', mode=?, stage='粗筛', asset_index=0, error=NULL, updated_at=? WHERE analysis_job_id=?",
            (mode, now, jid),
        )
    conn.execute("UPDATE tasks SET status='analyzing', outcome=NULL, error_json=NULL, updated_at=? WHERE task_id=?",
                 (now, task_id))
    conn.commit()
    _spawn(jid)
    return jid


def resume_running_jobs() -> None:
    """服务启动恢复：上次进程中断遗留的 running analysis_runs 标 failed（可重试），再继续执行 running 作业。
    mock 离线确定性：worker 内会直接重试完成；real：待核对费用后重试，不盲目重发。"""
    conn = _new_conn()
    try:
        from .request_journal import ensure_legacy_requests
        ensure_legacy_requests(conn)
        conn.execute("UPDATE model_requests SET status='pending_reconciliation',updated_at=? WHERE status='inflight'",(_now(),))
        rows = conn.execute("SELECT * FROM analysis_jobs WHERE status='running'").fetchall()
        for job in rows:
            tid = job["task_id"]
            if job["mode"] == "real":
                interrupted = conn.execute("SELECT * FROM analysis_runs WHERE task_id=? AND status='running'", (tid,)).fetchall()
                all_responses = True
                for run in interrupted:
                    scope = json.loads(run["coverage_json"] or "{}").get("request_scope")
                    rid = f"{tid}-{run['asset_id']}-{scope}-a0"
                    response = conn.execute("SELECT 1 FROM model_requests WHERE request_id=? AND status='responded' AND response_json IS NOT NULL", (rid,)).fetchone()
                    if scope and response:
                        conn.execute("UPDATE analysis_runs SET status='response_available' WHERE run_id=?", (run['run_id'],))
                    else:
                        all_responses = False
                waiting = conn.execute("SELECT 1 FROM model_requests WHERE task_id=? AND status IN ('inflight','pending_reconciliation') LIMIT 1",(tid,)).fetchone()
                unrecoverable = False
                for failed in conn.execute("SELECT * FROM analysis_runs WHERE task_id=? AND source='real' AND status='failed' AND created_at>=?",(tid,job["updated_at"])).fetchall():
                    scope=json.loads(failed["coverage_json"] or "{}").get("request_scope")
                    rid=f"{tid}-{failed['asset_id']}-{scope}-a0"
                    recorded=conn.execute("SELECT 1 FROM model_requests WHERE request_id=? AND status='responded'",(rid,)).fetchone()
                    if scope and recorded:
                        conn.execute("UPDATE analysis_runs SET status='response_available' WHERE run_id=?",(failed['run_id'],))
                    else:unrecoverable=True
                if all_responses and not waiting and not unrecoverable:
                    _spawn(job["analysis_job_id"])
                    continue
                if unrecoverable and not waiting and not interrupted:
                    _fail_job(conn,job["analysis_job_id"],tid,RuntimeError("已知失败响应无法恢复，请显式重试；未自动重发"),"incomplete_error")
                    continue
                conn.execute("UPDATE analysis_runs SET status='pending_reconciliation' WHERE task_id=? AND status='running'", (tid,))
                conn.execute("UPDATE model_requests SET status='pending_reconciliation',updated_at=? WHERE task_id=? AND status='inflight'", (_now(),tid))
                for cr in conn.execute("SELECT * FROM cost_records WHERE task_id=? AND status IN ('reserved','unknown')",(tid,)).fetchall():
                    conn.execute("INSERT OR IGNORE INTO model_requests(request_id,task_id,execution_id,cost_id,status,input_digest,input_json,error,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                 (cr['request_id'],tid,cr['execution_id'],cr['cost_id'],'pending_reconciliation','legacy-input-unavailable','{}','历史在途费用；原始输入缺失，须供应商核账',_now(),_now()))
                conn.execute("UPDATE analysis_jobs SET status='pending_reconciliation',error='进程中断：先核账，禁止自动重发' WHERE analysis_job_id=?", (job["analysis_job_id"],))
                conn.execute("UPDATE tasks SET status='pending_reconciliation',outcome='incomplete_error' WHERE task_id=? AND status='analyzing'", (tid,))
            else:
                from . import cost
                for cr in conn.execute("SELECT * FROM cost_records WHERE task_id=? AND status='reserved' AND call_source='mock'",(tid,)).fetchall():
                    cost.release_unsent(conn,cr['cost_id'],cr['request_id'])
                conn.execute("UPDATE analysis_runs SET status='failed' WHERE task_id=? AND status='running' AND source='mock'", (tid,))
                _spawn(job["analysis_job_id"])
    finally:
        conn.close()


def _spawn(jid: str) -> None:
    with _lock:
        if jid in _running:
            return
        _running.add(jid)
    t = threading.Thread(target=_run, args=(jid,), daemon=True)
    t.start()


def _update(conn: sqlite3.Connection, jid: str, *, stage: str, asset_index: int, current_asset: str | None = None) -> None:
    job = conn.execute("SELECT task_id FROM analysis_jobs WHERE analysis_job_id=?", (jid,)).fetchone()
    if job:
        task = conn.execute("SELECT status FROM tasks WHERE task_id=?", (job["task_id"],)).fetchone()
        if task and task["status"] != "analyzing":
            raise security.SecurityError("invalid_transition","任务已停止，不再进入下一分析阶段")
    conn.execute(
        "UPDATE analysis_jobs SET stage=?, asset_index=?, current_asset_id=?, updated_at=? WHERE analysis_job_id=?",
        (stage, asset_index, current_asset, _now(), jid),
    )
    conn.commit()


MAX_RECHECK_RETRY = 2  # 同版本 error 最多重试次数
FRAME_IDENTITY_VERSION = 2  # 源帧身份：v2=round-matching（历史整数毫秒四舍五入定位帧）


QUALITY_RECHECK_PROMPT = (
    "你是猫舍单张宣传照片审核员。此照片将独立交付，请逐项观察实际像素，再判断。\n"
    "输出严格 JSON：{\"quality_pass\":true或false,\"issues\":[具体依据],\"observations\":{"
    "\"face\":\"full|side|insufficient|occluded\",\"eyes\":\"open|half|closed|unclear\","
    "\"paws\":\"natural|awkward|not_visible\",\"clarity\":\"sharp|soft|blurry\","
    "\"expression\":\"natural|unnatural|unclear\"}}\n"
    "观察眼睑是否真的睁开，不要把眯眼和半闭写成睁眼；即使视频中是正常眨眼，单张照片也不选。\n"
    "完整侧脸、静态姿态可优秀；若脸转离镜头到只见后脑、脸藏在胸毛或看不到足够面部细节，应记 insufficient。\n"
    "检查爪子是否舒展自然，还是被物品勾住、肢体折叠别扭；抬爪不能自动等于优秀。\n"
    "检查前景虚化物是否遮住脸、眼睛或主体关键轮廓，检查主体对焦、运动重影。\n"
    "脸部展示不足、眼睛半闭/闭上/看不清、爪姿别扭、主体明显模糊遮挡或表情不自然均不得通过。\n"
    "不要将这些可见缺陷降格为仅相对略差。主体位置与构图另说明，偏侧不直接淘汰。\n"
    "没有证据不得编造硬伤；完整脸部与侧脸、自然眼神、清晰、肢体自然可以通过。\n"
)

QUALITY_RECHECK_REF_PROMPT = (
    "你是猫舍宣传照片质检员。下面第一张是「候选照片」，第二张是同一只猫的「参考优秀照片」。\n"
    "判断候选照片是否存在「明显硬伤」（相对参考）：\n"
    "- 爪子别扭/像被勾住/悬空不自然\n"
    "- 脸部展示不佳/角度差到不饱满/几乎看不到脸\n"
    "- 眼睛眯住/无神\n"
    "- 明显模糊/残影/虚影\n"
    "- 遮挡面部或关键部位\n"
    "输出严格 JSON：{\"fail\":true或false,\"issues\":[具体问题]}\n"
    "只有存在明显硬伤才 fail=true。相对参考只是角度稍侧、光线略暗、动作略逊，不构成硬伤，fail=false。静态、侧脸本身不算硬伤。\n"
)


def _quality_prompt(conn, task_id, *, policy_version=None):
    from .quality_policy import version, context
    if (version(conn,task_id) if policy_version is None else policy_version)<2:
        return QUALITY_RECHECK_PROMPT + context(conn, task_id)
    from .fullscan import _get_pref
    row=conn.execute('SELECT preference_ref_json,requirement FROM tasks WHERE task_id=?',(task_id,)).fetchone()
    preference=_get_pref(conn,json.loads(row['preference_ref_json'] or 'null'))
    return (QUALITY_RECHECK_PROMPT+
            '\n另外必须输出 promo_worthy 布尔、promo_reason 非空字符串，以及 evidence 对象（face、eyes、paws三个非空文字依据）。'
            'quality_pass仅指技术可用；promo_worthy指值得独立宣传精选，基本可用不能自动算精选。'
            '先明确实际像素中眼睑、面部可见轮廓和爪子所处位置，再下结论，不用“可爱、圆眼、抬爪”套话代替观察。'
            '完整侧脸、静态可以优秀；爪自然藏起或不入镜不等于勾住；前景/背景虚化不等于主体失焦。'
            '无法确认的事实明确写出，不能用猜测放行，也不能捏造硬伤。单张判断独立于任何参考照片。'
            + (context(conn, task_id) or ('\n任务锁定偏好：'+json.dumps(preference,ensure_ascii=False)+
            '\n本次用户要求：'+str(row['requirement'] or '按锁定偏好'))))


def _check_version(conn: sqlite3.Connection, task_id: str, ref_t: int | None = None, batch: str | None = None) -> str:
    """质检缓存身份：质检提示词（单图+参考比较）+ 任务锁定模型配置版本 + 参考图身份/比较方式 + 相关分析批次。
    ref_t 非空=照片参考图相对比较（参考图身份参与身份，避免更换参考后复用旧判断）；batch=最新 execution_id。"""
    import hashlib as _hl
    from .quality_input import DETAIL_INSTRUCTIONS
    cfg = conn.execute("SELECT model_config_ref_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if batch is None:
        b = conn.execute(
            "SELECT execution_id FROM analysis_runs WHERE task_id=? AND status='complete' AND execution_id IS NOT NULL ORDER BY created_at DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        batch = b["execution_id"] if b else "nb"
    ref_part = f"ref:{ref_t}" if ref_t is not None else "single"
    return _hl.sha256((_quality_prompt(conn,task_id) + DETAIL_INSTRUCTIONS + "soft-confirm-only-v3" + (cfg["model_config_ref_json"] or "") + ref_part + (batch or "") + f"fidv{FRAME_IDENTITY_VERSION}").encode()).hexdigest()[:12]


def _parse_recheck(text: str, *, strict: bool = False):
    """严格校验质检响应：quality_pass 布尔、issues 字符串列表；无效则抛异常。"""
    import re as _re
    if not isinstance(text, str) or not text.strip():
        raise ValueError("空响应")
    start = text.find("{")
    if start < 0:
        raise ValueError("非 JSON 响应")
    d, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(d, dict):
        raise ValueError("响应不是对象")
    qp = d.get("quality_pass")
    if not isinstance(qp, bool):
        raise ValueError("quality_pass 不是布尔值")
    issues = d.get("issues", [])
    if not isinstance(issues, list) or not all(isinstance(x, str) for x in issues):
        raise ValueError("issues 不是字符串列表")
    obs = d.get("observations")
    if strict:
        evidence=d.get('evidence')
        if not isinstance(obs,dict) or not isinstance(d.get('promo_worthy'),bool) or not isinstance(d.get('promo_reason'),str) or not d['promo_reason'].strip():
            raise ValueError('照片缺少客观观察或独立宣传价值判断')
        if not isinstance(evidence,dict) or any(not isinstance(evidence.get(k),str) or not evidence[k].strip() for k in ('face','eyes','paws')):
            raise ValueError('照片缺少脸、眼、爪的实际可见依据')
        if not d['promo_worthy']:
            qp=False
            issues=[*issues,'技术合格不等于精选：'+d['promo_reason']]
    if obs is not None:
        enums = {"face":{"full","side","insufficient","occluded"},"eyes":{"open","half","closed","unclear"},
                 "paws":{"natural","awkward","not_visible"},"clarity":{"sharp","soft","blurry"},
                 "expression":{"natural","unnatural","unclear"}}
        if not isinstance(obs,dict) or any(obs.get(k) not in values for k,values in enums.items()):
            raise ValueError("质检观察字段缺失或枚举非法")
        defects = [k+"="+obs[k] for k,bad in {"face":{"insufficient","occluded"},"eyes":{"half","closed","unclear"},
                   "paws":{"awkward"},"clarity":{"blurry"},"expression":{"unnatural"}}.items() if obs[k] in bad]
        if defects:
            qp = False
            issues += defects
    return qp, issues


def _parse_recheck_ref(text: str):
    """参考图相对比较：fail 布尔、issues 字符串列表。"""
    import re as _re
    if not isinstance(text, str) or not text.strip():
        raise ValueError("空响应")
    start = text.find("{")
    if start < 0:
        raise ValueError("非 JSON 响应")
    d, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(d, dict):
        raise ValueError("响应不是对象")
    fail = d.get("fail")
    if not isinstance(fail, bool):
        raise ValueError("fail 不是布尔值")
    issues = d.get("issues", [])
    if not isinstance(issues, list) or not all(isinstance(x, str) for x in issues):
        raise ValueError("issues 不是字符串列表")
    return fail, issues


def _soft_only_rejection(text: str) -> bool:
    """Only a factual, fully visible open-eyed frame with soft focus qualifies.
    A real defect or missing observation never gets overturned by another flattering view.
    """
    try:
        raw, _ = json.JSONDecoder().raw_decode(text[text.index("{"):])
        obs = raw.get("observations") or {}
        return (raw.get("quality_pass") is False and obs.get("face") in ("full","side")
                and obs.get("eyes") == "open" and obs.get("paws") in ("natural","not_visible")
                and obs.get("clarity") == "soft" and obs.get("expression") == "natural")
    except (ValueError,TypeError,AttributeError):
        return False


def _recheck_photos(conn: sqlite3.Connection, task_id: str, asset_id: str, transport, execution_id: str | None) -> int:
    """Shared recommendation pool, exact source inputs, durable paid-response reuse.
    The detail path only confirms soft-only rejections, never overturns hard defects.
    """
    from pathlib import Path
    from . import compare, quality_input, assets, batch_identity
    from .results import get_task_results
    import hashlib
    source = Path(assets.check_export_asset(conn,task_id,asset_id))
    cv = _check_version(conn,task_id,None,execution_id)
    view = get_task_results(conn,task_id,execution_overrides={asset_id:execution_id},require_qc=False)
    from .quality_policy import version as policy_version, selection_version
    quality_v2=policy_version(conn,task_id)>=2
    queue_v2=selection_version(conn,task_id)>=2
    candidates = [p for p in view['recommended_photos'] if p['asset_id']==asset_id]
    if not queue_v2:
        candidates.sort(key=lambda p:p['t_ms'])
    if queue_v2:
        from .results import _warm_frame_features
        _warm_frame_features(conn,candidates)
    accepted=[]
    done = 0
    for candidate in candidates:
        t=candidate['t_ms']
        if queue_v2:
            from .results import _visual_dedup
            if len(_visual_dedup([*accepted,candidate],conn,threshold=16))==len(accepted):
                continue  # Only a PASSED representative suppresses an unchecked neighbour.
        cid = "pc-"+batch_identity.digest([task_id,asset_id,execution_id,t,cv,"photo"])
        old = conn.execute("SELECT * FROM photo_checks WHERE check_id=?",(cid,)).fetchone()
        if old and (old["status"] in ("pass","fail") or old["retry_count"] >= MAX_RECHECK_RETRY):
            if old['status']=='pass':accepted.append(candidate)
            continue
        evidence = []
        def check(detail):
            messages, pts = quality_input.messages(source,t,_quality_prompt(conn,task_id),detail=detail)
            digest = hashlib.sha256(json.dumps(messages,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
            cached = conn.execute("SELECT request_id FROM model_requests WHERE task_id=? AND execution_id=? AND input_digest=? AND status='responded' ORDER BY created_at,request_id LIMIT 1",(task_id,execution_id,digest)).fetchone()
            request_id = cached[0][:-3] if cached and cached[0].endswith('-a0') else f"qc-{cid}-{'detail' if detail else 'full'}"
            response,cost_id = compare._guarded_call(conn,task_id,messages,transport,request_id,execution_id,max_retries=0)
            passed, issues = _parse_recheck(response.get("text", ""),strict=quality_v2)
            evidence.append({"input_digest":digest,"pts_ms":pts,"cost_id":cost_id,"detail":detail,"quality_pass":passed,"issues":issues})
            if quality_v2:
                evidence[-1]['assessment']=json.JSONDecoder().raw_decode(response['text'][response['text'].index('{'):])[0]
            return passed,issues,response.get("text", "")
        try:
            passed,issues,text = check(False)
            if not passed and _soft_only_rejection(text):
                detail_pass,detail_issues,_ = check(True)
                if detail_pass:
                    passed,issues = True,["软焦疑点经同帧细节复核通过；保留两次观察证据"]
                else:
                    issues += detail_issues
            status = "pass" if passed else "fail"
        except Exception as exc:
            if isinstance(exc,security.SecurityError) and exc.code in ("budget_exceeded","workspace_budget_exceeded","budget_unconfigured","pending_reconciliation","source_pending_reconciliation","invalid_transition"):
                raise
            status,passed,issues = "error",None,[f"{type(exc).__name__}: {str(exc)[:120]}"]
        conn.execute("INSERT OR REPLACE INTO photo_checks(check_id,task_id,asset_id,t_ms,check_version,status,quality_pass,issues_json,retry_count,check_kind,tier,created_at,evidence_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (cid,task_id,asset_id,t,cv,status,passed,json.dumps(issues,ensure_ascii=False),
                      (old["retry_count"]+1) if old else 0,"photo","feature" if passed else "fail",_now(),json.dumps(evidence,ensure_ascii=False)))
        done += 1
        if passed:accepted.append(candidate)
    return done


def _densify_boundary_gaps(conn: sqlite3.Connection, task_id: str, asset_id: str, transport,
                           execution_id: str | None = None, max_densify: int = 8) -> int:
    """边界证据不足自动补查：针对候选起止附近 + 大间隙；归属同一批次（execution_id）。"""
    from . import pipeline
    from .analysis import SelectParams, boundary_gaps, merge_natural_segments, _classify_roles
    from ..media.meta import read_video_metadata
    from pathlib import Path as _P
    row = conn.execute("SELECT source_path, media_type FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
    if row is None:
        return 0
    if row["media_type"] == "image":
        return 0
    duration_ms = read_video_metadata(_P(row["source_path"]))["duration_ms"]
    from .batch_identity import frames as batch_frames
    ann = batch_frames(conn,task_id,asset_id,execution_id)
    if not ann:
        return 0
    params = SelectParams()
    roles = _classify_roles(ann, params)
    segs = merge_natural_segments(ann, params)
    idx_by_t = {f["t_ms"]: i for i, f in enumerate(ann)}
    targets: set[tuple[int, int]] = set()
    for (g0, g1) in boundary_gaps(ann, params):
        targets.add((g0, g1))
    for seg in segs:
        si = idx_by_t.get(seg["start_ms"])
        if si is not None and si > 0 and roles[si - 1] == "ok":
            targets.add((ann[si - 1]["t_ms"], seg["start_ms"]))
        ei = idx_by_t.get(seg["end_ms"])
        if ei is not None and ei < len(ann) - 1 and roles[ei + 1] == "ok":
            targets.add((seg["end_ms"], ann[ei + 1]["t_ms"]))
    # 稀疏采样不确定过渡：非硬坏 bad 段两侧好帧间隔过大 → 局部补查
    from .analysis import _is_hard_bad
    i, n = 0, len(ann)
    while i < n:
        if roles[i] == "bad" and not _is_hard_bad(ann[i]):
            j = i
            while j < n and roles[j] == "bad" and not _is_hard_bad(ann[j]):
                j += 1
            prev_t = ann[i - 1]["t_ms"] if i > 0 else ann[i]["t_ms"]
            next_t = ann[j]["t_ms"] if j < n else ann[j - 1]["t_ms"]
            if next_t - prev_t > params.gap_ms:
                targets.add((prev_t, next_t))
            i = j
        else:
            i += 1
    done = 0
    from .fullscan import effective_sampling, _sampling_from_task
    max_densify = effective_sampling(_sampling_from_task(conn,task_id))["boundary_max_windows"]
    for (g0, g1) in sorted(targets)[:max_densify]:
        mid = (g0 + g1) // 2
        pad = min(1000, max(300, (g1 - g0) // 3))
        s = max(0, mid - pad)
        e = min(duration_ms, mid + pad)
        if e - s < 500:
            continue
        pipeline.analyze_asset(conn, task_id, asset_id, transport, s, e,
                               sampling={"max_frames": 4, "frames_per_sec": 4.0, "resize_edge": 600},
                               execution_id=execution_id)
        done += 1
    return done


def _evaluate_video_segments(conn: sqlite3.Connection, task_id: str, asset_id: str, transport, execution_id: str | None) -> int:
    """段级连续上下文判断：对自然段采样连续帧，调用 compare.evaluate_video_segment 得到自然起止，写入 video_evals。"""
    from . import compare
    from .analysis import SelectParams, merge_natural_segments
    from ..media.meta import read_video_metadata
    from ..media.sample import sample_frames
    from pathlib import Path as _P
    row = conn.execute("SELECT source_path, media_type FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
    if row is None or row["media_type"] == "image":
        return 0
    md = read_video_metadata(_P(row["source_path"]))
    from .batch_identity import frames as batch_frames, video_version, digest
    ann = batch_frames(conn,task_id,asset_id,execution_id)
    if not ann:
        return 0
    segs = merge_natural_segments(ann, SelectParams())
    version = video_version(conn,task_id)
    snapshot=json.loads(conn.execute('SELECT snapshot_ref_json FROM tasks WHERE task_id=?',(task_id,)).fetchone()[0] or '{}')
    done = 0
    for seg in segs:
        start, end = seg["start_ms"], seg["end_ms"]
        # Endpoint and outside-context frames are separate from the interval being judged.
        # A 12-frame midpoint grid must never silently shorten an accepted interval.
        lo, hi = max(0,start-1000), min(md["duration_ms"]-1,end+1000)
        input_plan = None
        if snapshot.get('video_input_version',0)>=1:
            from . import video_input
            fs,input_plan=video_input.build(row['source_path'],(start,end),md,ann,
                video_input.current_photo_inspections(conn,task_id,asset_id,execution_id))
        else:
            fs = sample_frames(_P(row["source_path"]), lo, hi, max_frames=48, frames_per_sec=2,
                               resize_edge=600,duration_ms=md["duration_ms"],rotation=md["rotation"])
            for t in (lo,start,max(start,end-1),hi):
                fs.extend(sample_frames(_P(row["source_path"]), t,t+1,max_frames=1,frames_per_sec=1,
                          resize_edge=600,duration_ms=md["duration_ms"],rotation=md["rotation"]))
            fs = sorted({f["t_ms"]: f for f in fs}.values(),key=lambda f:f["t_ms"])
        identity = digest({"task":task_id,"asset":asset_id,"batch":execution_id,"version":version,
                           "start":start,"end":end,"frames":fs})
        eid = "ve-" + identity
        existing = conn.execute("SELECT status FROM video_evals WHERE eval_id=?", (eid,)).fetchone()
        if existing:
            continue  # error/pending are visible, never silently send again
        status, error, result = "complete", None, {}
        try:
            result = compare.evaluate_video_segment(conn,task_id,fs,transport,eid,execution_id,
                                                    candidate_bounds=(start,end),context_bounds=(lo,hi))
        except Exception as exc:
            status, error = "error", str(exc)[:500]
            if isinstance(exc,security.SecurityError) and exc.code in ("budget_exceeded","workspace_budget_exceeded","budget_unconfigured","pending_reconciliation","source_pending_reconciliation","invalid_transition"):
                raise
        conn.execute("INSERT INTO video_evals(eval_id,task_id,asset_id,execution_id,start_ms,end_ms,segment_usable,usable_start_ms,usable_end_ms,reason,cost_id,created_at,judgment_version,input_digest,status,error,input_json,assessment_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (eid,task_id,asset_id,execution_id,start,end, int(bool(result.get("segment_usable"))),
                      result.get("usable_start_ms"),result.get("usable_end_ms"),result.get("reason"),result.get("cost_id"),
                      _now(),version,identity,status,error,json.dumps({"bounds":[start,end],"context":[lo,hi],"frames":fs,"sampling_plan":input_plan}),
                      json.dumps({k:v for k,v in result.items() if k!='frames'},ensure_ascii=False)))
        done += 1
    return done


def _fail_job(conn,jid,task_id,error,outcome):
    from ..db import atomic
    with atomic(conn):
        state=conn.execute("SELECT status FROM tasks WHERE task_id=?",(task_id,)).fetchone()[0]
        if state=="cancelled":
            conn.execute("UPDATE analysis_jobs SET status='cancelled',stage='已取消',error=NULL,updated_at=? WHERE analysis_job_id=?",(_now(),jid))
            return
        pending=state=="pending_reconciliation" or getattr(error,"code",None)=="pending_reconciliation"
        if state=="completed":return
        job_status="pending_reconciliation" if pending else "failed"
        conn.execute("UPDATE analysis_jobs SET status=?,error=?,updated_at=? WHERE analysis_job_id=?",(job_status,str(error)[:500],_now(),jid))
        conn.execute("UPDATE tasks SET status=?,outcome=?,error_json=?,updated_at=? WHERE task_id=? AND status IN ('analyzing','pending_reconciliation')",(job_status,outcome,json.dumps({"error":str(error)[:200],"code":getattr(error,"code",None)},ensure_ascii=False),_now(),task_id))


def _run_v3(conn: sqlite3.Connection, jid: str, task_id: str, transport, asset_ids: list[str]) -> None:
    """v3 新路径作业：每素材走 selection_v2.analyze_asset_v3（本阶段 mock），不经过旧 fullscan/质检/连续判断。"""
    from . import selection_v2
    for i, aid in enumerate(asset_ids, 1):
        _update(conn, jid, stage="v3选片", asset_index=i, current_asset=aid)
        result = selection_v2.analyze_asset_v3(conn, task_id, aid, transport)
        conn.execute("UPDATE executions SET status='complete', updated_at=? WHERE execution_id=?", (_now(), result.get("execution_id")))
        conn.commit()
    from .results import get_task_results
    actual = get_task_results(conn, task_id)
    total_photos = len(actual.get("curated_photos") or [])
    total_videos = len(actual.get("curated_videos") or [])
    has_video_assets = any(
        conn.execute("SELECT media_type FROM assets WHERE asset_id=?", (aid,)).fetchone()["media_type"] == "video"
        for aid in asset_ids
    )
    if total_photos == 0 and total_videos == 0:
        outcome = "none_qualified"
    elif has_video_assets and (total_photos == 0 or total_videos == 0):
        outcome = "partial"
    else:
        outcome = "complete"
    from ..db import atomic
    with atomic(conn):
        if conn.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()[0] != "analyzing":
            return
        conn.execute("UPDATE analysis_jobs SET status='completed', stage='完成', asset_index=?, updated_at=? WHERE analysis_job_id=? AND status='running'", (len(asset_ids), _now(), jid))
        conn.execute("UPDATE tasks SET status='ready_for_review', outcome=?, updated_at=? WHERE task_id=? AND status='analyzing'", (outcome, _now(), task_id))


def _run(jid: str) -> None:
    conn = _new_conn()
    try:
        job = conn.execute("SELECT * FROM analysis_jobs WHERE analysis_job_id=?", (jid,)).fetchone()
        if job is None:
            return
        task_id = job["task_id"]
        transport = build_transport(job["mode"] or "mock")
        asset_rows = conn.execute("SELECT asset_id FROM task_assets WHERE task_id=?", (task_id,)).fetchall()
        asset_ids = [r["asset_id"] for r in asset_rows]
        total_photos = 0
        total_videos = 0
        try:
            from .quality_policy import is_v3
            if is_v3(conn, task_id):
                _run_v3(conn, jid, task_id, transport, asset_ids)
                return
            for i, aid in enumerate(asset_ids, 1):
                _update(conn, jid, stage="粗筛", asset_index=i, current_asset=aid)
                unfinished = conn.execute("SELECT execution_id FROM executions WHERE task_id=? AND asset_id=? AND status IN ('running','processing','failed') ORDER BY created_at DESC LIMIT 1",(task_id,aid)).fetchone()
                result = fullscan.full_clip_analyze(conn, task_id, aid, transport, publish=False,
                                                    execution_id=unfinished[0] if unfinished else None,
                                                    on_progress=lambda st, j=jid, idx=i, a=aid: _update(conn, j, stage=st, asset_index=idx, current_asset=a))
                conn.execute("UPDATE executions SET status='processing' WHERE execution_id=?", (result.get("execution_id"),))
                total_photos += len(result.get("photo_frames") or [])
                total_videos += len(result.get("video_segments") or [])
                # 边界证据不足自动补查：与本次全片同批次（execution_id）
                _update(conn, jid, stage="补查", asset_index=i, current_asset=aid)
                _densify_boundary_gaps(conn, task_id, aid, transport,
                                       execution_id=(result or {}).get("execution_id"))
                # 交付质检复查：对 promo_worthy 帧查清晰度/遮挡/姿态/整体观感（受预算护栏）
                _update(conn, jid, stage="质检", asset_index=i, current_asset=aid)
                _recheck_photos(conn, task_id, aid, transport, (result or {}).get("execution_id"))
                _update(conn, jid, stage="寻找眼神与表情峰值", asset_index=i, current_asset=aid)
                if fullscan.refine_photo_peaks(conn,task_id,aid,transport,result.get('execution_id')):
                    _recheck_photos(conn,task_id,aid,transport,result.get('execution_id'))
                # 段级连续上下文判断：自然段起止与内部坏画面（写入 video_evals）
                _update(conn, jid, stage="视频段判断", asset_index=i, current_asset=aid)
                _evaluate_video_segments(conn, task_id, aid, transport, (result or {}).get("execution_id"))
                conn.execute("UPDATE executions SET status='complete',updated_at=? WHERE execution_id=?", (_now(),result.get("execution_id")))
        except security.SecurityError as e:
            # 预算类错误单独落 incomplete_budget（可提额续跑）；其余 incomplete_error
            outcome = "incomplete_budget" if e.code in ("budget_exceeded", "workspace_budget_exceeded", "budget_unconfigured") else "incomplete_error"
            _fail_job(conn,jid,task_id,e,outcome)
            return
        except Exception as e:
            _fail_job(conn,jid,task_id,e,"incomplete_error")
            return

        from .results import get_task_results
        actual = get_task_results(conn,task_id)
        total_photos = len(actual["recommended_photos"])
        total_videos = len(actual["video_segments"])
        # 按实际结果计算 outcome：空结果 none_qualified；只有一类 partial；两类 complete
        if actual.get('quality_errors'):
            outcome = 'incomplete_quality'
        elif total_photos == 0 and total_videos == 0:
            outcome = "none_qualified"
        elif total_photos and all(a['media_type']=='image' for a in task_asset_views(conn,task_id)):
            outcome = 'complete'  # an image-only task does not owe a nonexistent video result
        elif total_photos == 0 or total_videos == 0:
            outcome = "partial"
        else:
            outcome = "complete"
        from ..db import atomic
        with atomic(conn):
            if conn.execute("SELECT status FROM tasks WHERE task_id=?",(task_id,)).fetchone()[0] != "analyzing":
                return
            conn.execute("UPDATE analysis_jobs SET status='completed',stage='完成',asset_index=?,updated_at=? WHERE analysis_job_id=? AND status='running'",(len(asset_ids),_now(),jid))
            conn.execute("UPDATE tasks SET status='ready_for_review',outcome=?,updated_at=? WHERE task_id=? AND status='analyzing'",(outcome,_now(),task_id))
    finally:
        conn.close()
        with _lock:
            _running.discard(jid)


def task_asset_views(conn, task_id):
    return conn.execute('SELECT a.media_type FROM assets a JOIN task_assets ta ON ta.asset_id=a.asset_id WHERE ta.task_id=?',(task_id,)).fetchall()


def get_analysis_job(conn: sqlite3.Connection, jid: str) -> dict:
    row = conn.execute("SELECT * FROM analysis_jobs WHERE analysis_job_id=?", (jid,)).fetchone()
    if row is None:
        raise security.SecurityError("analysis_job_not_found", f"分析任务不存在：{jid}")
    return dict(row)


def get_task_analysis(conn: sqlite3.Connection, task_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM analysis_jobs WHERE task_id=? ORDER BY updated_at DESC LIMIT 1", (task_id,)).fetchone()
    return dict(row) if row else None

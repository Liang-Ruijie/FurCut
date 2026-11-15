"""Crash-safe request dispatch. Uncertain real requests require evidenced reconciliation.

No provider-independent exactly-once guarantee exists. This journal deliberately stops
at the uncertainty boundary, keeps reservations and raw responses, and never resends.
"""
from __future__ import annotations
import hashlib
import json
from datetime import datetime, timezone
from .. import db, security


def now():
    return datetime.now(timezone.utc).isoformat()


def assert_clear(conn, task_id):
    if conn.execute("SELECT 1 FROM model_requests WHERE task_id=? AND (status IN ('inflight','pending_reconciliation') OR (status='responded' AND cost_id IN (SELECT cost_id FROM cost_records WHERE status IN ('unknown','reserved')))) LIMIT 1", (task_id,)).fetchone():
        raise security.SecurityError('pending_reconciliation', '存在真实在途/不确定请求，须先核账，禁止重发')


def assert_sources_clear(conn, task_id):
    """A new task must not bypass unresolved requests on the same locked source.

    Keep other, unrelated assets usable. Legacy fees without an execution identity
    conservatively protect the previous task's entire source set.
    """
    from .cost import archived_workspace_ledger
    held = set(archived_workspace_ledger(conn)["pending_fingerprints"])
    if held.intersection(r[0] for r in conn.execute("SELECT fingerprint_at_lock FROM task_assets WHERE task_id=?", (task_id,))):
        raise security.SecurityError('source_pending_reconciliation','相同原片在归档账本中仍有待核账请求，请先核对费用；不能通过新工作区重发')
    row=conn.execute("""SELECT c.task_id FROM cost_records c
        LEFT JOIN model_requests r ON r.cost_id=c.cost_id
        LEFT JOIN executions e ON e.execution_id=c.execution_id
        WHERE c.task_id!=? AND c.call_source!='mock'
        AND (c.status IN ('unknown','reserved') OR r.status IN ('inflight','pending_reconciliation'))
        AND EXISTS (SELECT 1 FROM task_assets current JOIN task_assets previous
                    ON previous.asset_id=current.asset_id WHERE current.task_id=?
                    AND previous.task_id=c.task_id AND (c.execution_id IS NULL OR e.asset_id IS NULL OR e.asset_id=current.asset_id))
        LIMIT 1""",(task_id,task_id)).fetchone()
    if row:
        raise security.SecurityError('source_pending_reconciliation','相同原片在其他任务有待核账请求，不能通过新任务重发；请管理员先核对费用')


def dispatch(conn, task_id, execution_id, cost_id, request_id, cfg, messages, transport, timeout_s):
    from .vlm import MockVlmTransport, VlmNetworkError
    if isinstance(transport, MockVlmTransport):
        return transport.call(cfg, messages, timeout_s=timeout_s)
    payload = json.dumps(messages, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    with db.atomic(conn):
        row = conn.execute('SELECT * FROM model_requests WHERE request_id=?', (request_id,)).fetchone()
        if row:
            if row['input_digest'] != digest:
                raise security.SecurityError('request_conflict', '请求身份相同但输入不同')
            if row['status'] == 'responded':
                return json.loads(row['response_json'])
            raise security.SecurityError('pending_reconciliation', '请求已发送或已拒绝；不得重复发送同一请求')
        assert_clear(conn, task_id)
        assert_sources_clear(conn, task_id)
        task = conn.execute('SELECT status FROM tasks WHERE task_id=?', (task_id,)).fetchone()
        if task is None or task['status'] not in ('analyzing','selecting','assets_locked','exporting'):
            raise security.SecurityError('invalid_transition', '任务已停止，禁止发送请求')
        conn.execute('INSERT INTO model_requests(request_id,task_id,execution_id,cost_id,status,input_digest,input_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                     (request_id,task_id,execution_id,cost_id,'inflight',digest,payload,now(),now()))
    from .vlm import _clean_messages
    metadata = {"provider":cfg.get("provider"),"model":cfg.get("model"),"base_url":cfg.get("base_url"),
                "max_tokens":getattr(transport,"max_output_tokens",None),"temperature":getattr(transport,"temperature",None),
                "enable_thinking":getattr(transport,"enable_thinking",None),"wire_messages":_clean_messages(messages)}
    conn.execute("UPDATE model_requests SET request_meta_json=? WHERE request_id=?",(json.dumps(metadata,ensure_ascii=False),request_id))
    try:
        response = transport.call(cfg, messages, timeout_s=timeout_s)
    except Exception as exc:
        status = 'rejected' if isinstance(exc, VlmNetworkError) and not exc.retryable else 'pending_reconciliation'
        conn.execute('UPDATE model_requests SET status=?,error=?,updated_at=? WHERE request_id=?', (status,str(exc)[:500],now(),request_id))
        if status == 'pending_reconciliation':
            quarantine(conn,task_id)
            from . import cost
            cost.mark_unknown(conn, cost_id, note='dispatch uncertain; reconciliation required')
            raise security.SecurityError(status, '真实请求结果不确定，已保留费用与输入，禁止自动重试') from exc
        raise
    conn.execute("UPDATE model_requests SET status='responded',response_json=?,updated_at=? WHERE request_id=?",
                 (json.dumps(response,ensure_ascii=False),now(),request_id))
    return response


def reconcile(conn, task_id, request_id, evidence, disposition):
    """Operator settlement must already exist. Never clear unknown/reserved costs here."""
    if not isinstance(evidence,str) or len(evidence.strip()) < 10 or disposition not in ('no_response_retry_authorized','do_not_retry'):
        raise security.SecurityError('invalid_reconciliation','须填写供应商核账证据及明确处置')
    with db.atomic(conn):
        row = conn.execute('SELECT r.*, c.status AS cost_status FROM model_requests r JOIN cost_records c ON c.cost_id=r.cost_id WHERE r.task_id=? AND r.request_id=?', (task_id,request_id)).fetchone()
        if not row or row['status'] not in ('inflight','pending_reconciliation','responded'):
            raise security.SecurityError('invalid_reconciliation','请求不存在或不处于待核账状态')
        if row['cost_status'] != 'settled':
            raise security.SecurityError('unsettled_cost','必须依据供应商账单先结算该笔费用，不得冲销 unknown')
        conn.execute('UPDATE model_requests SET status=?,reconciliation_json=?,updated_at=? WHERE request_id=?',
                     ('reconciled',json.dumps({'evidence':evidence,'disposition':disposition},ensure_ascii=False),now(),request_id))
        if disposition == 'no_response_retry_authorized':
            assert_clear(conn,task_id)
            conn.execute("UPDATE analysis_runs SET status='failed' WHERE task_id=? AND status='pending_reconciliation'", (task_id,))
            conn.execute("UPDATE analysis_jobs SET status='failed' WHERE task_id=? AND status='pending_reconciliation'",(task_id,))
            conn.execute("UPDATE tasks SET status='failed',outcome='incomplete_error' WHERE task_id=? AND status='pending_reconciliation'",(task_id,))
    return {'request_id':request_id,'disposition':disposition}


def recorded_cost(conn, task_id, request_id):
    """Reuse a durable response and its existing reservation, never reserve/pay twice."""
    r = conn.execute("SELECT c.* FROM model_requests r JOIN cost_records c ON c.cost_id=r.cost_id WHERE r.task_id=? AND r.request_id=? AND r.status='responded' AND r.response_json IS NOT NULL",(task_id,request_id)).fetchone()
    return dict(r) if r else None


def quarantine(conn,task_id):
    conn.execute("UPDATE tasks SET status='pending_reconciliation',outcome='incomplete_error' WHERE task_id=? AND status IN ('assets_locked','analyzing','selecting','exporting','failed')",(task_id,))
    conn.execute("UPDATE analysis_jobs SET status='pending_reconciliation',error='真实请求待核账，禁止重发' WHERE task_id=? AND status='running'",(task_id,))


def ensure_legacy_requests(conn, task_id=None):
    """Old unresolved fees get audit placeholders; no invented input and no fee changes."""
    sql="SELECT c.* FROM cost_records c WHERE c.status IN ('reserved','unknown') AND c.call_source!='mock' AND NOT EXISTS (SELECT 1 FROM model_requests r WHERE r.cost_id=c.cost_id)"
    args=()
    if task_id is not None:
        sql+=' AND c.task_id=?';args=(task_id,)
    for row in conn.execute(sql,args).fetchall():
        conn.execute("INSERT OR IGNORE INTO model_requests(request_id,task_id,execution_id,cost_id,status,input_digest,input_json,error,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",(row['request_id'],row['task_id'],row['execution_id'],row['cost_id'],'pending_reconciliation','legacy-input-unavailable','{}','旧费用未核账；原始请求输入不可补造',now(),now()))

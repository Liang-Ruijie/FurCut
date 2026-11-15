"""One membership and source-frame evidence rule for analysis, QC and read models."""
from __future__ import annotations
import hashlib
import json


def runs(conn, task_id, asset_id, execution_id):
    members = conn.execute('SELECT r.* FROM batch_members b JOIN analysis_runs r ON r.run_id=b.run_id JOIN executions e ON e.execution_id=b.execution_id WHERE b.execution_id=? AND e.task_id=? AND e.asset_id=? AND r.task_id=e.task_id AND r.asset_id=e.asset_id AND r.status=\'complete\' ORDER BY r.created_at,r.run_id', (execution_id,task_id,asset_id)).fetchall()
    if members or conn.execute('SELECT 1 FROM executions WHERE execution_id=?',(execution_id,)).fetchone():
        return members
    return conn.execute("SELECT * FROM analysis_runs WHERE task_id=? AND asset_id=? AND execution_id=? AND status='complete' ORDER BY created_at,run_id",(task_id,asset_id,execution_id)).fetchall()


def frames(conn, task_id, asset_id, execution_id):
    from .results import _conservative_merge
    by_frame = {}
    for r in runs(conn,task_id,asset_id,execution_id):
        for f in json.loads(r['annotations_json'] or '[]'):
            by_frame.setdefault(f['t_ms'],[]).append({**f,'run_id':r['run_id'],'source':r['source']})
    return [_conservative_merge(g) for _,g in sorted(by_frame.items())]


def video_version(conn, task_id):
    from .compare import PROMPT_VIDEO
    from . import quality_policy, continuous_quality
    row = conn.execute('SELECT model_config_ref_json,snapshot_ref_json FROM tasks WHERE task_id=?',(task_id,)).fetchone()
    if quality_policy.version(conn,task_id)>=2:
        return hashlib.sha256((continuous_quality.PROMPT+'|timeline-evidence-v2|'+str(tuple(row))).encode()).hexdigest()[:24]
    return hashlib.sha256((PROMPT_VIDEO+'|context-endpoints-v3|'+str(tuple(row))).encode()).hexdigest()[:24]


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

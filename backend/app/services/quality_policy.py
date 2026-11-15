"""Task-pinned quality contracts. Missing versions retain the historical reader/input."""
import json

CURRENT_VERSION = 3
RELEASED_VERSION = 1  # Fixed-input real trials did not pass; v3 is isolated opt-in only.


def context(conn, task_id):
    """New tasks pin user intent for *all* downstream quality checks.

    No context marker means legacy input bytes/cache identities stay unchanged.
    Preference values are read by the immutable version reference, never latest.
    """
    row = conn.execute('SELECT snapshot_ref_json FROM tasks WHERE task_id=?', (task_id,)).fetchone()
    snapshot = json.loads(row[0] or '{}') if row else {}
    locked = snapshot.get('quality_context')
    if locked is None:
        return ''
    from .fullscan import _get_pref
    preference = _get_pref(conn, locked.get('preference_ref'))
    return ('\n任务锁定偏好：' + json.dumps(preference, ensure_ascii=False) +
            '\n本次用户要求：' + str(locked.get('requirement') or '按锁定偏好'))


def version(conn, task_id):
    row = conn.execute('SELECT snapshot_ref_json FROM tasks WHERE task_id=?', (task_id,)).fetchone()
    return int(json.loads(row[0] or '{}').get('quality_policy_version', 1)) if row else 1


def selection_version(conn, task_id):
    row=conn.execute('SELECT snapshot_ref_json FROM tasks WHERE task_id=?',(task_id,)).fetchone()
    snapshot=json.loads(row[0] or '{}') if row else {}
    return int(snapshot.get('selection_pipeline_version',2 if snapshot.get('quality_policy_version',1)>=2 else 1))


def is_v3(conn, task_id):
    """是否为 v3 新选片路径（显式版本门；旧代码所有 >=2 判断不得吞掉 v3）。"""
    return selection_version(conn, task_id) >= 3

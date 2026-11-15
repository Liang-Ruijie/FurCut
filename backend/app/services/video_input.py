"""Bounded continuous review inputs, including observations the coarse grid can miss.

QC failures are inspection targets, NOT instructions to cut a video. Normal blinks
can remain valid in motion. No manual review labels or source-specific times enter
this planner. All targets are decoded again from the source.
"""
from pathlib import Path
from ..media.sample import sample_frames, sample_frames_at_times

MAX_FRAMES = 52  # Prior path: at most 48 grid frames plus four endpoints.


def spread(values, limit):
    values = sorted(set(values))
    if limit <= 0:
        return []
    if len(values) <= limit:
        return values
    if limit == 1:
        return [values[len(values)//2]]
    return [values[round(i*(len(values)-1)/(limit-1))] for i in range(limit)]


def build(path, bounds, metadata, annotations, inspection_times=()):
    start, end = bounds
    lo, hi = max(0, start-1000), min(metadata['duration_ms']-1, end+1000)
    from .analysis import _is_hard_bad, _frame_is_exceptional, SelectParams
    suspects = {f['t_ms'] for f in annotations if _is_hard_bad(f) or not f.get('quality_floor_pass')}
    suspects.update(inspection_times)
    suspects = {t for t in suspects if lo <= t <= hi}
    cores = {f['t_ms'] for f in annotations if _frame_is_exceptional(f, {}, SelectParams()) and lo <= f['t_ms'] <= hi}
    endpoints = {lo, start, max(start,end-1), hi}
    # Keep half the budget for continuous temporal coverage, even if many QC
    # disagreements exist. A crowded tail must not hide the rest of the clip.
    selected_suspects = spread(suspects-endpoints, 12)
    selected_cores = spread(cores-endpoints-set(selected_suspects), 8)
    requested = sorted(endpoints | set(selected_suspects) | set(selected_cores))
    frames = sample_frames_at_times(Path(path),requested,resize_edge=600,
                  duration_ms=metadata['duration_ms'],rotation=metadata['rotation'])
    frames.extend(sample_frames(Path(path),lo,hi,max_frames=MAX_FRAMES-len(requested),frames_per_sec=2,
                  resize_edge=600,duration_ms=metadata['duration_ms'],rotation=metadata['rotation']))
    # Actual source identity, not the requested integer millisecond, deduplicates.
    frames = sorted({(f['source_pts'], f['time_base']): f for f in frames}.values(),key=lambda f:f['pts_ms'])
    sampled = {f['t_ms'] for f in frames}
    plan = {'version':1,'max_frames':MAX_FRAMES,'candidate_bounds':[start,end],
            'context_bounds':[lo,hi],'inspection_times':sorted(suspects),
            'included_inspection_times':sorted(suspects & sampled),
            'omitted_inspection_times':sorted(suspects-sampled),
            'core_times':sorted(cores),'included_core_times':sorted(cores & sampled),
            'sampled_times':[f['t_ms'] for f in frames],
            'note':'抽样观察；照片拒绝仅触发同源检查，不自动判视频不合格。'}
    return frames, plan


def current_photo_inspections(conn, task_id, asset_id, execution_id):
    from .analysis_job import _check_version
    version = _check_version(conn, task_id, None, execution_id)
    return [r[0] for r in conn.execute(
        "SELECT t_ms FROM photo_checks WHERE task_id=? AND asset_id=? AND check_version=? AND check_kind='photo' AND status IN ('fail','error')",
        (task_id, asset_id, version))]

"""实验执行编排：领取→护栏化调用→完成/失败记录；未领取则返回处理中/中断/复用，不再调用。"""
from __future__ import annotations

import json
import sqlite3
from typing import Callable

from . import compare, experiment


def _cost_ids(conn: sqlite3.Connection, execution_id: str) -> list[str]:
    rows = conn.execute("SELECT cost_id FROM cost_records WHERE execution_id=?", (execution_id,)).fetchall()
    return [r["cost_id"] for r in rows]


def run_photo_experiment(conn: sqlite3.Connection, task_id: str, frames: list[dict], transport: Callable,
                         ident: str, experiment_key: str, model_cfg: dict | None = None) -> dict:
    e = experiment.acquire(conn, ident, task_id, "photo", experiment_key)
    if e["status"] != "acquired":
        return e
    exec_id = f"{e['experiment_id']}-a{e['attempt']}"
    try:
        r = compare.evaluate_photo_frames(conn, task_id, frames, transport,
                                          request_id=exec_id, execution_id=exec_id, model_cfg=model_cfg)
        experiment.complete(conn, e["experiment_id"], r, _cost_ids(conn, exec_id))
        return {"status": "complete", "reused": False, "experiment_id": e["experiment_id"],
                "attempt": e["attempt"], "retry_of": e["retry_of"], "result": r}
    except Exception as ex:
        experiment.fail(conn, e["experiment_id"], str(ex), _cost_ids(conn, exec_id))
        raise


def run_photo_batched(conn: sqlite3.Connection, task_id: str, frames: list[dict], transport: Callable,
                      ident_fn: Callable, experiment_key: str, batch_size: int = 6,
                      model_cfg: dict | None = None) -> dict:
    """照片分批执行：每批一个实验记录（可复用/失败可恢复）；合并按 frame_id 映射回输入生成全局标识；
    未完成批次（in_progress/interrupted）单独汇总，不把缺少批次当完整成功。"""
    all_anns: list[dict] = []
    batch_results: list[dict] = []
    incomplete: list[int] = []
    for i in range(0, len(frames), batch_size):
        batch = frames[i:i + batch_size]
        bidx = i // batch_size
        ident = ident_fn(batch, bidx, batch_size)
        e = run_photo_experiment(conn, task_id, batch, transport, ident, experiment_key, model_cfg)
        batch_results.append({"batch": bidx, "status": e.get("status"), "reused": e.get("reused"),
                              "experiment_id": e.get("experiment_id")})
        if e.get("status") in ("in_progress", "interrupted"):
            incomplete.append(bidx)
            continue
        if e.get("result"):
            local_map = {f"f{j}": i + j for j in range(len(batch))}
            for a in e["result"]["annotations"]:
                gidx = local_map.get(a["frame_id"])
                if gidx is None:
                    continue
                a["global_frame_id"] = f"g{gidx}"
                a["t_ms"] = frames[gidx]["t_ms"]
                a["source"] = frames[gidx].get("source", "")
                all_anns.append(a)
    status = "incomplete" if incomplete else "complete"
    return {"batches": batch_results, "annotations": all_anns, "status": status,
            "incomplete_batches": incomplete}


def run_video_experiment(conn: sqlite3.Connection, task_id: str, ordered_frames: list[dict], transport: Callable,
                         ident: str, experiment_key: str, group: str, model_cfg: dict | None = None) -> dict:
    e = experiment.acquire(conn, ident, task_id, "video", experiment_key)
    if e["status"] != "acquired":
        return e
    exec_id = f"{e['experiment_id']}-a{e['attempt']}"
    try:
        r = compare.evaluate_video_segment(conn, task_id, ordered_frames, transport,
                                           request_id=exec_id, execution_id=exec_id, model_cfg=model_cfg)
        r["group"] = group
        experiment.complete(conn, e["experiment_id"], r, _cost_ids(conn, exec_id))
        return {"status": "complete", "reused": False, "experiment_id": e["experiment_id"],
                "attempt": e["attempt"], "retry_of": e["retry_of"], "result": r}
    except Exception as ex:
        experiment.fail(conn, e["experiment_id"], str(ex), _cost_ids(conn, exec_id))
        raise

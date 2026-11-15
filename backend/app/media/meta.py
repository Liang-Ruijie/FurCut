"""视频元数据读取：时长/宽高/旋转/帧率/音轨。基于 PyAV + 项目内 ffprobe。"""
from __future__ import annotations

import json
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

import av


def _ffprobe_path() -> str:
    import static_ffmpeg
    static_ffmpeg.add_paths()
    p = shutil.which("ffprobe")
    if not p:
        raise RuntimeError("ffprobe 不可用（static-ffmpeg 未就绪）")
    return p


def read_rotation(path: Path) -> int:
    """读取视频旋转角度（0/90/180/270）。"""
    ffprobe = _ffprobe_path()
    try:
        info = json.loads(subprocess.check_output(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream_side_data=rotation", "-of", "json", str(path)],
            stderr=subprocess.DEVNULL,
        ))
        return next(
            (int(sd["rotation"]) for sd in info.get("streams", [{}])[0].get("side_data_list", []) if "rotation" in sd),
            0,
        )
    except Exception:
        try:
            info = json.loads(subprocess.check_output(
                [ffprobe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream_tags=rotate", "-of", "json", str(path)],
                stderr=subprocess.DEVNULL,
            ))
            tags = info.get("streams", [{}])[0].get("tags", {})
            return int(tags.get("rotate", 0) or 0)
        except Exception:
            return 0


def _read_video_metadata(path: Path) -> dict[str, Any]:
    """读取视频元数据。duration 为毫秒；display_width/height 为旋转后的显示尺寸。"""
    container = av.open(str(path))
    try:
        vs = next((s for s in container.streams if s.type == "video"), None)
        if vs is None:
            raise ValueError(f"No video stream found: {path}")

        duration_ms = 0
        if container.duration is not None:
            duration_ms = round(container.duration / 1000)
        elif vs.duration is not None and vs.time_base is not None:
            duration_ms = round(float(vs.duration * vs.time_base) * 1000)

        raw_w = int(vs.codec_context.width or 0)
        raw_h = int(vs.codec_context.height or 0)
        fps = float(vs.average_rate) if vs.average_rate else float(vs.base_rate or 0)

        audio = next((s for s in container.streams if s.type == "audio"), None)
        has_audio = audio is not None
        audio_sample_rate_hz = int(audio.rate) if audio and audio.rate else 0
    finally:
        container.close()

    rotation = read_rotation(path)
    if abs(rotation) in (90, 270):
        display_w, display_h = raw_h, raw_w
    else:
        display_w, display_h = raw_w, raw_h

    return {
        "duration_ms": duration_ms,
        "width": raw_w,
        "height": raw_h,
        "display_width": display_w,
        "display_height": display_h,
        "rotation": rotation,
        "fps": fps,
        "has_audio": has_audio,
        "audio_sample_rate_hz": audio_sample_rate_hz,
    }


@lru_cache(maxsize=128)
def _cached_metadata(identity: tuple) -> dict[str, Any]:
    return _read_video_metadata(Path(identity[0]))


def read_video_metadata(path: Path) -> dict[str, Any]:
    # Return a fresh dict: a caller must never mutate another task's metadata.
    from .file_identity import file_identity
    return dict(_cached_metadata(file_identity(path)))

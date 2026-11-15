"""视频帧采样：全片覆盖、参数化、返回实际帧时点（毫秒）+ base64 data URL。基于 PyAV。"""
from __future__ import annotations

import base64
import io
import math
from pathlib import Path
from typing import Any

import av
from PIL import Image, ImageOps

DEFAULT_RESIZE_EDGE = 600
DEFAULT_JPEG_QUALITY = 80
DEFAULT_MIN_FRAMES = 1
DEFAULT_MAX_FRAMES = 6
DEFAULT_FRAMES_PER_SEC = 3.0


def choose_num_frames(duration_ms: float, min_frames: int, max_frames: int, frames_per_sec: float) -> int:
    n = int(math.ceil((duration_ms / 1000.0) * frames_per_sec))
    return max(min_frames, min(max_frames, n))


def _pil_to_data_url(img: Image.Image, resize_edge: int, jpeg_quality: int) -> str:
    w, h = img.size
    if resize_edge and max(w, h) > resize_edge:
        scale = resize_edge / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=jpeg_quality)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("utf-8")


def _frame_at_or_after(container: av.container.InputContainer, video, target_s: float):
    """seek 后向前解码，返回首个 PTS >= target_s 的帧（记录实际时点）。"""
    tb = float(video.time_base)
    container.seek(int(target_s / tb), stream=video, backward=True)
    for frame in container.decode(video):
        if frame.pts is None:
            continue
        pts = float(frame.pts * tb)
        if pts >= target_s - 1e-4:
            return frame, pts
    return None, None


def _frames_at_or_after(container, video, targets_s):
    """One forward decode for increasing targets, with the original PTS rule."""
    if not targets_s:return
    container.seek(int(targets_s[0]/float(video.time_base)),stream=video,backward=True)
    decoded=container.decode(video)
    frame=None;pts=None
    for target in targets_s:
        if frame is not None and target-pts>2:
            # Sparse targets in a long file must not decode the entire gap.
            container.seek(int(target/float(video.time_base)),stream=video,backward=True)
            decoded=container.decode(video);frame=None;pts=None
        while frame is None or pts < target-1e-4:
            try:frame=next(decoded)
            except StopIteration:return
            if frame.pts is None:
                frame=None;continue
            pts=float(frame.pts*float(video.time_base))
        yield frame,pts


def _frame_rounding_to(container: av.container.InputContainer, video, target_ms: int):
    """返回四舍五入后等于 target_ms 的那一帧（历史整数毫秒的帧身份）；找不到回退 None。"""
    tb = float(video.time_base)
    container.seek(int((target_ms - 2000) / 1000.0 / tb), stream=video, backward=True)
    for frame in container.decode(video):
        if frame.pts is None:
            continue
        pts = float(frame.pts * tb)
        r = round(pts * 1000)
        if r == target_ms:
            return frame, pts
        if r > target_ms:
            break
    return None, None


def _apply_rotation(img: Image.Image, rotation: int) -> Image.Image:
    """按视频旋转元数据把画面转正（方向已用 ffmpeg 自动旋转输出实测校准）。"""
    rotation = rotation % 360
    if rotation == 90:
        return img.transpose(Image.ROTATE_90)
    if rotation == 180:
        return img.transpose(Image.ROTATE_180)
    if rotation == 270:
        return img.transpose(Image.ROTATE_270)
    return img


def extract_frame_at(path: Path, t_ms: int | float, *, rotation: int | None = None,
                     resize_edge: int | None = None, source_pts: int | None = None, time_base: str | None = None):
    """按源 PTS 取帧：优先取 round(pts*1000)==t_ms 的那一帧（历史整数毫秒的帧身份）；
    找不到则回退到 >= t_ms 的第一帧。旋转转正，可选缩放。
    返回 (PIL Image, 精确 pts_ms)；找不到返回 (None, None)。导出/预览/dHash 共用此函数保证同帧。"""
    if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".heic"}:
        if float(t_ms) != 0 or (source_pts is not None and (source_pts != 0 or time_base != "1/1")):
            return None,None
        with Image.open(path) as source:
            img = ImageOps.exif_transpose(source).convert("RGB")
        if resize_edge:
            img.thumbnail((resize_edge, resize_edge))
        img.info.update(source_pts=0,time_base="1/1")
        return img, 0.0
    if rotation is None:
        from .meta import read_video_metadata
        rotation = read_video_metadata(path)["rotation"]
    container = av.open(str(path))
    try:
        video = next(s for s in container.streams if s.type == "video")
        target = int(round(float(t_ms)))
        if source_pts is not None:
            if time_base != str(video.time_base):
                return None,None
            tb = float(video.time_base)
            container.seek(max(0,source_pts-int(2/tb)),stream=video,backward=True)
            matched = (None,None)
            for exact in container.decode(video):
                if exact.pts == source_pts:
                    matched = exact,float(exact.pts*video.time_base)
                    break
                if exact.pts is not None and exact.pts > source_pts:
                    break
        else:
            matched = _frame_rounding_to(container, video, target)
            if matched[0] is None:
                frame, pts_sec = _frame_at_or_after(container, video, target / 1000.0)
                matched = (frame, pts_sec) if frame is not None else (None, None)
        frame, pts_sec = matched
        if frame is None:
            return None, None
        img = _apply_rotation(frame.to_image(), rotation or 0)
        if resize_edge and max(img.size) > resize_edge:
            scale = resize_edge / max(img.size)
            img = img.resize((int(img.size[0] * scale), int(img.size[1] * scale)))
        img.info.update(source_pts=frame.pts,time_base=str(video.time_base))
        return img, round(pts_sec * 1000, 3)
    finally:
        container.close()


def image_as_frame(path: Path, resize_edge: int = 600) -> dict[str, Any]:
    """图片素材作为单帧（t_ms=0）：读取、转 RGB、缩放、base64 data URL，供看图分析。"""
    from PIL import Image, ImageOps
    with Image.open(str(path)) as source:
        img = ImageOps.exif_transpose(source).convert("RGB")
    if resize_edge and max(img.size) > resize_edge:
        scale = resize_edge / max(img.size)
        img = img.resize((int(img.size[0] * scale), int(img.size[1] * scale)))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=DEFAULT_JPEG_QUALITY)
    return {"t_ms": 0, "source_pts":0, "time_base":"1/1", "pts_ms":0.0, "data_url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("utf-8")}


def sample_frames(path: Path, start_ms: int, end_ms: int, *,
                  max_frames: int = DEFAULT_MAX_FRAMES,
                  frames_per_sec: float = DEFAULT_FRAMES_PER_SEC,
                  min_frames: int = DEFAULT_MIN_FRAMES,
                  resize_edge: int = DEFAULT_RESIZE_EDGE,
                  jpeg_quality: int = DEFAULT_JPEG_QUALITY,
                  duration_ms: int | None = None,
                  rotation: int | None = None) -> list[dict[str, Any]]:
    """在 [start_ms, end_ms] 内按帧率采样，范围裁到有效时长；返回 [{t_ms, data_url}]。
    若 rotation 未显式传入，则读元数据并对画面转正后再编码。"""
    if duration_ms is None or rotation is None:
        from .meta import read_video_metadata
        md = read_video_metadata(path)
        duration_ms = duration_ms if duration_ms is not None else md["duration_ms"]
        rotation = rotation if rotation is not None else md["rotation"]

    start_ms = max(0, min(start_ms, duration_ms))
    end_ms = max(0, min(end_ms, duration_ms))
    if end_ms <= start_ms:
        return []

    seg = end_ms - start_ms
    n = choose_num_frames(seg, min_frames, max_frames, frames_per_sec)
    # 单帧取帧：目标精确对齐 start_ms（用源 PTS 身份），避免中点 t+0.5 跨到下一帧；多帧才按中点均匀分布
    if n == 1:
        targets_ms = [float(start_ms)]
    else:
        targets_ms = [start_ms + ((i + 0.5) / n) * seg for i in range(n)]

    container = av.open(str(path))
    try:
        video = next(s for s in container.streams if s.type == "video")
        out: list[dict[str, Any]] = []
        if n == 1:
            selected=[]
            for t_ms in targets_ms:
                # 单帧：优先取四舍五入后等于目标毫秒的那一帧（历史整数毫秒帧身份），避免跨帧
                frame, pts = _frame_rounding_to(container, video, int(round(t_ms)))
                if frame is None:
                    frame, pts = _frame_at_or_after(container, video, t_ms / 1000.0)
                if frame is not None:selected.append((frame,pts))
        else:
            selected=_frames_at_or_after(container,video,[t/1000 for t in targets_ms])
        for frame, pts in selected:
            if frame is None:
                continue
            img = _apply_rotation(frame.to_image(), rotation or 0)
            out.append({
                "t_ms": round(pts * 1000),
                "source_pts": frame.pts, "time_base": str(video.time_base),
                "pts_ms": round(pts * 1000, 3),  # 源 PTS 精确毫秒（帧身份），t_ms 为显示用整数
                "data_url": _pil_to_data_url(img, resize_edge, jpeg_quality),
            })
        return out
    finally:
        container.close()


def iter_source_frames_at_times(path: Path, times: list[int], *, duration_ms=None, rotation=None):
    """Yield (requested_ms, full_source_image, actual_pts_ms) in target order.

    Decode once, retain exact integer-rounding/fallback rules, and never retain
    a list of full-resolution images in memory.
    """
    if duration_ms is None or rotation is None:
        from .meta import read_video_metadata
        md=read_video_metadata(path)
        duration_ms=md['duration_ms'] if duration_ms is None else duration_ms
        rotation=md['rotation'] if rotation is None else rotation
    if any(isinstance(t,bool) or int(t)!=t for t in times):
        raise ValueError('Explicit frame targets must be integer milliseconds')
    targets=sorted(t for t in times if 0<=t<duration_ms)
    if not targets:return
    with av.open(str(path)) as container:
        video=next(s for s in container.streams if s.type=='video')
        container.seek(int((targets[0]-2000)/1000/float(video.time_base)),stream=video,backward=True)
        decoded=container.decode(video);frame=None;pts=None
        for target in targets:
            if frame is not None and target/1000-pts>2:
                container.seek(int((target-2000)/1000/float(video.time_base)),stream=video,backward=True)
                decoded=container.decode(video);frame=None;pts=None
            while frame is None or round(pts*1000)<target:
                try:frame=next(decoded)
                except StopIteration:return
                if frame.pts is None:
                    frame=None;continue
                pts=float(frame.pts*float(video.time_base))
            img=_apply_rotation(frame.to_image(),rotation or 0)
            img.info.update(source_pts=frame.pts,time_base=str(video.time_base))
            yield target,img,pts*1000


def sample_frames_at_times(path: Path, times: list[int], *, resize_edge=600,
                          jpeg_quality=80, duration_ms=None, rotation=None):
    """Batch explicit integer targets with unchanged model-input JPEG encoding."""
    return [{'t_ms':round(pts),'source_pts':img.info['source_pts'],'time_base':img.info['time_base'],
             'pts_ms':round(pts,3),'data_url':_pil_to_data_url(img,resize_edge,jpeg_quality)}
            for _,img,pts in iter_source_frames_at_times(path,times,duration_ms=duration_ms,rotation=rotation)]

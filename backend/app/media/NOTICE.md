# 第三方来源与许可证记录

## FireRed-OpenStoryline（底座）

- 主许可证：Apache-2.0（见原版 `LICENSE`）
- 本项目复用的媒体逻辑（经阅读后改造，未直接复制运行时代码依赖）：
  - 视频元数据读取思路 → `backend/app/media/meta.py`（参考 `nodes/core_nodes/load_media.py::_video_metadata_from_path`）
  - 帧采样思路 → `backend/app/media/sample.py`（参考 `mcp/sampling_handler.py::_sample_video_segment_to_data_urls` / `_choose_num_frames`）
  - 旋转读取思路 → `backend/app/media/meta.py::read_rotation`（参考 `utils/util.py::get_video_rotation`）
- 改造差异：
  1. 采样由 moviepy 改为 PyAV(`av`)，返回**实际帧 PTS**（准确时点），而非请求时点。
  2. ffprobe 由项目内 `static-ffmpeg` 提供，不依赖外部原版 `.venv/bin/ffprobe` 或系统 ffmpeg。
  3. 时间统一为毫秒整数，范围裁到有效时长内。

## 附带资产许可（阶段 4 上线前逐项核对）

- 音乐、字体、TransNetV2 权重、转场/配音等第三方资产：主 LICENSE（Apache-2.0）之外，**对外分发/商用前必须逐项核对**，无法确认的不得默认可商用。

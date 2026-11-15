# 媒体组件（独立开发副本，不依赖外部 FireRed 原版目录）
#
# 来源：FireRed-OpenStoryline（Apache-2.0）
# - meta.py 的元数据读取逻辑参考 `nodes/core_nodes/load_media.py::_video_metadata_from_path`
# - sample.py 的采样逻辑参考 `mcp/sampling_handler.py::_sample_video_segment_to_data_urls` 与 `_choose_num_frames`
# - 旋转读取逻辑参考 `utils/util.py::get_video_rotation`（原实现用 ffprobe，本项目用 static-ffmpeg 自带的 ffprobe）
#
# 与原版的差异（改造记录）：
# 1. 采样不再用 moviepy，改用 PyAV(av) 直接解码，返回「实际帧 PTS」的准确时点（原版返回请求时点，非实际帧时点）。
# 2. 元数据/旋转不依赖外部 ffprobe，改用项目内 static-ffmpeg 提供的 ffprobe 二进制。
# 3. 时间统一为毫秒（ms）整数，范围裁到有效时长内。

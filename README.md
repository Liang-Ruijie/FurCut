# FurCut

用于从宠物素材中挑选照片和连续视频的本地应用。包含素材上传、偏好设置、模型分析、人工选择和文件导出。

## 启动

使用 Python 3.12，在项目根目录执行：

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
cp backend/.env.example backend/.env
python run.py
```

浏览器打开 `http://127.0.0.1:8004`。首次启动创建空工作空间。上传自有素材，在管理设置中配置模型，在偏好页面设置选择要求后创建任务。模型密钥仅保存在本地环境变量或 `backend/.env` 中，真实分析需要配置服务和任务预算。

## 代码结构

- `backend/app`：接口、任务流程、媒体处理和存储。
- `backend/static`：前端页面与交互。
- `run.py`：本地启动入口。

运行时数据库、上传素材、预览和导出文件保存在 `data/`，不纳入版本管理。媒体来源说明见 `backend/app/media/NOTICE.md`。

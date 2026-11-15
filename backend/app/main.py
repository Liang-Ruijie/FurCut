"""FastAPI 入口。仅监听本机（127.0.0.1）。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from . import db
from .build import BUILD
from .api.routes import router
from .api.workspace import router as workspace_router
from .config import BACKEND_DIR, settings
from .security import SecurityError
from .access import WorkspaceAccess, validate_access_settings, router as access_router
from .upload_limit import UploadBodyLimit


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_access_settings()
    from .runtime_lock import exclusive_runtime
    with exclusive_runtime(settings.db_path):
        db.init_db(settings.db_path)
        from .services import analysis_job, export_job
        analysis_job.resume_running_jobs()
        export_job.resume_running_jobs()
        yield



app = FastAPI(title="拾光 · AI Video 猫舍选片选图", lifespan=lifespan)
app.add_middleware(UploadBodyLimit)
app.add_middleware(WorkspaceAccess)


@app.middleware('http')
async def revalidate_product_assets(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path == '/' or (not path.startswith('/api/') and path.endswith(('.html', '.js', '.css'))):
        # Module URLs are stable; validate them after an upgrade instead of
        # running a cached review UI against the newly started backend.
        response.headers['Cache-Control'] = 'no-cache'
    return response


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code,content={'error':{'code':'upload_limit' if exc.status_code==413 else 'http_error','message':str(exc.detail)}})


@app.exception_handler(SecurityError)
async def security_error_handler(request: Request, exc: SecurityError):
    return JSONResponse(status_code=400, content={"error": {"code": exc.code, "message": exc.message}})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"error": {"code": "invalid_request", "message": "请求参数不合法"}})


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"error": {"code": "internal_error", "message": "服务内部错误"}})


@app.get("/api/health")
def health():
    import hashlib
    workspace_id=hashlib.sha256(str(settings.db_path.resolve()).encode()).hexdigest()[:24]
    return {"status": "ok", "stage": "product-implementation", "build":BUILD,"workspace_id":workspace_id}


app.include_router(router)
app.include_router(workspace_router)
app.include_router(access_router)
app.mount("/", StaticFiles(directory=str(BACKEND_DIR / "static"), html=True), name="static")


def run() -> None:
    import uvicorn
    uvicorn.run(app, host=settings.app_host, port=settings.app_port, log_level="info")


if __name__ == "__main__":
    run()

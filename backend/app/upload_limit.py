"""Reject oversized multipart bodies before framework spooling can exhaust temporary storage."""
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from .services.assets import MAX_UPLOAD_BYTES

MAX_REQUEST_BYTES=MAX_UPLOAD_BYTES+1024*1024  # bounded multipart overhead

class UploadBodyLimit:
    def __init__(self,app):self.app=app

    async def __call__(self,scope,receive,send):
        path=scope.get('path')
        if scope['type']!='http' or scope.get('method')!='POST' or path not in {'/api/v1/assets/upload','/api/auth/login'}:
            return await self.app(scope,receive,send)
        limit=4096 if path=='/api/auth/login' else MAX_REQUEST_BYTES
        message='登录请求过大' if path=='/api/auth/login' else '上传请求超过单文件2GB限制，请拆分或选择较小素材。'
        headers=dict(scope.get('headers',[]))
        try:declared=int(headers.get(b'content-length',b'0'))
        except ValueError:declared=limit+1
        if declared>limit:
            return await JSONResponse({'error':{'code':'body_too_large','message':message}},413)(scope,receive,send)
        total=0
        async def limited():
            nonlocal total
            event=await receive()
            if event['type']=='http.request':
                total+=len(event.get('body',b''))
                if total>limit:raise HTTPException(413,message)
            return event
        return await self.app(scope,limited,send)

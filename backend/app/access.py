"""A single private workspace with operator/admin access. Secrets never go to views."""
from __future__ import annotations
import hashlib,hmac,ipaddress,secrets,time
from urllib.parse import urlparse
from fastapi import APIRouter,Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from .config import settings

router=APIRouter(prefix='/api/auth')
COOKIE='aivideo_session'
TTL=12*3600

def validate_access_settings():
    if settings.app_access_mode not in {'local','shared'}:raise RuntimeError('APP_ACCESS_MODE must be local or shared')
    if settings.app_access_mode=='shared':
        if settings.app_workspace_budget_limit_minor is None or settings.app_workspace_budget_limit_minor<=0:
            raise RuntimeError('Shared mode requires a positive workspace budget ceiling in CNY micro-yuan')
        if min(len(settings.app_admin_token),len(settings.app_user_token))<32 or settings.app_admin_token==settings.app_user_token:
            raise RuntimeError('Shared mode requires different operator/admin secrets of at least 32 characters')
        origin=urlparse(settings.app_public_origin)
        if origin.scheme!='https' or not origin.netloc or origin.path not in ('','/'):
            raise RuntimeError('Shared mode requires APP_PUBLIC_ORIGIN=https://your-host')
    elif settings.app_host not in {'127.0.0.1','localhost','::1'}:
        raise RuntimeError('Non-loopback hosting requires shared access mode')

def _match(value,secret):return bool(secret) and hmac.compare_digest(value.encode(),secret.encode())
def _role(token):
    if _match(token,settings.app_admin_token):return 'admin'
    if _match(token,settings.app_user_token):return 'operator'
    return None

def _session(role):
    payload=f'{role}.{int(time.time())+TTL}.{secrets.token_hex(12)}'
    return payload+'.'+hmac.new(settings.app_admin_token.encode(),payload.encode(),hashlib.sha256).hexdigest()

def _read_session(raw):
    try:
        payload,sig=raw.rsplit('.',1);role,expiry,nonce=payload.split('.')
        expected=hmac.new(settings.app_admin_token.encode(),payload.encode(),hashlib.sha256).hexdigest()
        if role in {'admin','operator'} and int(expiry)>time.time() and hmac.compare_digest(sig,expected):return role
    except (ValueError,TypeError):pass
    return None

def _admin_path(path,method):
    return path.startswith('/api/v1/workspace/admin/') or path in {'/advanced.html','/review.html','/delivery.html','/docs','/redoc','/openapi.json'} or path.startswith(('/api/v1/configs','/api/v1/costs/')) or '/requests' in path or path=='/api/v1/assets/files' or path=='/api/v1/assets/register'

class WorkspaceAccess(BaseHTTPMiddleware):
    async def dispatch(self,request,call_next):
        path=request.url.path;shared=settings.app_access_mode=='shared'
        if not shared:
            host=request.client.host if request.client else ''
            try:local=ipaddress.ip_address(host).is_loopback
            except ValueError:local=host=='testclient'
            if not local or request.url.hostname not in {'localhost','127.0.0.1','::1','testserver'}:
                return JSONResponse({'error':{'code':'local_only','message':'本地模式仅允许本机访问；对外运行必须配置访问保护。'}},403)
            request.state.role='admin'
        else:
            auth=request.headers.get('authorization','');bearer=auth[7:] if auth.lower().startswith('bearer ') else ''
            request.state.role=_role(bearer) if bearer else _read_session(request.cookies.get(COOKIE,''))
        if request.method not in {'GET','HEAD','OPTIONS'}:
            origin=request.headers.get('origin')
            expected=(settings.app_public_origin.rstrip('/') if shared else str(request.base_url).rstrip('/'))
            if origin and origin!=expected:
                return JSONResponse({'error':{'code':'origin_rejected','message':'来源校验失败，请从本工作空间页面重新操作。'}},403)
        public=path=='/api/auth/login' or path=='/api/auth/me' or path=='/api/health' or not path.startswith('/api/') and not _admin_path(path,request.method)
        if shared and not request.state.role and not public:
            return JSONResponse({'error':{'code':'authentication_required','message':'请先登录工作空间'}},401)
        if shared and request.state.role!='admin' and _admin_path(path,request.method):
            return JSONResponse({'error':{'code':'admin_required','message':'此操作需要管理员权限'}},403)
        response=await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='same-origin'
        response.headers['X-Frame-Options']='DENY'
        if path.startswith('/api/') and 'Cache-Control' not in response.headers:response.headers['Cache-Control']='no-store'
        return response

@router.get('/me')
def me(request:Request):return {'mode':settings.app_access_mode,'role':request.state.role,'workspace':'private'}

@router.post('/login')
async def login(request:Request):
    # Credential value is excluded from errors and response. Reverse proxy must rate-limit login.
    try:
        data=await request.json();value=data.get('token','')
        role=_role(value) if isinstance(value,str) and len(value)<=512 else None
    except (ValueError,AttributeError):role=None
    if not role:return JSONResponse({'error':{'code':'invalid_credentials','message':'访问口令不正确'}},401)
    response=JSONResponse({'role':role});response.set_cookie(COOKIE,_session(role),httponly=True,secure=settings.app_cookie_secure,samesite='strict',max_age=TTL,path='/');return response

@router.post('/logout')
def logout():
    response=JSONResponse({'ok':True});response.delete_cookie(COOKIE,path='/');return response

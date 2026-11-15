"""Immutable output receipts. Downloads validate the saved bytes, not a current UI claim."""
import json
from datetime import datetime,timezone
from pathlib import Path
from .. import security
from ..config import settings
from . import assets


def record(conn,task_id,path:Path,items):
    for item in items:assets.check_export_asset(conn,task_id,item['asset_id'])
    digest=security.sha256_file(path);size=path.stat().st_size
    prior=conn.execute('SELECT sha256,size_bytes FROM artifact_receipts WHERE file_name=?',(path.name,)).fetchone()
    if prior and (prior['sha256']!=digest or prior['size_bytes']!=size):raise security.SecurityError('artifact_conflict','拒绝覆盖已有导出凭据')
    conn.execute('INSERT OR IGNORE INTO artifact_receipts(file_name,task_id,sha256,size_bytes,snapshot_json,created_at) VALUES(?,?,?,?,?,?)',
                 (path.name,task_id,digest,size,json.dumps([{k:v for k,v in i.items() if k!='frames'} for i in items],ensure_ascii=False),datetime.now(timezone.utc).isoformat()))


def verify(conn,name):
    if Path(name).name!=name:raise security.SecurityError('invalid_path','非法下载路径')
    path=settings.output_dir/name
    if not path.is_file():raise security.SecurityError('export_not_found','导出文件缺失，请重新导出以恢复')
    receipt=conn.execute('SELECT * FROM artifact_receipts WHERE file_name=?',(name,)).fetchone()
    if not receipt:
        # Legacy batch outputs have durable hashes in their original metadata.
        for row in conn.execute('SELECT file_name,meta_json FROM export_items WHERE status=?',('done',)):
            meta=json.loads(row['meta_json'] or '{}')
            if row['file_name']==name and meta.get('output_sha256'):
                receipt={'sha256':meta['output_sha256'],'size_bytes':meta.get('output_size',path.stat().st_size)};break
            variant=next((v for v in meta.get('variant_files',[]) if v['file']==name),None)
            if variant:receipt={'sha256':variant['sha256'],'size_bytes':variant['size']};break
    if not receipt:raise security.SecurityError('unverified_export','这个历史单项文件缺少可验证导出凭据；请重新导出，原文件会保留。已有封闭包仍可独立使用。')
    if path.stat().st_size!=receipt['size_bytes'] or security.sha256_file(path)!=receipt['sha256']:
        raise security.SecurityError('artifact_corrupt','导出文件损坏，请重试导出生成恢复版本，旧文件不会覆盖')
    return path

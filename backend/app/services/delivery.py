"""Immutable, closed review packages. Stage, validate everything, then atomic publish."""
from __future__ import annotations
import html
import json
import os
import shutil
import tempfile
from datetime import datetime,timezone
from urllib.parse import quote
from pathlib import Path
from . import results, assets, batch_identity
from .. import security


def create_package(conn, task_id: str, root: Path, version: str) -> Path:
    if not version or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_. ' for c in version):
        raise security.SecurityError('invalid_version','交付版本只能包含字母、数字及 -_. 空格')
    items = results.reviewed_items(conn,task_id)
    if not items:
        raise security.SecurityError('no_items','尚无明确保留项，不能生成正式交付包')
    # Snapshot canonical input and decisions. Repeated identical deliveries reuse verified artifacts.
    from .tasks import display_title,get_task
    title=display_title(get_task(conn,task_id))
    identity = batch_identity.digest({'task_id':task_id,'version':version,'task_title':title,'items':items})
    root.mkdir(parents=True,exist_ok=True)
    target = root / f'{task_id}-{version}-{identity[:16]}'
    for item in items:
        assets.check_export_asset(conn,task_id,item['asset_id'])
    if target.exists():
        try:
            verify_package(target)
            return target
        except (OSError,ValueError,security.SecurityError):
            # Keep the corrupted historical package for diagnosis. Publish a repaired
            # immutable sibling, never clear a directory currently being delivered.
            for repair in sorted(root.glob(target.name+'-repair-*')):
                try:
                    verify_package(repair)
                    return repair
                except (OSError,ValueError,security.SecurityError):
                    pass
            target = root/(target.name+'-repair-'+assets.new_id('pkg'))
    stage = Path(tempfile.mkdtemp(prefix='.delivery-',dir=root))
    try:
        from ..build import BUILD
        manifest = {'build':BUILD,'task_id':task_id,'task_title':title,'created_at':datetime.now(timezone.utc).isoformat(),'code_version':version,'review_snapshot':identity,'items':[]}
        for item in items:
            assets.check_export_asset(conn,task_id,item['asset_id'])
            if item['kind'] == 'photo':
                out = results.export_photo(conn,item['asset_id'],item['t_ms'],stage,source_pts=item.get('source_pts'),time_base=item.get('time_base'))
            else:
                from .video_variants import export_reviewed_video
                out,variants = export_reviewed_video(conn,task_id,item,stage)
            row = conn.execute('SELECT fingerprint,source_path FROM assets WHERE asset_id=?',(item['asset_id'],)).fetchone()
            entry = {k:v for k,v in item.items() if k != 'frames'}
            entry.update(source_name=Path(row['source_path']).name,file=out.name,sha256=security.sha256_file(out),size=out.stat().st_size,source_fingerprint=row['fingerprint'])
            if item['kind']=='photo':
                entry.update(results.photo_identity(out))
            else:
                entry.update(results.video_identity(out))
            manifest['items'].append(entry)
            if item['kind']=='video':
                entry['variant']='continuous'
                entry['variant_files']=variants
        (stage/'index.html').write_text(_render_package(manifest),encoding='utf-8')
        manifest['index_sha256'] = security.sha256_file(stage/'index.html')
        (stage/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
        verify_package(stage)
        # Sources and saved decisions must still match before the publication boundary.
        for item in items:
            assets.check_export_asset(conn,task_id,item['asset_id'])
        current_title=display_title(get_task(conn,task_id))
        if batch_identity.digest({'task_id':task_id,'version':version,'task_title':current_title,'items':results.reviewed_items(conn,task_id)}) != identity:
            raise security.SecurityError('review_changed','审核决定已变化，请重新生成')
        os.rename(stage,target)  # immutable target; never empty or overwrite an earlier delivery
        return target
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def _verify_package(path: Path,decode: bool = True) -> dict:
    doc = json.loads((path/'manifest.json').read_text())
    expected = {'manifest.json','index.html'}
    if security.sha256_file(path/'index.html') != doc['index_sha256']:
        raise security.SecurityError('package_corrupt','交付索引损坏')
    for it in doc['items']:
        name = it['file']
        if Path(name).name != name:
            raise security.SecurityError('package_corrupt','清单路径非法')
        expected.add(name)
        p = path/name
        if decode:
            results.validate_media(p,it['kind'])
        if security.sha256_file(p) != it['sha256'] or p.stat().st_size != it['size']:
            raise security.SecurityError('package_corrupt','交付媒体与清单不符')
        for variant in it.get('variant_files',[]):
            filename=variant['file']
            if Path(filename).name!=filename:raise security.SecurityError('package_corrupt','派生文件路径非法')
            expected.add(filename)
            derived=path/filename
            if decode:
                results.validate_media(derived,'video')
            if security.sha256_file(derived)!=variant['sha256'] or derived.stat().st_size!=variant['size']:
                raise security.SecurityError('package_corrupt','派生版本损坏')
    if {p.name for p in path.iterdir()} != expected:
        raise security.SecurityError('package_not_closed','交付包包含清单外文件或文件缺失')
    return doc


def verify_package(path: Path,decode: bool=True) -> dict:
    try:
        return _verify_package(path,decode=decode)
    except security.SecurityError:
        raise
    except (OSError,ValueError,KeyError,TypeError) as exc:
        raise security.SecurityError('package_corrupt','交付包清单或媒体损坏') from exc


def _render_package(doc: dict) -> str:
    e=lambda value:html.escape(str(value),quote=True)
    url=lambda name:e(quote(name,safe=''))
    photos=[p for p in doc['items'] if p['kind']=='photo']
    videos=[p for p in doc['items'] if p['kind']=='video']
    cards=[]
    for index,item in enumerate(doc['items'],1):
        name='照片' if item['kind']=='photo' else '视频片段'
        timing=f"{item.get('t_ms',0)/1000:.2f}s" if item['kind']=='photo' else f"{item['start_ms']/1000:.2f} — {item['end_ms']/1000:.2f}s"
        media=(f'<img loading="lazy" src="{url(item["file"])}" alt="已保留照片 {index}">' if item['kind']=='photo'
               else f'<h3>连续原片版</h3><video controls playsinline preload="metadata" src="{url(item["file"])}"></video>')
        links=f'<a class="button" href="{url(item["file"])}" download>下载{name}</a>'
        for variant in item.get('variant_files',[]):
            media+=f'<h3>最佳帧开头版</h3><video controls playsinline preload="metadata" src="{url(variant["file"])}"></video>'
            links+=f'<a class="button secondary" href="{url(variant["file"])}" download>下载最佳帧开头版</a>'
        trace=e(json.dumps({k:item.get(k) for k in ('candidate_id','source_fingerprint','source_pts','time_base','review_evidence')},ensure_ascii=False,indent=2))
        cards.append(f'<article><header><h2>{name} {index:02d}</h2><p>{e(item.get("source_name","原片"))} · {timing}</p></header>{media}<div class="actions">{links}</div><details><summary>来源与审核记录</summary><pre>{trace}</pre></details></article>')
    style="""*{box-sizing:border-box}body{margin:0;background:#f5f5ef;color:#213632;font:16px/1.65 -apple-system,'PingFang SC',sans-serif}main{max-width:1080px;margin:auto;padding:36px 20px}h1{font-size:32px;margin:12px 0}h2{font-size:22px;margin:0}h3{font-size:15px;margin:22px 0 10px}p{color:#66766c;overflow-wrap:anywhere}.brand{font-size:12px;letter-spacing:3px;color:#226957}.intro{padding-bottom:24px}.stats{display:flex;gap:12px;flex-wrap:wrap}.stats span{background:#e6eddf;padding:7px 15px;border-radius:30px}article{background:white;border:1px solid #dde5d9;border-radius:18px;padding:24px;margin-bottom:24px}article header{margin-bottom:18px}article p{margin:5px 0}img,video{display:block;width:100%;max-height:72vh;object-fit:contain;background:#e9eee5;border-radius:12px}.actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:20px}.button{display:inline-block;background:#226957;color:white;text-decoration:none;border-radius:9px;padding:10px 18px;min-height:44px}.secondary{background:#e5eee5;color:#226957}details{margin-top:18px;color:#6b776e;font-size:13px}summary{cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:11px;background:#f4f7f1;padding:14px;border-radius:10px}footer{font-size:12px;color:#6c786f;margin:32px 0}.manifest{color:#226957}@media(max-width:600px){main{padding:24px 14px}article{padding:16px}h1{font-size:26px}.actions .button{flex:1;text-align:center}}"""
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            +'<title>'+e(doc.get('task_title','精选成果'))+' · AI Video</title><style>'+style+'</style></head><body><main>'
            +'<section class="intro"><div class="brand">拾光 · AI VIDEO</div><h1>'+e(doc.get('task_title','精选成果'))+'</h1>'
            +'<p>根据已保存的保留项整理。照片、连续原片版与最佳帧开头版可独立查看和下载。</p>'
            +f'<div class="stats"><span>{len(photos)}张照片</span><span>{len(videos)}段视频</span></div></section>'
            +''.join(cards)+'<footer>此包可独立离线使用，不需要连接开发者电脑或运行服务。连续版保持原片顺序；开头版本是同一片段的派生成果。'
            +'<p><a class="manifest" href="manifest.json" download>下载完整来源清单</a></p></footer></main></body></html>')

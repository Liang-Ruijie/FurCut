"""Optional best-frame intro plus untouched continuous-cut alternative."""
from __future__ import annotations
import json
import subprocess
from pathlib import Path
from . import results, preferences
from .. import security

RENDER_VERSION = 2

def export_options(conn,task_id):
    row=conn.execute('SELECT preference_ref_json FROM tasks WHERE task_id=?',(task_id,)).fetchone()
    ref=json.loads(row[0]) if row and row[0] else None
    if not ref:
        return {'best_frame_enabled':False,'best_frame_hold_ms':300}
    opts=preferences.get_preference(conn,ref['preference_id'],ref['version'])['export_defaults']
    hold=opts.get('best_frame_hold_ms',300)
    if isinstance(hold,bool) or not isinstance(hold,int) or not 0<=hold<=5000:
        raise security.SecurityError('invalid_export_options','最佳帧停留须为 0–5000 整数毫秒')
    return {**opts,'best_frame_hold_ms':hold}


def export_with_hold(conn,asset_id,start_ms,end_ms,best_t_ms,hold_ms,out_dir:Path,*,continuous_path:Path|None=None):
    if not start_ms<=best_t_ms<=end_ms or not 0<hold_ms<=5000:
        raise security.SecurityError('invalid_boundary','最佳帧或停留时长不合法')
    import tempfile
    from ..media.meta import read_video_metadata
    with tempfile.TemporaryDirectory(prefix='.hold-',dir=out_dir) as d:
        work=Path(d)
        clip=continuous_path or results.export_video_clip(conn,asset_id,start_ms,end_ms,work)
        photo=results.export_photo(conn,asset_id,best_t_ms,work)
        md=read_video_metadata(clip)
        out=results._unique_path(out_dir,clip.stem+f'-best{best_t_ms}-hold{hold_ms}.mp4')
        temp=work/'variant.mp4'
        seconds=hold_ms/1000
        # Match media dimensions; retain original audio after a silent intro.
        filt=f'[0:v]scale={md["width"]}:{md["height"]},setsar=1,fps=30,trim=duration={seconds},setpts=PTS-STARTPTS[a];[1:v]setsar=1,fps=30,setpts=PTS-STARTPTS[b];[a][b]concat=n=2:v=1:a=0[v]'
        cmd=['ffmpeg','-y','-loop','1','-i',str(photo),'-i',str(clip),'-filter_complex',filt,'-map','[v]']
        if md['has_audio']:
            cmd+=['-map','1:a:0','-af',f'adelay={hold_ms}:all=1','-c:a','aac']
        else:cmd+=['-an']
        # The first input is a still image (ffmpeg defaults to 25fps). Pin the
        # encoded output to the filter's 30fps so moving footage is not dropped.
        cmd+=['-r','30','-c:v','libx264','-preset','fast','-crf','18','-pix_fmt','yuv420p',str(temp)]
        subprocess.run(cmd,check=True,capture_output=True)
        results.validate_media(temp,'video')
        if abs(read_video_metadata(temp)['fps']-30)>0.001:
            raise security.SecurityError('invalid_variant','最佳帧版帧率校验失败，未发布文件')
        temp.replace(out)
        return out


def best_time(item):
    from .analysis import _photo_quality_score
    frames=[f for f in item.get('frames',[]) if item['start_ms']<=f['t_ms']<=item['end_ms']]
    return max(frames,key=_photo_quality_score)['t_ms'] if frames else item['start_ms']


def export_reviewed_video(conn,task_id,item,out_dir):
    continuous=results.export_video_clip(conn,item['asset_id'],item['start_ms'],item['end_ms'],out_dir)
    variants=[]
    opts=export_options(conn,task_id)
    if opts.get('best_frame_enabled') and opts['best_frame_hold_ms']>0 and item.get('frames'):
        best=best_time(item)
        intro=export_with_hold(conn,item['asset_id'],item['start_ms'],item['end_ms'],best,opts['best_frame_hold_ms'],out_dir,continuous_path=continuous)
        variants.append({'file':intro.name,'kind':'video','variant':'best_frame_intro','best_t_ms':best,
                         'hold_ms':opts['best_frame_hold_ms'],'render_version':RENDER_VERSION,'fps':30,
                         'sha256':security.sha256_file(intro),'size':intro.stat().st_size})
    return continuous,variants

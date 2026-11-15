"""Same-source full frame plus optional overlapping detail views; never reference photos."""
from pathlib import Path
from ..media.sample import extract_frame_at, _pil_to_data_url

DETAIL_INSTRUCTIONS = (
    "下列图像均来自同一源帧：第一张为全图，其余为重叠分区放大，仅辅助辨认眼睑、脸和爪子。"
    "只以全图判断构图、遮挡和脸部完整性，不得把细节裁片边缘当作原图裁切。"
    "背景及前景景深虚化与主体失焦必须区分；仅背景虚化、前景遮住身体非关键下半部、"
    "爪子自然藏在身体后面或不入镜，不直接判硬伤。"
    "柔软毛发或略暗光线不等于脸和眼睛模糊。若无法确定是硬伤，请明确观察不确定处，不编造爪子勾住或遮脸。"
)


def messages(path:Path,t_ms:int,prompt:str,*,detail:bool=False):
    img,pts=extract_frame_at(path,t_ms)
    if img is None:raise ValueError('源帧不存在')
    images=[img]
    if detail:
        w,h=img.size
        # Overlap preserves complete anatomy at tile joins; cover the full source.
        if h>=w:
            for start in (0,1/6,1/3):
                images.append(img.crop((0,round(h*start),w,min(h,round(h*(start+2/3))))))
        else:
            for start in (0,1/6,1/3):
                images.append(img.crop((round(w*start),0,min(w,round(w*(start+2/3))),h)))
    blocks=[{'type':'image_url','image_url':{'url':_pil_to_data_url(i,1024,80)}} for i in images]
    return [{'role':'system','content':prompt+ ('\n'+DETAIL_INSTRUCTIONS if detail else '')},
            {'role':'user','content':blocks}],pts

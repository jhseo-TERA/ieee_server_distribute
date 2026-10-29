"""Bounded local OCR adapter. Never downloads dependencies or calls a cloud API."""
import json
import math
import os
from pathlib import Path
import shutil
import subprocess


def recognize_page(page, root, digest, index):
    root=Path(root)
    directory=root/'outputs/serdes_ocr'
    model=directory/'eng.traineddata'
    node=os.getenv('SERDES_OCR_NODE') or shutil.which('node')
    if not model.is_file() or not node:
        return [], 'ocr_unavailable'
    cache=directory/f'{digest}-{index+1}-v1.json'
    bounds=page.get_bbox(); width,height=bounds[2]-bounds[0],bounds[3]-bounds[1]
    if cache.is_file():
        try:
            data=json.loads(cache.read_text(encoding='utf-8'))
            if valid_lines(data.get('lines')):
                return data['lines'], 'ocr_cached'
        except (OSError, ValueError, AttributeError):
            pass  # A damaged cache must not prevent native figure extraction.
    image_path=directory/f'{digest}-{index+1}-v1.png'
    bitmap=page.render(scale=2.5, rotation=(-page.get_rotation())%360, draw_annots=False)
    try:
        bitmap.to_pil().save(image_path)
        pixel_width,pixel_height=bitmap.width,bitmap.height
    finally:bitmap.close()
    env=os.environ.copy()
    # Bundled node installations keep node_modules adjacent to bin, not the repo.
    modules=Path(node).resolve().parent.parent/'node_modules'
    if modules.is_dir():env['NODE_PATH']=str(modules)
    try:
        result=subprocess.run([node,str(root/'scripts/serdes_ocr.cjs'),str(image_path),str(directory)],
                              capture_output=True,text=True,encoding='utf-8',timeout=45,check=True,env=env)
        recognized=json.loads(result.stdout)
        if not valid_lines(recognized.get('lines')):
            return [], 'ocr_failed'
    except (OSError,subprocess.SubprocessError,ValueError):
        return [], 'ocr_failed'
    lines=[]
    for line in recognized['lines']:
        l,t,r,b=line['box']
        lines.append({'text':line['text'],'box':[l/pixel_width*width,height-b/pixel_height*height,
                                               r/pixel_width*width,height-t/pixel_height*height]})
    temporary=cache.with_suffix('.tmp')
    temporary.write_text(json.dumps({'lines':lines},ensure_ascii=False),encoding='utf-8');temporary.replace(cache)
    return lines, 'ocr_recognized'


def valid_lines(lines):
    return (isinstance(lines,list) and len(lines)<=20000 and all(
        isinstance(line,dict) and isinstance(line.get('text'),str) and
        isinstance(line.get('box'),list) and len(line['box'])==4 and
        all(type(v) in (int,float) and math.isfinite(v) for v in line['box']) and
        line['box'][2]>line['box'][0] and line['box'][3]>line['box'][1]
        for line in lines))

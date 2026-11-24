"""Index visible PDF placements and export selected figures from the original document."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import re

import pymupdf as pdf


def digest(path):
    with path.open('rb') as src:
        h = sha256()
        for chunk in iter(lambda: src.read(1 << 20), b''):
            h.update(chunk)
        return h.hexdigest()


def box(page, coordinates):
    r = (pdf.Rect(coordinates) * page.rotation_matrix) & page.rect
    return [] if r.is_empty else [round(v, 2) for v in r]


def page_detail(page):
    lines = []
    blocks = page.get_text('dict', flags=pdf.TEXTFLAGS_DICT & ~pdf.TEXT_PRESERVE_IMAGES)['blocks']
    for b in blocks:
        for line in b.get('lines', []):
            text = ''.join(s['text'] for s in line['spans']).strip()
            rect = box(page, line['bbox'])
            if text and rect:
                lines.append(dict(id=f'T{len(lines)+1}', text=text, box=rect))
    figures = []
    for img in page.get_image_info(hashes=True, xrefs=True):
        rect = box(page, img['bbox'])
        if not rect:
            continue
        figures.append(dict(id=f'F{len(figures)+1}', box=rect, xref=img['xref'],
                            pixels=[img['width'], img['height']], transform=list(img['transform'])))
    for figure in figures:
        rect = pdf.Rect(figure['box'])
        neighbors = []
        for line in lines:
            label = pdf.Rect(line['box'])
            dx = max(rect.x0-label.x1, label.x0-rect.x1, 0)
            dy = max(rect.y0-label.y1, label.y0-rect.y1, 0)
            if dx and dy or dx > rect.width*.8 or dy > rect.height*.35:
                continue
            offset = abs((label.y0+label.y1-rect.y0-rect.y1)/2) if dx else abs((label.x0+label.x1-rect.x0-rect.x1)/2)
            neighbors.append((dx+dy+offset*.12,line['id']))
        figure['nearby_text'] = [id_ for _,id_ in sorted(neighbors)[:10]]
    return dict(page=page.number+1, size=[page.rect.width,page.rect.height], figures=figures, text=lines)


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, separators=(',', ':')),encoding='utf-8')


def index_document(source, directory):
    fingerprint = digest(source)
    directory = directory / fingerprint[:16]
    directory.mkdir(parents=True,exist_ok=True)
    index_path = directory/'index.json'
    summary = []
    texts = []
    inherited = []
    saved = {}
    with pdf.open(source) as doc:
        for page in doc:
            detail = page_detail(page)
            lines = [r['text'] for r in detail['text']]
            texts.append('\n'.join(lines))
            for figure in detail['figures']:
                xref = figure['xref']
                if not xref:
                    continue
                if xref not in saved:
                    img = doc.extract_image(xref)
                    if not img:
                        continue
                    target = directory/f"object-{xref}.{img['ext']}"
                    if not target.exists():
                        target.write_bytes(img['image'])
                    saved[xref] = str(target)
                inherited.append(dict(path=saved[xref], alt=f"pdf page {page.number+1} {figure['id']}",
                                      page=page.number+1, figure=figure['id']))
            name = f'p{page.number+1:03}.json'
            write_json(directory/name,detail)
            summary.append(dict(page=page.number+1,heading=' '.join(lines[:2])[:110],
                                figures=len(detail['figures']), detail=name))
    write_json(index_path,dict(source=str(source.resolve()), sha256=fingerprint, pages=summary))
    return dict(path=str(source),type='pdf',text='\n'.join(texts),pages=len(summary),tables=[],
                inherited_images=inherited,page_visuals=[],figure_index=str(index_path))


def pixmap(doc, xref):
    image = pdf.Pixmap(doc,xref)
    mask = doc.xref_get_key(xref,'SMask')
    if mask[0]=='xref':
        alpha = pdf.Pixmap(doc,int(mask[1].split()[0]))
        if (image.width,image.height)!=(alpha.width,alpha.height):
            return None
        if image.alpha:
            image=pdf.Pixmap(image,0)
        image=pdf.Pixmap(image,alpha)
    if image.colorspace and image.colorspace.n>3:
        image=pdf.Pixmap(pdf.csRGB,image)
    return image


def grid_rect(page, grid):
    match = re.fullmatch(r'([A-F])([1-8])(?:-([A-F])([1-8]))?', grid.upper())
    if not match:
        raise ValueError('Use grid cells A1 through F8, for example B2-D6')
    a,b,c,d=match.groups()
    x0,x1=sorted((ord(a)-65, ord(c or a)-65))
    y0,y1=sorted((int(b)-1,int(d or b)-1))
    return pdf.Rect(x0*page.rect.width/6,y0*page.rect.height/8,
                    (x1+1)*page.rect.width/6,(y1+1)*page.rect.height/8)


def selection(index_path, number, operation, ids, grid, output):
    idx=json.loads(index_path.read_text())
    source=Path(idx['source'])
    if digest(source)!=idx['sha256']:
        raise ValueError('The source PDF has changed. Parse it again before selecting a figure.')
    if number < 1 or number > len(idx['pages']):
        raise ValueError('Page number outside the indexed PDF')
    detail=json.loads((index_path.parent/idx['pages'][number-1]['detail']).read_text())
    items={r['id']:r for r in detail['figures']+detail['text']}
    unknown=set(ids)-items.keys()
    if unknown:
        raise ValueError(f'Unknown items on page {number}: {sorted(unknown)}')
    with pdf.open(source) as doc:
        page=doc[number-1]
        if operation=='sheet':
            from PIL import Image,ImageDraw
            ratio=min(1.8,1250/max(page.rect.width,page.rect.height))
            pm=page.get_pixmap(matrix=pdf.Matrix(ratio,ratio),alpha=False,colorspace=pdf.csRGB)
            im=Image.frombytes('RGB',(pm.width,pm.height),pm.samples)
            canvas=ImageDraw.Draw(im)
            for i in range(1,6):canvas.line((im.width*i/6,0,im.width*i/6,im.height),fill='#87979c',width=1)
            for i in range(1,8):canvas.line((0,im.height*i/8,im.width,im.height*i/8),fill='#87979c',width=1)
            for i in range(6):canvas.text((im.width*(i+.5)/6,4),chr(65+i),fill='#17242b',stroke_width=2,stroke_fill='white')
            for i in range(8):canvas.text((4,im.height*(i+.5)/8),str(i+1),fill='#17242b',stroke_width=2,stroke_fill='white')
            for f in detail['figures']:
                coords=tuple(v*ratio for v in f['box'])
                if min(coords[2]-coords[0],coords[3]-coords[1])<10:continue
                canvas.rectangle(coords,outline='#cc3d19',width=2)
                canvas.text((coords[0]+2,coords[1]+2),f['id'],fill='#5c1c08',stroke_width=2,stroke_fill='white')
            output.parent.mkdir(parents=True,exist_ok=True)
            im.save(output,format='JPEG',quality=88)
            return
        if operation=='image' and (len(ids)!=1 or not ids[0].startswith('F') or grid):
            raise ValueError('image needs one figure ID. Use region for captions, grid cells or multiple items.')
        bounds=None
        for id_ in ids:
            r=pdf.Rect(items[id_]['box'])
            bounds = r if bounds is None else bounds|r
        if grid:
            r=grid_rect(page,grid);bounds=r if bounds is None else bounds|r
        if bounds is None:
            bounds=page.rect
        elif operation=='region' and ids and not grid:
            margin=max(2,min(6,min(bounds.width,bounds.height)*.035))
            bounds=(bounds+(-margin,-margin,margin,margin)) & page.rect
        pm=None
        if operation=='image':
            fig=items[ids[0]]
            a,b,c,d,e,f=fig['transform']
            full=pdf.Rect(0,0,1,1)*pdf.Matrix(a,b,c,d,e,f)
            if fig['xref'] and page.rotation==0 and abs(b)+abs(c)<0.01 and a>0 and d>0 and page.rect.contains(full):
                try:pm=pixmap(doc,fig['xref'])
                except (ValueError,RuntimeError):pass
        if pm is None:
            scale=min(3,3000/max(bounds.width,bounds.height))
            pm=page.get_pixmap(matrix=pdf.Matrix(scale,scale),clip=bounds,alpha=False,colorspace=pdf.csRGB)
        if pm.width==0 or pm.height==0:raise ValueError('Selection contains no visible pixels')
        output.parent.mkdir(parents=True,exist_ok=True)
        pm.save(str(output),output='png')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('operation',choices=('sheet','image','region'))
    p.add_argument('--index',type=Path,required=True)
    p.add_argument('--page',type=int,required=True)
    p.add_argument('--ids',nargs='+',default=[])
    p.add_argument('--grid',help='Inclusive cells on the numbered sheet, for example B2-D6')
    p.add_argument('--out',type=Path,required=True)
    a=p.parse_args()
    selection(a.index,a.page,a.operation,a.ids,a.grid,a.out)
    print(a.out.resolve())

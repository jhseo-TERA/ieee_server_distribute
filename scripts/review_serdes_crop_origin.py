"""Render an isolated crop-coordinate comparison; never mutate FigureStore."""
import json
import sys
from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT / '.venv/Lib/site-packages'))
import pypdfium2 as pdfium


def main():
    out = ROOT / 'outputs/serdes_figure_audit'
    inventory = json.loads((out / 'inventory.json').read_text(encoding='utf-8'))
    ids = {'11214097', '10719519', '6649142', '5433823', '6571889'}
    for paper in inventory['papers']:
        article = str(paper['article_number'])
        if article not in ids:
            continue
        figure = paper['figure']
        with pdfium.PdfDocument(str(ROOT / paper['pdf_local_path'])) as doc:
            page = doc[figure['page'] - 1]
            l, b, r, t = figure['bbox']
            width, height = page.get_size()
            x0, y0, x1, y1 = page.get_bbox()
            crops = [(l, b, width-r, height-t),
                     (l-x0, b-y0, x1-r, y1-t)]
            canvas = Image.new('RGB', (1500, 900), 'white')
            draw = ImageDraw.Draw(canvas)
            for index, crop in enumerate(crops):
                bitmap = page.render(scale=2, crop=crop, draw_annots=False)
                image = bitmap.to_pil().convert('RGB')
                image.thumbnail((730, 820))
                canvas.paste(image, (index*750+10, 65))
                draw.text((index*750+10, 10), f'{article}: ' + ('current raw coordinates' if index==0 else f'view-origin normalized ({x0:.1f},{y0:.1f})'), fill='black')
                bitmap.close()
            canvas.save(out / f'origin_{article}.png')
            print(article, 'rotation', page.get_rotation(), 'bbox', page.get_bbox())
            page.close()


if __name__ == '__main__':
    main()

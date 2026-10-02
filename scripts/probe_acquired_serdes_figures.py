"""Read-only candidate probe for the frozen 30-paper acquisition target."""
import json
import sys
from collections import Counter
from pathlib import Path
import pypdfium2 as pdfium

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from serdes_figures import scan_document


def main():
    out = ROOT / 'outputs/serdes_figure_audit'
    inventory = json.loads((out / 'inventory.json').read_text(encoding='utf-8'))
    results = []
    for paper in inventory['papers']:
        if paper['figure']['status'] != 'missing_pdf':
            continue
        path = ROOT / 'ieee-pdf' / f"{paper['article_number']}.pdf"
        item = {'article_number': paper['article_number'], 'title': paper['title']}
        if not path.exists():
            item['status'] = 'missing'
        else:
            with pdfium.PdfDocument(str(path)) as document:
                candidates = scan_document(document, paper['title'])
                item.update(status='candidate' if candidates else 'no_candidate',
                            candidate_count=len(candidates), candidates=candidates)
        results.append(item)
        print(item['article_number'], item['status'], flush=True)
    payload = {'scope': 'read-only; not selected-image verification or FigureStore.prepare',
               'counts': dict(Counter(item['status'] for item in results)), 'papers': results}
    (out / 'acquisition_candidates.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    print(payload['counts'])


if __name__ == '__main__':
    main()

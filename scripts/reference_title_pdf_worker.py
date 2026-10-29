"""Bounded PDFium text evidence for only ambiguous reference titles; no inference."""
from __future__ import annotations

import argparse
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from paper_server.parsers.reference_normalization import compact


def quoted_titles(text, number):
    labels = list(re.finditer(r'(?m)^\s*\[\s*(\d{1,3})\s*\]', text))
    results = []
    for index, label in enumerate(labels):
        if int(label.group(1)) != number:
            continue
        stop = labels[index + 1].start() if index + 1 < len(labels) else len(text)
        reference = text[label.end():stop][:12000]
        title = re.search(r'[“"]([^”"]{5,1800})[”"]', reference)
        if not title:
            continue
        literal = title.group(1)
        # PDFium sometimes marks a discretionary line hyphen as U+FFFE. Only
        # join alphabetic word parts; never join digits across an unknown glyph.
        if re.search(r'(?<![^\W\d_])[\ufffe\u00ad]|[\ufffe\u00ad](?!\s*[^\W\d_])', literal):
            continue
        literal = re.sub(r'(?<=[^\W\d_])[\ufffe\u00ad]\s*(?=[^\W\d_])', '', literal)
        if any(c in literal for c in ('\ufffe', '\ufffd')):
            continue
        literal = compact(literal).rstrip(' ,;')
        results.append({'title': literal, 'reference_text': reference,
                        'author_text': reference[:title.start()],
                        'reference_text_sha256': hashlib.sha256(reference.encode()).hexdigest()})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    request = json.loads(args.request.read_text(encoding='utf-8'))
    path = Path(request['pdf']).resolve()
    allowed = [ROOT / p for p in ('ieee-pdf', 'optica-pdf', 'nature-pdf', 'DesignCon')]
    if path.suffix.lower() != '.pdf' or not any(path.is_relative_to(p.resolve()) for p in allowed):
        raise ValueError('PDF must be inside an authorized publisher library')
    if path.stat().st_size > 64 * 1024 * 1024 or len(request['entries']) > 100:
        raise ValueError('PDF request exceeds experiment limits')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != request['source_sha256']:
        raise ValueError('Source PDF changed')
    import pypdfium2 as pdfium
    rows = []
    with pdfium.PdfDocument(str(path)) as document:
        if len(document) > 300:
            raise ValueError('PDF exceeds page bound')
        for entry in request['entries']:
            page_number = entry['page']
            if not 1 <= page_number <= len(document):
                continue
            page = document[page_number - 1]
            textpage = page.get_textpage()
            text = textpage.get_text_range()
            textpage.close()
            page.close()
            for candidate in quoted_titles(text, entry['number']):
                rows.append({**candidate, 'number': entry['number'], 'page': page_number,
                    'source_sha256': digest, 'extractor': 'pypdfium2', 'extractor_version': version('pypdfium2')})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({'source_sha256': digest, 'rows': rows}, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()

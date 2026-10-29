"""Find a bounded bibliography before assigning any bibliographic fields.

Both numbered styles and inline short citations are supported. A count is not
evidence by itself: headings, sequential labels and citation cues must agree.
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from paper_server.parsers.reference_urls import standalone_url

VERSION = 'reference-boundaries-v4'
HEADING = re.compile(r'(?im)^[ \t]*(?:(?:[IVX]+|\d+)\.?[ \t]+)?(?:REFERENCES(?:[ \t]+AND[ \t]+LINKS)?|BIBLIOGRAPHY)[ \t]*:?[ \t\r]*$')
ADDITIONAL = re.compile(r'(?im)^[ \t]*Additional[ \t]+References[ \t]*:?[ \t\r]*$')
STOP = re.compile(r'(?im)^[ \t]*(?:(?:[IVX]+|\d+)\.?[ \t]+)?(?:'
    r'Author contributions|Additional information|Competing (?:financial )?interests|'
    r'Acknowledg(?:e?ments?|ment)|Introduction|Conclusion(?:s)?|'
    r'Fig(?:ure)?\.?[ \t]*\d+|TABLE[ \t]+(?:[IVX]+|\d+))\b')
FOOTER = re.compile(r'(?im)^.*(?:Authorized licensed use|Downloaded (?:on|from)|'
    r'©.*(?:IEEE|Optica|Springer|Macmillan)|^\s*NATURE PHOTONICS.*\bDOI:|'
    r'^\s*(?:20\d{2}\s+)?IEEE[^\n]*\|\s*DOI:|'
    r'DIGEST OF TECHNICAL PAPERS|ISSCC\s+20\d{2}\s*/|^\s*\d+\s+IEEE\s+(?:TRANSACTIONS|JOURNAL)|'
    r'^[A-Z][A-Z \-]+\s+et al\.:).*$')


def compact(raw):
    raw = re.sub(r'(?<=[^\W\d_])[\ufffe\u00ad]\s*(?=[^\W\d_])', '', raw)
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', raw)).strip()


def citation_cue(raw):
    return bool(re.search(r'\b(?:18|19|20)\d{2}\b|10\.\d{4,9}/|[“”"]\s*[^“”"]+[“”"]|\[Online\]', raw, re.I))


def damaged_characters(value):
    """Whitespace is legal; control, replacement and noncharacters are not text."""
    return [c for c in value if (unicodedata.category(c) == 'Cc' and c not in '\t\n\r')
            or c == '\ufffd' or 0xFDD0 <= ord(c) <= 0xFDEF or ord(c) & 0xFFFF in (0xFFFE, 0xFFFF)]


def uncertain_scientific_text(value):
    """Detect lost notation, without guessing a power or unit from context."""
    return bool((re.search(r'PRBS|Pseudorandom|Sequence Generator', value, re.I)
                 and re.search(r'\b2(?:\d{2,4}|[*°\'’]{1,3})\s*[-−]1\b', value))
                or re.search(r'\b0\.\d+\s*-?\s*m\s+CMOS\b', value, re.I)
                or re.search(r'\b\d+(?:\.\d+)?\s*-?\s*/\d{1,3}m\b',value)
                or re.search(r'/(?:Omega|Delta|times|mu)(?=\d|\s|m\b)',value))


def _parse_section(section, offset, positions, start=1):
    quotes = [(m.start(), m.end()) for m in re.finditer(r'[“"]\s*[^”"]+[”"]', section)]
    candidates = []
    for style, pattern in (('bracket', r'\[\s*(\d{1,3})\s*\]\s*'),
                           ('dot', r'(?m)^\s*(\d{1,3})\.\s+(?=[A-Za-zÀ-ž“”"])')):
        labels = [m for m in re.finditer(pattern, section) if not any(a < m.start() < b for a,b in quotes)]
        if not labels or [int(m[1]) for m in labels] != list(range(start, start + len(labels))):
            continue
        entries = []
        for i, label in enumerate(labels):
            end = labels[i+1].start() if i+1 < len(labels) else len(section)
            literal = section[label.end():end].strip()
            raw = compact(literal)
            if not raw or len(raw) > 5000 or not (citation_cue(raw) or standalone_url(raw)):
                entries = []
                break
            absolute = offset + label.start(1)
            page = next(p for position,p in reversed(positions) if position <= absolute)
            entries.append({'number': int(label[1]), 'page': page, 'raw': raw,
                'source_raw': literal, 'boundary_style': style, 'boundary_version': VERSION})
        if entries:
            candidates.append(entries)
    return candidates


def _later_citation(text, number):
    # Table cells such as "[8] [9] This work" and running body citations do not
    # establish a bibliography. Require a label at the start of a printed line,
    # a recognizable citation opening on that line, and bibliographic context.
    pattern = rf'(?m)^[ \t]*(?:\[[ \t]*{number}[ \t]*\]|{number}\.)[ \t]*([^\r\n]+)'
    for match in re.finditer(pattern, text):
        if re.match(r'(?:[A-Z][ \t]*\.[ \t]*-?[ \t]*|[“"]|IEEE\b|OIF\b|ITU\b|'
                    r'(?:[A-Z][a-z]+[ \t]+){1,3}[A-Z][\w’\'-]+[ \t]*,)', match[1]):
            tail = text[match.start():match.start()+2000]
            stop = STOP.search(tail)
            if citation_cue(tail[:stop.start()] if stop else tail):
                return True
    return False


def segment_references(pages):
    chunks, positions, headings = [], [], []
    length = 0
    for page, value in pages:
        value = FOOTER.sub('', value)
        positions.append((length, page))
        chunks.append(value + '\n')
        length += len(value) + 1
    text = ''.join(chunks)
    headings = list(HEADING.finditer(text))
    if not headings:
        return {'entries': [], 'status': 'extraction_unresolved', 'warnings': ['bibliography_heading_not_found']}
    candidates, continuation_warnings, additional_unresolved = [], False, False
    for heading in headings:
        section = text[heading.end():]
        stop = STOP.search(section)
        section = section[:stop.start()] if stop else section
        additional = ADDITIONAL.search(section)
        if additional:
            section = section[:additional.start()]
        # A biography can follow the last entry without a section heading.
        bio = re.search(r'(?im)^[ \t]*[A-Z][\w .’\-]+(?:\([^)]{0,150}\))?\s*'
                        r'(?:received\s+the|was\s+born\s+(?:in|on)|has\s+been\s+with)\b', section)
        if bio:
            section = section[:bio.start()]
        for entries in _parse_section(section, heading.end(), positions):
            cutoff = heading.end() + len(section)
            # An explicitly named continuation is evidence for joining two
            # disjoint runs; arbitrary figure/table text is never joined.
            for extra in ADDITIONAL.finditer(text, cutoff):
                tail = text[extra.end():]
                end = STOP.search(tail)
                tail = tail[:end.start()] if end else tail
                runs = _parse_section(tail, extra.end(), positions, len(entries)+1)
                if len(runs) == 1:
                    entries = entries + runs[0]
                    cutoff = extra.end() + len(tail)
                else:
                    additional_unresolved = True
            if _later_citation(text[cutoff:], len(entries)+1):
                continuation_warnings = True
            candidates.append(entries)
    unique = {tuple((e['number'], e['page'], e['raw']) for e in entries): entries for entries in candidates}
    if len(unique) != 1:
        return {'entries': [], 'status': 'extraction_unresolved',
                'warnings': ['ambiguous_or_noncontiguous_bibliography' if candidates else 'unsupported_bibliography_layout']}
    warnings = ['possible_omitted_references'] if continuation_warnings else []
    if additional_unresolved:
        warnings.append('unresolved_additional_references')
    return {'entries': next(iter(unique.values())), 'status': 'ready', 'warnings': warnings}


def entry_quality(entries):
    warnings = []
    numbers = [e['number'] for e in entries]
    if numbers != list(range(1, len(numbers) + 1)):
        warnings.append('noncontiguous_labels')
    for row in entries:
        raw = row['raw']
        literal = row.get('source_raw', raw)
        # A malformed OCR label is evidence of an unsplit citation, not
        # permission to guess its number. Ask the layout parser to separate it.
        if re.search(r'(?:\(\s*\d{1,3}\s*\]|\[\s*\d{1,3}\s*\))\s*[A-Z]\s*\.', literal):
            warnings.append('collapsed_reference_labels')
        if damaged_characters(raw):
            warnings.append('damaged_reference_text')
        if uncertain_scientific_text(raw):
            warnings.append('uncertain_scientific_notation')
        if re.search(r'\[\s*\d+\s*\].*\[\s*\d+\s*\]', raw):
            warnings.append('collapsed_reference_labels')
        if re.search(r'(?i)\b(?:Fig(?:ure)?\.?\s*\d|received the|was born (?:in|on)|has been with|Authorized licensed use)\b', raw):
            warnings.append('non_bibliographic_tail')
        if len(raw) >= 5000:
            warnings.append('source_at_truncation_bound')
    return sorted(set(warnings))


def choose_entries(primary, source):
    """Prefer independent source segmentation only for an observed defect.

    A disagreement never silently becomes a successful extraction. Unchanged
    primary boundaries retain their original raw strings and inference cache.
    """
    secondary = source['entries']
    quality_warnings = entry_quality(primary)
    warnings = list(quality_warnings)
    warnings.extend(w for w in source.get('warnings', [])
                    if w.startswith('ocr_') or w in ('possible_omitted_references','unresolved_additional_references'))
    def damage_rank(entries):
        hard = set(entry_quality(entries)) - {'damaged_reference_text','uncertain_scientific_notation'}
        return (len(hard) * 10000 + sum(len(damaged_characters(e['raw'])) for e in entries)
                + sum(uncertain_scientific_text(e['raw']) for e in entries) * 10)
    if secondary and (not primary or len(primary) < len(secondary)
                      or quality_warnings and damage_rank(secondary) < damage_rank(primary)):
        # A repaired independent source resolves bad primary glyphs, but the
        # original diagnostic is retained separately for inspection.
        resolved = [w for w in quality_warnings if w == 'damaged_reference_text' and w not in entry_quality(secondary)]
        chosen = secondary
        if primary and len(primary) == len(secondary) and any(e.get('ocr_required_review') for e in secondary):
            chosen = []
            for original, replacement in zip(primary, secondary):
                defects = set(entry_quality([original])) - {'noncontiguous_labels'}
                if (not defects and original['number'] == replacement['number']
                        and SequenceMatcher(None, original['raw'].casefold(), replacement['raw'].casefold(), autojunk=False).ratio() >= .82):
                    chosen.append({**original, 'native_text_preferred': True})
                else:
                    chosen.append(replacement)
        return chosen, {'source': 'bounded_pdf_text', 'warnings': sorted(set(warnings)-set(resolved)),
            'resolved_warnings': resolved,
            'previous_count': len(primary), 'count': len(secondary), 'version': VERSION}
    if secondary and len(primary) != len(secondary):
        warnings.append('independent_bibliography_count_disagreement')
    if not primary:
        warnings.extend(source['warnings'])
    return primary, {'source': 'primary', 'warnings': sorted(set(warnings)),
                     'count': len(primary), 'version': VERSION}

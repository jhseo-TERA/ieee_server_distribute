"""Bibliographic roles constrain parser agreement; evidence is kept per field."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import re
import unicodedata
from dataclasses import dataclass

from paper_server.parsers.reference_segmentation import compact, damaged_characters, uncertain_scientific_text
from paper_server.parsers.reference_urls import standalone_url, logged_url_row
from paper_server.parsers.reference_identifiers import literal_doi
from paper_server.parsers.reference_scope import citation_scope

VERSION = 'reference-roles-v3'
QUOTE = re.compile(r'[“”"]([^“”"]{4,1800})[“”"]')


@dataclass(frozen=True)
class QuoteSpan:
    text: str
    begin: int
    stop: int
    def __getitem__(self, index):
        return self.text if index == 1 else None
    def start(self):
        return self.begin
    def end(self):
        return self.stop


def quoted_title(raw):
    """Keep balanced inner curly quotes inside the outer article title."""
    opening=re.search(r'[“”"]',raw)
    if not opening:return None
    if opening[0]=='“':
        depth=1
        for position in range(opening.end(),min(len(raw),opening.end()+1801)):
            if raw[position]=='“':depth+=1
            elif raw[position]=='”':
                depth-=1
                if depth==0:
                    text=raw[opening.end():position]
                    return QuoteSpan(text,opening.start(),position+1) if len(text)>=4 else None
        # Do not turn an unmatched inner quote into a shorter complete title.
        if '“' in raw[opening.end():]:return None
    return QUOTE.search(raw)
YEAR = re.compile(r'\b(?:18|19|20)\d{2}\b')
CORPORATE = re.compile(r'^(?:IEEE\b|ITU-?T\b|ISO\b|IEC\b|IETF\b|NIST\b|OIF\b|'
    r'Implementation Agreement\b|Optical Internetworking Forum\b|Ethernet Alliance\b|Cisco\b|Ansys\b|SFF-\d)', re.I)


def key(value):
    return ''.join(c for c in compact(str(value or '')).casefold() if c.isalnum())


def personal_name(raw):
    prefix = re.split(r'[“”"]', raw, maxsplit=1)[0]
    prefix = re.split(r'\bet\s+al\.?', prefix, maxsplit=1, flags=re.I)[0].strip(' ,;')
    prefix = re.sub(r'\s+etc\.?(?=\s*[,;]|$)', '', prefix)
    prefix = re.sub(r'\b([A-Z])\s+\.', r'\1.', prefix)
    if not prefix:
        return {'no_personal_author': True}
    if re.match(r'^[—–_]{2,}', prefix):
        return {'repeat': True}
    if (CORPORATE.match(prefix) or prefix.startswith('http')
            or re.match(r'^[^,]{2,70},?\s+(?:Inc\.?|Ltd\.?|Corp\.?|Corporation|LLC|GmbH)\b', prefix, re.I)
            or not re.match(r'^[A-Z]\.',prefix) and re.search(r'\b(?:JESD\d[\w-]*|PCI[- ]SIG|PCI Express)\b',prefix,re.I)):
        return {'organization': prefix.split(',')[0].strip()}
    match = re.match(r'^((?:[A-Z]\.(?:-?[A-Z]\.)?\s*)+)([^,]+)', prefix)
    if match:
        family = re.split(r'\s+and\s+', match[2], maxsplit=1)[0].strip(' ,;')
        # A terminal period may follow a spelled-out surname. A single
        # initial, or a damaged et-al marker, must not become a new surname.
        if family.endswith('.') and ' ' not in family and len(family)>2:family=family[:-1]
        if re.search(r'\b(?:et|el|ef|er)\s+\w+\b',family,re.I):return {'ambiguous':True}
        # A hyphen attached to an initial does not identify a surname boundary.
        if family.startswith('-') or not re.fullmatch(r"[^\W\d_][\w’'\-]*(?:\s+[^\W\d_][\w’'\-]*){0,3}", family):
            return {'ambiguous': True}
        return {'family': family, 'initials': re.findall(r'[A-Z]', match[1])}
    match = re.match(r"^([^,]+),\s*((?:[A-Z]\.(?:-?[A-Z]\.)?\s*)+)", prefix)
    if match and len(match[1].split()) <= 3 and not re.match(r'\s*[A-Z][a-z]',prefix[match.end():]):
        return {'family': match[1].strip(), 'initials': re.findall(r'[A-Z]', match[2])}
    return {'ambiguous': True}


def publication_year(raw, title):
    value = citation_scope(raw)[0]
    quote = quoted_title(value)
    if quote:
        value = value[:quote.start()] + ' ' + value[quote.end():]
    elif re.search(r'[“”"]',value):
        # A malformed/unclosed quote must not let a parser's overlong title
        # swallow the venue and the publication year.
        venue=re.search(r'\bin\s+(?:IEEE|Proc\.)\b',value,re.I)
        if venue:
            value=value[venue.start():]
    elif title:
        # Unquoted report titles commonly contain their forecast interval.
        value = value.replace(title, ' ', 1)
    # A wrapped URL path can contain years and four-digit article identifiers.
    # A terminal availability clause is not publication-date evidence.
    value = re.split(r'\[Online\]|\bAvailable\s*(?::|at\b)',value,maxsplit=1,flags=re.I)[0]
    value = re.sub(r'https?\s*:\s*//\S+', ' ', value, flags=re.I)
    value = re.sub(r'10\.\d{4,9}\s*/\s*\S+', ' ', value, flags=re.I)
    value = re.sub(r'\barXiv\s*:\s*\d{4}\.\d{4,5}(?:v\d+)?', ' ', value, flags=re.I)
    value = re.sub(r'\b(DesignCon|ISSCC|ESSCIRC|ECOC|OFC|IEDM)(20\d{2})\b',r'\1 \2',value,flags=re.I)
    value = re.sub(r'\[\s*(?:accessed|retrieved|visited)\b[^\]]{0,120}\]', ' ', value, flags=re.I)
    # A page number followed by a journal running header belongs to the host
    # PDF, rather than the cited publication (whose title was removed above).
    value = re.sub(r'\b\d{1,5}\s+(?:IEEE\s+)?(?:JOURNAL|TRANSACTIONS)\s+(?:OF|ON)\s+'
                   r'[^,]{1,100},?\s*VOL\.[^\n]*', ' ', value, flags=re.I)
    value = re.split(r'\b(?:He|She)\s+(?:is|was|received|joined)\b|\b[A-Z][\w .\-]+\([^)]*\)\s+received the\b',value,maxsplit=1)[0]
    value = re.sub(r'(?i)\bAccessed\s*:[^.]*\b(?:18|19|20)\d{2}\b\.?', ' ', value)
    # Also accept month abbreviations containing periods in an Accessed clause.
    value = re.sub(r'(?i)\bAccessed\s*:.{0,65}?\b(?:18|19|20)\d{2}\b\.?', ' ', value)
    value = re.sub(r'(?i)\b(?:visited|accessed|retrieved)\s+on\b.{0,65}?\b(?:18|19|20)\d{2}\b', ' ', value)
    value = re.sub(r'(?i)\b(?:visited|accessed|retrieved)\s+(?:[A-Za-z]{3,12}\.?\s+)?\d[^;\n]{0,40}?\b(?:18|19|20)\d{2}\b',' ',value)
    value = re.sub(r'\b(?:18|19|20)\d{2}\s*[-–—]\s*(?:18|19|20)\d{2}\b', ' ', value)
    value = re.sub(r'(?i)\b(?:pp?\.|pages?)\s*\d+(?:\s*[-–—]\s*\d+)?', ' ', value)
    value = re.sub(r'(?i)\b(?:document|standard|std\.?|OIF|P802\.\w+)[^,;]{0,45}', ' ', value)
    value = re.sub(r'\b\d+\s*,\s*(?:18|19|20)\d{2}(?=\s*\((?:18|19|20)\d{2}\))',' ',value)
    found = sorted({int(m[0]) for m in YEAR.finditer(value)})
    return (found[0] if len(found) == 1 else None), found


def corporate_title(raw, current):
    value = re.split(r'(?i)\bAccessed\s*:|\[Online\]|https?\s*:', raw, maxsplit=1)[0].strip(' .,;')
    value = re.sub(r'(?i)\s*[-–]?\s*Online\s*$', '', value).strip(' .,;')
    if re.search(r'(?i)\bDraft amendment\b', value):
        return re.split(r'(?i),\s*(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)',
                        re.search(r'(?i)\bDraft amendment\b.*', value)[0])[0].strip(' .,;')
    if re.match(r'(?i)^SFF-\d+\s*,', value):
        value = value.split(',', 1)[1].strip()
        return re.split(r'(?i)\bRevision\s+\d', value)[0].replace('“','').replace('”','').strip(' .,;')
    if re.match(r'(?i)^Optical Internetworking Forum\b|^OIF\s*,', value):
        value = re.sub(r'(?i)^(?:Optical Internetworking Forum(?:\s*\(OIF\))?|OIF)\s*,?\s*','',value)
        return re.split(r'(?i),\s*(?:IA\s*#|Contribution\b|document\b)', value)[0].strip(' .,;')
    if current:
        return re.sub(r'(?i)\s*[-–]?\s*Online\s*$', '', current).strip(' ,;')
    if re.match(r'(?i)^(?:IEEE|ISO|IEC|ITU-?T)\s+Standard\b', value):
        return re.sub(r',\s*(?:18|19|20)\d{2}\s*$', '', value)
    if 'Accessed' in raw:
        return value
    return current


def apply_roles(rows, source_rows=None):
    """Reconcile only explicit roles, never invent missing citation metadata.

    source_rows are independently bounded PDF text aligned by the caller. The
    original raw/model fields remain available in field_changes and source_view.
    Repeated authors resolve only within this bibliography, never globally.
    """
    source_rows = source_rows or {}
    names = [personal_name(source_rows.get(r['number'], {}).get('raw', r['raw'])) for r in rows]
    output = []
    for row, name in zip(rows, names):
        if standalone_url(row['raw']):
            result = logged_url_row(row)
            result.update(role_version=VERSION, field_changes=[], source_view={
                'raw': row['raw'], 'page': row['page'], 'source_raw': row.get('source_raw', row['raw']),
                'sha256': hashlib.sha256(row['raw'].encode()).hexdigest(),
                'extractor': row.get('extractor', 'original_reference')})
            output.append(result)
            continue
        result = deepcopy(row)
        source = source_rows.get(row['number'], {})
        raw = source.get('raw', row['raw'])
        raw,excluded_tail=citation_scope(raw)
        result['citation_text']=raw
        if excluded_tail:result['excluded_source_tail']=excluded_tail
        changes = []
        def assign(field, value, reason):
            if result.get(field) != value:
                changes.append({'field': field, 'before': result.get(field), 'after': value, 'reason': reason})
                result[field] = value
        if name.get('no_personal_author'):
            assign('author', None, 'no_personal_author_before_title')
        if name.get('organization'):
            assign('author', None, 'organization_is_not_personal_family_name')
            result['author_organization'] = name['organization']
            assign('title', corporate_title(raw, result.get('title')), 'resource_title_boundary')
        elif name.get('repeat'):
            previous = output[-1] if output else {}
            if previous.get('author') and not previous.get('author_organization'):
                assign('author', previous['author'], 'same_bibliography_repeat_author')
                result['author_repeat_of'] = previous['number']
            else:
                assign('author', None, 'repeat_author_has_no_local_predecessor')
                result['needs_review'] = True
        elif name.get('family'):
            family = name['family']
            aliases = [n['family'] for n in names if n.get('family') and len(n.get('initials', [])) == 1
                       and len(name.get('initials', [])) == 2 and n['initials'][0] == name['initials'][0]
                       and n['family'].endswith(family) and n['family'] != family
                       and n['family'][0].upper() == name['initials'][1]]
            if len(set(aliases)) == 1:
                family = aliases[0]
            original_name=personal_name(row['raw']).get('family')
            if source and original_name and result.get('author')==original_name and family.startswith(original_name+' '):
                # A font-decoding artifact can look like extra surname words.
                # Two readable sources disagree: retain the existing explicit
                # name and request review instead of silently extending it.
                family=original_name
                result['needs_review']=True
                result.setdefault('warnings',[]).append('independent_author_source_disagreement')
            assign('author', family, 'explicit_personal_name_boundary')
            if ' ' in family:
                result['needs_review'] = True
                result.setdefault('warnings', []).append('compound_family_name_requires_review')
        elif name.get('ambiguous'):
            result['needs_review'] = True
            result.setdefault('warnings', []).append('personal_name_boundary_uncertain')
        if result.get('author') and re.search(r'[ÿ\ufffd\ufffe\x00-\x1f]',result['author']):
            result['needs_review'] = True
            result.setdefault('warnings', []).append('personal_name_glyph_requires_review')
        quote = quoted_title(raw)
        if quote and not name.get('organization'):
            title = quote[1].strip(' ,;')
            # Existing normalized TeX has its own stricter recovery evidence.
            if not re.search(r'\\|\$', title) and not re.search(r'\\|\$|[_^]',result.get('title') or ''):
                previous = result.get('title')
                old_key, new_key = key(previous), key(title)
                remaining = iter(new_key)
                retains_original = all(character in remaining for character in old_key)
                if not previous or retains_original:
                    assign('title', title, 'complete_source_quote')
                else:
                    result['needs_review'] = True
                    result.setdefault('warnings', []).append('independent_title_text_disagreement')
        if not quote and re.search(r'^.{0,90}?,\s*(?:IEEE\s+)?(?:ESSCIRC|ISSCC|JSSC|TCAS-I|AICSP)\s*[,;]', raw):
            assign('title', None, 'short_citation_omits_title')
        if not quote and re.search(r'\bet\s+al\.?\s*,?\s+in\s+(?:20\d{2}\s+)?IEEE\s+.{0,90}?Conference',raw,re.I):
            assign('title', None, 'short_citation_enters_conference_without_article_title')
        year, candidates = publication_year(raw, result.get('title'))
        if year is None and not candidates and source:
            original_year, original_candidates = publication_year(row['raw'],row.get('title'))
            if original_year and original_year == result.get('year'):
                year = original_year
        assign('year', year, 'publication_context_excludes_title_pages_access_date_and_url')
        if len(candidates) > 1:
            result['needs_review'] = True
            result.setdefault('warnings', []).append('ambiguous_publication_year')
        doi_text=source.get('source_raw',raw)
        doi=literal_doi(doi_text)
        if doi:
            assign('doi',doi['doi'],'literal_doi_with_supported_continuations')
            result['doi_evidence']=doi
            if doi.get('ambiguous_discretionary_hyphen'):
                result['needs_review']=True
                result.setdefault('warnings',[]).append('doi_discretionary_hyphen_requires_review')
            if doi['incomplete']:
                result['needs_review']=True
                result.setdefault('warnings',[]).append('doi_source_incomplete')
        elif source and result.get('doi') and not literal_doi(row.get('source_raw',row['raw'])):
            assign('doi',None,'doi_absent_from_independent_reference_and_original_citation_scope')
            result.setdefault('warnings',[]).append('unbacked_doi_removed')
        if result.get('title') and len(result['title']) > 400:
            assign('title', None, 'overlong_title_requires_review')
            result['needs_review'] = True
        title = result.get('title') or ''
        odd_prefix = re.search(r'\b\d+(?:\.\d+)?\s*-?\s*([^\W\d_])m\b',title)
        odd_multiplier = re.search(r'(?:Gb/s|Gbps)\s+([^\W\d_])\d+\b',title)
        corrupted_latin = any(m and ord(m[1])>127 and unicodedata.name(m[1],'').startswith('LATIN')
                              for m in (odd_prefix,odd_multiplier))
        if (uncertain_scientific_text(title) or re.search(r'\b0\.\d+\s*-\s+m\b',title)
                or corrupted_latin or re.search(r'\b\d+\s+\d+\s*(?:Gb/s|Gbps)\b|\b\d+\s+oversampling\b',title,re.I)
                or re.search(r'\b\d+\.\d+\s+m\s+(?:InGa|AlGa|GaAs)',title)):
            result['needs_review'] = True
            result.setdefault('warnings', []).append('possible_lost_scientific_glyph')
        result['role_version'] = VERSION
        result['field_changes'] = changes
        proof_raw=source.get('raw',row['raw'])
        result['source_view'] = {'raw': proof_raw, 'page': source.get('page', row['page']),
            'source_raw': source.get('source_raw', raw),
            'sha256': hashlib.sha256(proof_raw.encode()).hexdigest(),
            'extractor': source.get('extractor', 'original_reference')}
        if source.get('ocr_required_review') or row.get('ocr_required_review'):
            result['ocr_required_review'] = True
            result['needs_review'] = True
            result['warnings'] = list(dict.fromkeys(result.get('warnings', []) + ['ocr_source_requires_review']))
            result['source_view']['ocr_required_review'] = True
        active = []
        if damaged_characters(raw) or any(damaged_characters(str(result.get(f) or '')) for f in ('title', 'author')):
            active.append('damaged_reference_text')
        # A failed grounding check is unresolved only while its field is empty.
        # Explicit short citations legitimately omit titles and remain null.
        for field in ('title', 'author', 'year', 'doi'):
            if not result.get(field) and field + '_not_in_source' in result.get('warnings', []):
                active.append('unresolved_field_grounding')
        if result.get('llm_status') == 'failed':
            active.append('local_model_review_failed')
        if not result.get('year') and name.get('family') and result.get('title'):
            active.append('publication_year_unresolved')
        omitted_title = ('title_absent_in_short_reference' in result.get('warnings', [])
                         or any(c['reason'].startswith('short_citation_') for c in changes))
        if not result.get('title') and 'unquoted_or_missing_title' in result.get('warnings', []) and not omitted_title:
            active.append('unresolved_title_boundary')
        if active:
            result['needs_review'] = True
            result['warnings'] = list(dict.fromkeys(result.get('warnings', []) + active))
        output.append(result)
    return output

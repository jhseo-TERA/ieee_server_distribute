"""Exact source-image reviews and user-requested display transliteration.

These corrections are curated evidence, not new automatic extraction accuracy.
"""
from __future__ import annotations

import hashlib
import json
import unicodedata
import re
from pathlib import Path

from paper_server.paths import ROOT
from paper_server.parsers.reference_normalization import match_key

VERSION='reference-source-reviews-v2'
REGISTRY=ROOT/'config/reference_source_reviews.json'


def load_source_reviews():
    return json.loads(REGISTRY.read_text(encoding='utf-8'))['records']


def display_name(value):
    if value is None:return None
    return unicodedata.normalize('NFKC',value).translate(str.maketrans({'ÿ':'y','Ÿ':'Y'}))


def _context_key(value):
    return re.sub(r'\s+','',unicodedata.normalize('NFC',value or '').casefold()).translate(str.maketrans({'–':'-','−':'-'}))


def _review_binding(record,row):
    actual_hash=hashlib.sha256(row.get('raw','').encode()).hexdigest()
    if record['raw_sha256']==actual_hash:return {'method':'exact_raw_sha256','before_applied':record['before']}
    # A source-image review remains stronger evidence than a new OCR spelling,
    # but only for the identical PDF, page, ordinal and year (checked by caller).
    if record['field']!='title_display' or not row.get('ocr_required_review'):return None
    proof=record.get('evidence',{});image=ROOT/str(proof.get('image',''))
    if proof.get('kind')!='source_pdf_image_review' or not image.is_file():return None
    if hashlib.sha256(image.read_bytes()).hexdigest()!=proof.get('image_sha256'):return None
    before,after=(_context_key(record[k]) for k in ('before','after'))
    prefix=0
    while prefix<min(len(before),len(after)) and before[prefix]==after[prefix]:prefix+=1
    suffix=0
    while suffix<min(len(before)-prefix,len(after)-prefix) and before[-suffix-1]==after[-suffix-1]:suffix+=1
    left,right=before[:prefix],before[len(before)-suffix:] if suffix else ''
    bpart=before[prefix:len(before)-suffix if suffix else len(before)]
    apart=after[prefix:len(after)-suffix if suffix else len(after)]
    if min(len(left)+len(right),len(before))<32 or max(len(bpart),len(apart))>8 or re.search(r'\d',bpart+apart):return None
    resolution=row.get('source_review_resolution',{})
    candidate=row.get('title_display')
    if candidate==record['after'] and resolution.get('record_id')==record['id']:
        candidate=resolution.get('binding',{}).get('before_applied',candidate)
    current=_context_key(candidate)
    if not current.startswith(left) or not current.endswith(right):return None
    middle=current[len(left):len(current)-len(right) if right else len(current)]
    if len(middle)>8 or re.search(r'\d',middle) or len(left)+len(right)<.9*len(current):return None
    if current not in _context_key(row.get('raw','')):return None
    return {'method':'same_source_reviewed_title_context_with_ocr_variant','before_applied':candidate,
            'left_context':left,'right_context':right,'observed_fragment':middle,
            'reviewed_before_fragment':bpart,'reviewed_after_fragment':apart,'image_sha256':proof['image_sha256']}


def apply_source_reviews(row,source_sha256,records=None):
    records=load_source_reviews() if records is None else records
    matches=[(r,_review_binding(r,row)) for r in records if r['pdf_sha256']==source_sha256 and r['number']==row.get('number')
        and r['page']==row.get('page') and r['year']==row.get('year')]
    matches=[(r,b) for r,b in matches if b]
    if len(matches)!=1:return row
    record,binding=matches[0];field=record['field']
    if field not in ('author','title_display'):return row
    if binding['method']=='exact_raw_sha256' and row.get(field)!=record['before'] and not (row.get(field)==record['after'] and row.get('source_review_resolution')):
        return row
    result={**row,field:record['after']}
    result['source_review_resolution']={'version':VERSION,'record_id':record['id'],
        'record_sha256':hashlib.sha256(json.dumps(record,sort_keys=True).encode()).hexdigest(),
        'pdf_sha256':source_sha256,'raw_sha256':hashlib.sha256(row.get('raw','').encode()).hexdigest(),
        'reviewed_raw_sha256':record['raw_sha256'],'binding':binding,'field':field,
        'before':record['before'],'after':record['after'],'evidence':record['evidence']}
    if record.get('first_author_name'):result['first_author_name']=record['first_author_name']
    result['warnings']=[w for w in result.get('warnings',[]) if w not in record['resolved_warnings']]
    if field=='title_display':
        result['title_match_key']=match_key(record['after'])
        steps=[s for s in result.get('normalization_steps',[]) if s.get('rule')!='reviewed_source_display']
        result['normalization_steps']=steps+[{'rule':'reviewed_source_display','record_id':record['id'],
            'before':record['before'],'display':record['after']}]
    return result


def verified_source_review(row,source_sha256,records=None):
    expected=apply_source_reviews(row,source_sha256,records)
    return bool(expected is not row and row.get('source_review_resolution')==expected.get('source_review_resolution')
                and row.get(expected['source_review_resolution']['field'])==expected[expected['source_review_resolution']['field']])

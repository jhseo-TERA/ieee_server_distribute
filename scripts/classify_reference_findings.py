"""Source-grounded adjudication; uncertain source roles are never forced normal.

Uses freshly decoded, hash-pinned PDF pages. Model quotes/reasons are the claim
being checked, not gold. Individual decisions retain the literal source span.
"""
from __future__ import annotations
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
import json,re,sys,unicodedata
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.adjudicate_reference_findings import OUT,read,save,digest
from paper_server.parsers.reference_roles import personal_name,publication_year,quoted_title
from paper_server.parsers.reference_identifiers import literal_doi
from paper_server.parsers.reference_segmentation import segment_references,compact,damaged_characters,uncertain_scientific_text
from paper_server.parsers.reference_display import micro_unit_display
from paper_server.parsers.reference_scope import citation_scope


def align_key(value):
    return ''.join(c for c in unicodedata.normalize('NFKD',str(value)).casefold() if c.isalnum())


def display_key(value):
    return micro_unit_display(compact(str(value or ''))).casefold().strip(' ,;.')


def source_span(row,native):
    choices=[];original=align_key(row['raw']);number=row['number']
    entries=segment_references([(p['page'],p['text']) for p in native['pages']])['entries']
    choices.extend(e for e in entries if e['number']==number)
    # Independent fallback does not require the whole bibliography to have
    # passed the old contiguous-number policy. Require a literal line label.
    pages=native['pages'];combined='\n'.join(p['text'] for p in pages)
    for match in re.finditer(rf'(?m)^[ \t]*(?:\[[ \t]*{number}[ \t]*\]|{number}\.)[ \t]*',combined):
        later=re.search(rf'(?m)^[ \t]*(?:\[[ \t]*{number+1}[ \t]*\]|{number+1}\.)[ \t]*',combined[match.end():])
        end=match.end()+later.start() if later else match.end()+5000
        raw=combined[match.end():end][:5000].strip()
        page=1
        position=0
        for p in pages:
            if position<=match.start()<position+len(p['text'])+1:page=p['page'];break
            position+=len(p['text'])+1
        choices.append({'number':number,'page':page,'raw':compact(raw),'source_raw':raw,'extractor':'pypdfium2'})
    scored=[]
    for choice in choices:
        key=align_key(choice['raw']);ratio=SequenceMatcher(None,original,key,autojunk=False).ratio()
        prefix=SequenceMatcher(None,original[:120],key[:120],autojunk=False).ratio()
        if ratio>=.68 or min(len(original),len(key))>=100 and prefix>=.92:
            scored.append((ratio,choice))
    return max(scored,key=lambda x:x[0])[1] if scored else None


def adjudicate(row,issue,source):
    field=issue['field'];value=row.get('title_display',row.get('title')) if field=='title' else row.get(field)
    result={'field':field,'claim_kind':issue['kind'],'model_evidence':issue['evidence'],
            'before':value,'status':'unresolved','cause':'source_role_or_glyph_ambiguous'}
    if not source:
        result['cause']='independent_reference_alignment_unresolved';return result
    raw=citation_scope(source['raw'])[0];literal=citation_scope(source.get('source_raw',raw))[0]
    result.update(source_page=source['page'],source_text=raw,source_sha256=__import__('hashlib').sha256(raw.encode()).hexdigest())
    def decide(status,cause,expected=None):
        result.update(status=status,cause=cause,source_expected=expected);return result
    if field=='author':
        name=personal_name(raw)
        if name.get('family'):
            expected=name['family']
            if damaged_characters(expected) or re.search(r'[\u0300-\u036f]',expected):
                return decide('unresolved','unresolved_author_glyph_geometry')
            if display_key(value)==display_key(expected):
                return decide('not_pipeline_error','first_author_matches_source_other_text_not_target',expected)
            if value and expected.startswith(str(value)+' '):
                return decide('unresolved','author_extension_requires_visual_review')
            if len(name.get('initials',[]))>=2 and str(value or '').endswith(expected) and str(value)[0].upper()==name['initials'][-1]:
                return decide('unresolved','compound_surname_initial_cannot_disprove_expansion')
            return decide('confirmed_pipeline_error','first_author_value_differs_from_explicit_source',expected)
        if name.get('organization') or name.get('no_personal_author'):
            if value is None:return decide('not_pipeline_error','no_personal_author_in_source',None)
            return decide('confirmed_pipeline_error','organization_assigned_to_personal_author',None)
        return result
    if field=='title':
        quote=quoted_title(raw)
        if quote:
            expected=quote[1].strip(' ,;')
            if damaged_characters(expected):return decide('unresolved','title_source_encoding_requires_visual_review')
            if uncertain_scientific_text(expected):
                return decide('unresolved','independent_source_still_loses_scientific_symbols')
            if re.search(r'/(?:Omega|Delta|times|mu)(?=\d|\s|m\b)|/\d{1,3}m\b',str(value)) and not re.search(r'[ΩµμΔ×]',expected):
                return decide('unresolved','independent_source_still_loses_scientific_symbols')
            if display_key(value)==display_key(expected):
                return decide('not_pipeline_error','quoted_title_matches_source',expected)
            # Case/word-wrap punctuation is distinct from a factual value loss.
            if align_key(value)==align_key(expected):
                return decide('unresolved','title_typography_or_scientific_operator_requires_visual_review',expected)
            if not re.search(r'\\|\$|\ufffd|/\d+m\b',expected):
                return decide('confirmed_pipeline_error','title_differs_from_complete_source_quote',expected)
        if value is None and re.search(r'\bet\s+al\.?\s*,?\s+in\b',raw,re.I):
            return decide('not_pipeline_error','short_citation_does_not_print_article_title',None)
        return result
    if field=='year':
        title=quoted_title(raw)
        expected,candidates=publication_year(raw,title[1] if title else row.get('title'))
        if len(candidates)>1:return decide('unresolved','multiple_printed_publication_years',None)
        if expected is not None:
            return decide('not_pipeline_error' if value==expected else 'confirmed_pipeline_error',
                'publication_year_matches_source' if value==expected else 'explicit_publication_year_missing_or_wrong',expected)
        if value is None:return decide('not_pipeline_error','no_unique_publication_year_printed',None)
        return decide('unresolved','candidate_year_without_unique_source_context')
    if field=='doi':
        expected=literal_doi(literal)
        if not expected:
            if re.search(r'\bdoi\s*:\s*10\.\d+',raw,re.I):return decide('unresolved','source_doi_itself_truncated')
            if value is None:return decide('not_pipeline_error','doi_not_printed_in_source',None)
            return decide('confirmed_pipeline_error','doi_without_literal_source',None)
        if expected['incomplete']:return decide('unresolved','source_doi_itself_truncated',expected['doi'])
        return decide('not_pipeline_error' if display_key(value)==display_key(expected['doi']) else 'confirmed_pipeline_error',
            'doi_matches_literal_source' if display_key(value)==display_key(expected['doi']) else 'doi_continuation_lost',expected['doi'])
    if field in ('boundary','source'):
        if re.search(r'\b(?:Fig(?:ure)?\.?\s*\d+(?:\.\d+)*\s*:|Authorized licensed use)',row['raw'],re.I):
            return decide('confirmed_pipeline_error','non_bibliographic_tail_in_reference')
        if re.search(r'\b(?:19|20)\d{2}\.[A-Z]\.\s*[A-Z][a-z]+',row['raw']):
            return decide('unresolved','unlabelled_citations_merged_in_printed_source')
        return decide('unresolved','bibliography_boundary_needs_visual_review')
    return result


def run(out=OUT):
    cases=read(out/'cases.json');results=[];native_cache={};evidence_cache={}
    manual=read(out/'visual-adjudications.json') if (out/'visual-adjudications.json').exists() else {}
    for case in cases:
        pid=case['finding']['id']
        if pid not in native_cache:
            path=out/'native-full'/(pid+'.json');native_cache[pid]=read(path)
            if native_cache[pid]['source_sha256']!=case['pdf_sha256']:raise ValueError('Fresh native source identity mismatch')
            evidence_cache[pid]=digest(path)
        source=source_span(case['row'],native_cache[pid])
        decisions=[]
        for index,issue in enumerate(case['finding']['issues']):
            decision=adjudicate(case['row'],issue,source)
            override=manual.get(case['key']+'#'+str(index))
            if override:
                path=out/override['image']
                if not path.exists() or digest(path)!=override['image_sha256']:raise ValueError('Visual evidence changed')
                decision.update(override)
            decisions.append(decision)
        statuses={d['status'] for d in decisions}
        status='confirmed_pipeline_error' if 'confirmed_pipeline_error' in statuses else 'unresolved' if 'unresolved' in statuses else 'not_pipeline_error'
        results.append({'key':case['key'],'pdf':case['finding']['pdf'],'pdf_sha256':case['pdf_sha256'],
            'number':case['row']['number'],'status':status,'decisions':decisions,
            'native_source_path':str((out/'native-full'/(pid+'.json')).relative_to(out)),
            'native_source_sha256':evidence_cache[pid],'source_aligned':source is not None})
    save(out/'adjudications.json',results)
    summary={'review_candidates':len(results),'cases':Counter(r['status'] for r in results),
        'issues':Counter(d['status'] for r in results for d in r['decisions']),
        'causes':Counter(d['cause'] for r in results for d in r['decisions']),
        'alignment_failed':sum(not r['source_aligned'] for r in results),'visual_issue_reviews':len(manual),
        'note':'Automated literal-source adjudication is distinguished from explicit image reviews; unresolved roles/glyphs are not forced to correct/error.'}
    save(out/'adjudication-summary.json',summary)
    print(json.dumps(summary,ensure_ascii=True,indent=2))


if __name__=='__main__':run()

"""Replay frozen references with boundary/role constraints; never read gold to parse.

New boundaries use local GROBID, never an old LLM response for a different input.
Evaluation is a separate final pass over the previously reviewed fixed gold.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.prepare_reference_validation import read, save, sha
from scripts.review_reference_validation import field_key
from paper_server.citation_fallback import reference_entry
from paper_server.parsers.references import consensus, grounded, grobid_raw
from paper_server.parsers.reference_segmentation import choose_entries, entry_quality
from paper_server.parsers.reference_evidence import pdf_evidence, bounded_evidence, align_sources
from paper_server.parsers.reference_roles import apply_roles
from paper_server.parsers.reference_normalization import apply_title_normalization, recover_from_pdf
from paper_server.parsers.reference_authority import apply_author_resolution, load_authorities
from paper_server.parsers.reference_pipeline import reconcile_rows
from paper_server.parsers.reference_display import micro_unit_display
from paper_server.parsers.reference_urls import write_url_log

VALIDATION = ROOT / 'outputs/reference_validation_100_20261017'
BASELINE = ROOT / 'outputs/reference_bulk_500_author_fixed_v4_20261017'
DISPLAY_COMPARISON_POLICY = 'Micro unit prefixes μ/µ and u are equivalent; missing prefixes remain errors. Frozen source gold is unchanged.'


def evaluation_field_key(value, field):
    if field == 'title' and isinstance(value, str):
        value = micro_unit_display(value).translate(str.maketrans({'≤':'<=','≥':'>=','≪':'<<','≫':'>>'}))
    return field_key(value, field)


def event(**values):
    print(json.dumps(values, ensure_ascii=True), flush=True)


def finalize(rows, source, paper, evidence):
    return reconcile_rows(rows,bounded=source,evidence=evidence,source_sha256=paper['sha256'])


def compare(out, validation, *, fresh_sample=False):
    frozen = read(validation/'gold-freeze.json')
    for name,digest in frozen['files'].items():
        if sha(validation/name) != digest:
            raise ValueError('Gold/evidence changed after validation')
    manifest = read(out/'manifest.json')
    details, errors, regressions, improvements = [], [], [], []
    references_sampled = 0
    fields = {f:Counter() for f in ('title','author','year','doi')}
    for paper in manifest['papers']:
        gold = read(validation/'gold'/(paper['audit_id']+'.json'))
        references_sampled += len(gold['references'])
        before = {r['number']:r for r in read(validation/'predictions'/(paper['id']+'.json'))['rows']}
        rows = read(out/'papers'/paper['id']/'result.json')['rows']
        actual = {r['number']:r for r in rows}
        paper_errors = []
        for expected in gold['references']:
            for field in fields:
                if field not in expected:
                    continue
                def value(row):
                    return (row.get('title_display',row.get('title')) if field=='title' else row.get(field)) if row else None
                row, old = actual.get(expected['number']), before.get(expected['number'])
                ok = row is not None and evaluation_field_key(value(row),field)==evaluation_field_key(expected[field],field)
                was = old is not None and evaluation_field_key(value(old),field)==evaluation_field_key(expected[field],field)
                fields[field]['checked'] += 1
                fields[field]['errors'] += not ok
                case = {'audit_id':paper['audit_id'],'pdf':paper['pdf'],'number':expected['number'],
                    'field':field,'expected':expected[field],'actual':value(row),'before':value(old),'row_missing':row is None}
                if not ok:
                    errors.append(case);paper_errors.append(case)
                if was and not ok:
                    regressions.append(case)
                if ok and not was:
                    improvements.append(case)
        details.append({'audit_id':paper['audit_id'],'pdf':paper['pdf'],'expected_count':gold['expected_count'],
            'actual_count':len(rows),'count_match':len(rows)==gold['expected_count'],'field_errors':len(paper_errors)})
    checked=sum(f['checked'] for f in fields.values())
    report={'scope':('Fresh source-reviewed sample excluded from earlier development reviews; fixed pipeline, no tuning during evaluation.'
                    if fresh_sample else 'Regression on the same source-reviewed 100, not a new accuracy estimate.'),
        'display_comparison_policy':DISPLAY_COMPARISON_POLICY,
        'papers':len(details),'references_sampled':references_sampled,'fields_checked':checked,'field_errors':len(errors),
        'field_error_rate_percent':round(100*len(errors)/checked,3),'fields':fields,
        'count_exact_papers':sum(p['count_match'] for p in details),
        'papers_with_detected_error':sum(not p['count_match'] or p['field_errors']>0 for p in details),
        'errors':errors,'regressions':regressions,'improvements':improvements,'paper_results':details,
        'gold_freeze_sha256':sha(validation/'gold-freeze.json')}
    save(out/'comparison.json',report)
    event(**{k:v for k,v in report.items() if k not in ('errors','regressions','improvements','paper_results')},
          regressions=len(regressions),improvements=len(improvements))
    return report


def run(out, validation, baseline, *, fresh_sample=False):
    if out.exists():
        raise FileExistsError('Use a new output folder; frozen results are never overwritten')
    out.mkdir(parents=True)
    manifest = read(validation/'manifest.json')
    save(out/'manifest.json',manifest)
    snapshots = {str(path.relative_to(ROOT)):sha(path) for path in (ROOT/'paper_server/parsers').glob('reference*.py')}
    snapshots['scripts/reference_evidence_worker.py']=sha(ROOT/'scripts/reference_evidence_worker.py')
    snapshots['scripts/reference_ocr_worker.py']=sha(ROOT/'scripts/reference_ocr_worker.py')
    snapshots['scripts/reference_ocr.cjs']=sha(ROOT/'scripts/reference_ocr.cjs')
    snapshots['config/reference_authorities.json']=sha(ROOT/'config/reference_authorities.json')
    snapshots['config/reference_source_reviews.json']=sha(ROOT/'config/reference_source_reviews.json')
    save(out/'pipeline-code.json',snapshots)
    prepared=[]; new_entries=[]
    for index,paper in enumerate(manifest['papers'],1):
        original=read(baseline/'papers'/paper['id']/'result.json')
        if sha(baseline/'papers'/paper['id']/'result.json') != paper['prediction_sha256']:
            raise ValueError('Baseline changed')
        evidence=pdf_evidence(ROOT/paper['pdf'],paper['sha256'],ROOT/'.runtime/reference-evidence-cache')
        bounded=bounded_evidence(evidence)
        entries,boundary=choose_entries(original['rows'],bounded)
        changed = entries != original['rows'] and boundary['source']=='bounded_pdf_text'
        item={'paper':paper,'original':original,'entries':entries,'boundary':boundary,
              'bounded':bounded,'evidence':evidence,'changed':changed}
        prepared.append(item)
        if changed:
            item['new_start']=len(new_entries);new_entries.extend(entries)
        event(stage='source',done=index,total=len(manifest['papers']),pdf=paper['pdf'],
              before=len(original['rows']),after=len(entries),changed=changed)
    grobid=[]
    if new_entries:
        signature=hashlib.sha256(json.dumps(new_entries,sort_keys=True).encode()).hexdigest()
        cache=ROOT/'.runtime/reference-replay-grobid'/signature/'parsed.json'
        if cache.exists():
            grobid=read(cache)['rows']
        else:
            grobid,seconds=grobid_raw(new_entries,cache.parent)
            save(cache,{'rows':grobid,'seconds':seconds})
        event(stage='new_boundary_grobid',references=len(grobid))
    for item in prepared:
        paper,original=item['paper'],item['original']
        result=deepcopy(original)
        if item['changed']:
            gs=grobid[item['new_start']:item['new_start']+len(item['entries'])]
            rs=[grounded(e,reference_entry(e['raw']),'regex-v2') for e in item['entries']]
            rows=[consensus(e,r,g) for e,r,g in zip(item['entries'],rs,gs)]
            for row in rows:
                row['llm_status']='not_run_after_boundary_repair'
            result['regex'],result['grobid']=rs,gs
        else:
            rows=original['rows']
        result['rows']=finalize(rows,item['bounded'],paper,item['evidence'])
        result['boundary_resolution']=item['boundary']
        result['ocr']=item['evidence'].get('ocr', {})
        result['status']='extraction_unresolved' if not rows else 'needs_review' if (
            item['boundary']['warnings'] or any(r.get('needs_review') or r.get('normalization_needs_review')
            or item['changed'] and r.get('needs_llm') for r in result['rows'])) else 'complete'
        result['replay_provenance']={'baseline_sha256':paper['prediction_sha256'],'new_llm_calls':0,
            'external_api_calls':0,'sql_writes':0,'boundary_changed':item['changed']}
        write_url_log(result, out/'papers'/paper['id'])
        save(out/'papers'/paper['id']/'result.json',result)
        prep=read(baseline/'papers'/paper['id']/'prepared.json')
        prep.update(entries=item['entries'],boundary_resolution=item['boundary'],ocr=result['ocr'],
                    status='ready' if item['entries'] else 'extraction_unresolved')
        save(out/'papers'/paper['id']/'prepared.json',prep)
    report=compare(out,validation,fresh_sample=fresh_sample)
    save(out/'replay-summary.json',{'status':'complete','finished_at':datetime.now(timezone.utc).isoformat(),
        'papers':len(prepared),'boundary_rebuilt_papers':sum(p['changed'] for p in prepared),
        'new_grobid_references':len(new_entries),'new_llm_calls':0,'external_api_calls':0,'sql_writes':0,
        'field_errors':report['field_errors'],'regressions':len(report['regressions'])})


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--validation',type=Path,default=VALIDATION)
    parser.add_argument('--baseline',type=Path,default=BASELINE)
    parser.add_argument('--fresh-sample',action='store_true')
    args=parser.parse_args()
    run(args.out.resolve(),args.validation.resolve(),args.baseline.resolve(),fresh_sample=args.fresh_sample)

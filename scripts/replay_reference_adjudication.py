"""Offline final-stage replay; all decisions and old results stay separate."""
from pathlib import Path
from collections import Counter
from copy import deepcopy
import json,sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.adjudicate_reference_findings import OUT,BASE,read,save,digest
from paper_server.parsers.reference_evidence import bounded_evidence,authenticated_sources
from paper_server.parsers.reference_pipeline import reconcile_rows
from scripts.classify_reference_findings import display_key


def value(row,field):
    return row.get('title_display',row.get('title')) if field=='title' else row.get(field)


def run():
    out=OUT/'replay';out.mkdir(exist_ok=True)
    snapshot={str(p.relative_to(ROOT)):digest(p) for p in (ROOT/'paper_server/parsers').glob('reference*.py')}
    snapshot['scripts/reference_evidence_worker.py']=digest(ROOT/'scripts/reference_evidence_worker.py')
    save(out/'pipeline-code.json',snapshot)
    changes=[];rows_by_key={};checks=Counter()
    for index,path in enumerate(sorted((OUT/'evidence').glob('*.json')),1):
        proof=read(path);paper=proof['paper'];pid=paper['id']
        old_path=BASE/'papers'/pid/'result.json'
        if digest(old_path)!=proof['result_sha256']:raise ValueError('Baseline result changed')
        old=read(old_path)
        native_path=OUT/'native-full'/(pid+'.json');native=read(native_path)
        if native['source_sha256']!=paper['sha256']:raise ValueError('Source PDF mismatch')
        native.update(evidence_path=str(native_path.resolve()),evidence_sha256=digest(native_path))
        bounded=bounded_evidence(native)
        rows=reconcile_rows(deepcopy(old['rows']),bounded=bounded,evidence=native,source_sha256=paper['sha256'])
        authenticated_sources(rows,paper['sha256'])
        save(out/'papers'/pid/'result.json',{'paper':paper,'rows':rows,'scope':'Final-stage replay of original rows with fresh native PDF evidence, no new upstream parsing/model inference.'})
        for before,after in zip(old['rows'],rows):
            key=pid+':'+str(before['number']);rows_by_key[key]=after
            for field in ('title','author','year','doi'):
                if value(before,field)!=value(after,field):
                    changes.append({'key':key,'field':field,'before':value(before,field),'after':value(after,field),
                        'source_aligned':after.get('source_view',{}).get('extractor')!='original_reference'})
        checks['papers']+=1;checks['rows']+=len(rows)
        if index%100==0:print(json.dumps({'replayed_papers':index}),flush=True)
    decisions=[];potential_regressions=[];case_fixed={};summary=Counter()
    adjudications=read(OUT/'adjudications.json')
    for case in adjudications:
        after=rows_by_key[case['key']];issue_states=[]
        for index,d in enumerate(case['decisions']):
            field=d['field'];v=value(after,field) if field in ('title','author','year','doi') else None
            state='not_scored'
            if d['status']=='confirmed_pipeline_error':
                if field in ('boundary','source'):
                    state='span_logged_and_scoped' if after.get('excluded_source_tail') else 'span_requires_upstream_repair'
                else:
                    expected=d.get('source_expected')
                    null_is_certified=(field=='doi' and d['cause'] in ('host_article_doi_assigned_to_cited_paper','doi_without_literal_source')
                        or field=='author' and ('resource' in d['cause'] or 'organization' in d['cause'])
                        or field=='year' and ('access' in d['cause'] or 'url_path' in d['cause']))
                    if expected is None and not null_is_certified:state='replacement_not_certified'
                    else:state='corrected' if display_key(v)==display_key(expected) else 'still_requires_repair'
                issue_states.append(state);summary[state]+=1
            elif d['status']=='not_pipeline_error' and field in ('title','author','year','doi'):
                if display_key(v)!=display_key(d['before']):
                    potential_regressions.append({'key':case['key'],'issue':index,'field':field,'before':d['before'],'after':v,'source_expected':d.get('source_expected'),'cause':d['cause']})
            decisions.append({'key':case['key'],'issue':index,'field':field,'adjudication':d['status'],'cause':d['cause'],
                'before':d['before'],'source_expected':d.get('source_expected'),'after':v,'repair_status':state})
        if issue_states:case_fixed[case['key']]=all(s=='corrected' for s in issue_states)
    for name,sha in snapshot.items():
        if digest(ROOT/name)!=sha:raise ValueError('Replay code changed during execution')
    report={'scope':'Final-stage source replay, not full-pipeline or independent corpus accuracy',**checks,
        'field_changes':len(changes),'confirmed_issue_repairs':summary,'confirmed_cases_fully_corrected':sum(case_fixed.values()),
        'confirmed_cases':len(case_fixed),'source_consistent_fields_changed_for_review':len(potential_regressions),
        'external_api_calls':0,'sql_writes':0,'new_llm_calls':0}
    save(out/'summary.json',report);save(out/'field-changes.json',changes)
    save(out/'issue-outcomes.json',decisions);save(out/'source-consistent-changes.json',potential_regressions)
    print(json.dumps(report,ensure_ascii=True,indent=2))


if __name__=='__main__':run()

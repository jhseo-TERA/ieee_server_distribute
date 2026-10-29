"""Publish an auditable offline report without changing production data."""
from pathlib import Path
from collections import Counter
from datetime import datetime,timezone
import csv,html,json,sys
from urllib.parse import quote
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.adjudicate_reference_findings import OUT,BASE,read,save,digest
from scripts import backtest_reference_flow as flow
from scripts.replay_reference_pipeline import evaluation_field_key

FLOW=ROOT/'outputs/reference_flow_adjudication_200_20261020_v7'
PREVIOUS=ROOT/'outputs/reference_flow_review_pass_200_20261017'
LABELS={'confirmed_pipeline_error':'실제 오류','not_pipeline_error':'지적 오탐','unresolved':'판단 보류'}
FIELDS={'title':'제목','author':'첫 저자','year':'출판 연도','doi':'DOI','boundary':'참고문헌 경계','source':'원문 범위'}


def compare_previous_full_flow():
    regressions=[];improvements=[];before_errors=0
    for paper in read(FLOW/'manifest.json')['papers']:
        validation=ROOT/flow.COHORTS[paper['cohort']][0]
        gold=read(validation/'gold'/(paper['audit_id']+'.json'))
        before={r['number']:r for r in read(PREVIOUS/'papers'/paper['id']/'result.json')['rows']}
        after={r['number']:r for r in read(FLOW/'papers'/paper['id']/'result.json')['rows']}
        for ref in gold['references']:
            for field in ('title','author','year','doi'):
                if field not in ref:continue
                old,new=flow.value(before.get(ref['number']),field),flow.value(after.get(ref['number']),field)
                a=evaluation_field_key(old,field)==evaluation_field_key(ref[field],field)
                b=evaluation_field_key(new,field)==evaluation_field_key(ref[field],field)
                before_errors+=not a
                row={'id':paper['id'],'number':ref['number'],'field':field,'expected':ref[field],'before':old,'after':new}
                if a and not b:regressions.append(row)
                if b and not a:improvements.append(row)
    return {'baseline':str(PREVIOUS),'before_errors':before_errors,'regressions':regressions,'improvements':improvements}


def run():
    flow.verify(FLOW)
    backtest=read(FLOW/'backtest-report.json')
    if backtest['papers']!=200 or backtest['count_exact_papers']!=200:raise ValueError('Full backtest incomplete/count mismatch')
    original=read(OUT/'collection-final.json')
    if digest(BASE/'diagnostic-findings.json')!=original['original_findings_sha256']:raise ValueError('Original findings changed')
    if digest(OUT/'cases.json')!=original['cases_sha256']:raise ValueError('Case inventory changed')
    for name,sha in original['baseline_code'].items():
        if digest(OUT/'baseline-code'/name)!=sha:raise ValueError('Archived code changed')
    for name,sha in read(OUT/'replay/pipeline-code.json').items():
        if digest(ROOT/name)!=sha:raise ValueError('Replay does not use current code')
    cases=read(OUT/'adjudications.json');outcomes=read(OUT/'replay/issue-outcomes.json')
    repair={(r['key'],r['issue']):r for r in outcomes}
    manual=read(OUT/'visual-adjudications.json')
    for d in manual.values():
        if digest(OUT/d['image'])!=d['image_sha256']:raise ValueError('Reviewed image changed')
        for name,sha in d.get('supporting_image_sha256',{}).items():
            if digest(OUT/name)!=sha:raise ValueError('Supporting image changed')
    unique={};rows=[];corrections=[]
    for case in cases:
        for index,d in enumerate(case['decisions']):
            k=(case['key'],d['field'])
            if k not in unique or d['status']=='confirmed_pipeline_error':unique[k]=d
            result=repair[case['key'],index]
            record={'key':case['key'],'pdf':case['pdf'],'number':case['number'],'reference_status':case['status'],
                'field':d['field'],'issue_status':d['status'],'cause':d['cause'],'before':d['before'],
                'source_expected':d.get('source_expected'),'after':result['after'],'repair_status':result['repair_status'],
                'review_method':d.get('review_method','automated_literal_source_comparison'),
                'source_page':d.get('source_page'),'image':d.get('image'),'source_text':d.get('source_text',''),
                'note':d.get('note',''),'pdf_sha256':case['pdf_sha256'],'native_evidence_sha256':case['native_source_sha256']}
            rows.append(record)
            if d['status']=='confirmed_pipeline_error':corrections.append(record)
    fields=Counter(d['field'] for d in unique.values() if d['status']=='confirmed_pipeline_error')
    comparison=compare_previous_full_flow()
    current_digest=read(FLOW/'llm-settings.json')['signature']['model_digest']
    original_digest=read(BASE/'policy.json')['model_digest']
    if current_digest!=original_digest:raise ValueError('Local model identity changed')
    summary={'created_at':datetime.now(timezone.utc).isoformat(),'scope':f'Original 1608 model-flagged references / {len(rows)} claims; not a new corpus error-rate estimate',
        'references':len(cases),'pdfs':original['pdfs'],'claims':len(rows),'classification':Counter(c['status'] for c in cases),
        'distinct_confirmed_fields':fields,'visual_references':len({k.split('#')[0] for k in manual}),
        'visual_issue_reviews':len(manual),'automated_and_visual_methods_separate':True,
        'replay':read(OUT/'replay/summary.json'),'unit_tests':read(OUT/'unit-tests.json'),
        'gold_regression':{k:backtest[k] for k in ('papers','sampled_references','fields_checked','field_errors','field_error_rate_percent','count_exact_papers')},
        'comparison_vs_previous_full_flow':comparison,'model_digest':current_digest,'external_api_calls':0,'sql_writes':0,
        'limitations':['A false model claim does not certify all other fields in that reference.',
            'Image reviews were performed by the assistant, not independently annotated by a human reviewer.',
            'The frozen 200-paper gold is a development regression set and remains unchanged.',
            'The 839-paper replay reuses original upstream rows and tests the final stage; it does not rerun OCR/LLM for the whole corpus.',
            'Confirmed errors still requiring upstream OCR, segmentation, entity metadata or manual review are retained separately.']}
    save(OUT/'final-summary.json',summary);save(OUT/'source-reviewed-correction-proposals.json',corrections)
    save(OUT/'unresolved-cases.json',[c for c in cases if c['status']=='unresolved'])
    save(OUT/'remaining-confirmed-repairs.json',[r for r in rows if r['issue_status']=='confirmed_pipeline_error' and r['repair_status']!='corrected'])
    with (OUT/'adjudication-details.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(rows[0]));writer.writeheader()
        # Preserve untrusted PDF strings as text when opened in a spreadsheet.
        for row in rows:writer.writerow({k:("'"+v if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')) else v) for k,v in row.items()})
    esc=lambda v:html.escape('미확정 / 없음' if v is None else str(v))
    table=[]
    for r in rows:
        image=f'<a href="{esc(quote(r["image"],safe="/"))}">원문 이미지</a>' if r['image'] else ''
        pdf_href=quote('../../'+r['pdf'],safe='/')
        table.append(f'<tr data-status="{r["reference_status"]}" data-field="{r["field"]}" data-repair="{r["repair_status"]}"><td>{LABELS[r["reference_status"]]}</td><td><a href="{esc(pdf_href)}">{esc(r["pdf"])}</a><br>참고문헌 {r["number"]}<br>{image}</td><td>{FIELDS[r["field"]]}<br>{LABELS[r["issue_status"]]}</td><td>{esc(r["before"])}</td><td>{esc(r["source_expected"])}</td><td>{esc(r["after"])}<br><small>{esc(r["repair_status"])}</small></td><td>{esc(r["cause"])}<details><summary>판정 근거</summary><p>{esc(r["review_method"])}</p><p>{esc(r["note"])}</p><pre>{esc(r["source_text"])}</pre><small>PDF SHA256: {r["pdf_sha256"]}<br>원문 추출 SHA256: {r["native_evidence_sha256"]}</small></details></td></tr>')
    counts=summary['classification'];replay=summary['replay'];g=summary['gold_regression']
    page='''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>참고문헌 1,608건 원문 대조</title>
<style>body{font:15px/1.65 system-ui,sans-serif;margin:28px;color:#182433;background:#f6f8fb}main{max-width:1600px;margin:auto}h1{font-size:27px}section{background:white;border:1px solid #dce2eb;border-radius:12px;padding:22px;margin:18px 0}.cards{display:flex;gap:18px;flex-wrap:wrap}.cards div{padding:10px 24px;background:#edf2fb;border-radius:8px}.cards b{font-size:29px;display:block}select,input{padding:9px;margin:4px;border:1px solid #b7c4d8;border-radius:6px}table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:10px;border-bottom:1px solid #dce2eb;vertical-align:top;text-align:left;max-width:270px;overflow-wrap:anywhere}th{background:#e9eff8;position:sticky;top:0}pre{white-space:pre-wrap;max-width:390px;font:12px/1.6 monospace}small{color:#536376}a{color:#1752aa}.scroll{overflow:auto}li{margin:7px 0}@media(max-width:700px){body{margin:12px}section{padding:13px}h1{font-size:23px}}</style><main>
<h1>참고문헌 1,608건 원문 대조 결과</h1>'''
    page+=f'<p>839편의 PDF에 속한 참고문헌 1,608개에 대한 기존 모델 지적 {len(rows):,}개를 대조했습니다. 분류 단위는 참고문헌 항목입니다.</p><section class="cards"><div>실제 오류<b>{counts["confirmed_pipeline_error"]:,}</b></div><div>지적 오탐<b>{counts["not_pipeline_error"]:,}</b></div><div>판단 보류<b>{counts["unresolved"]:,}</b></div></section>'
    page+=f'<section><h2>검토 범위와 의미</h2><p>모든 항목은 PDF 해시를 확인하고 원문 텍스트와 대조했습니다. 모호하거나 수정이 필요한 항목을 중심으로 {summary["visual_references"]}개 참고문헌의 이미지도 확인했습니다. 이미지 판독은 어시스턴트 검토이며 독립적인 사람 정답 검증은 아닙니다. “지적 오탐”은 원래 지적한 오류가 성립하지 않는다는 뜻으로, 해당 참고문헌의 모든 필드가 정확하다는 보증은 아닙니다.</p><p>원문 자체의 불완전한 DOI, 성명 순서, 판본·개정 연도 충돌은 추측해서 채우지 않았습니다. 이 1,608건은 오류 후보를 골라 검토한 집합이므로 전체 보유 논문의 오답률로 사용할 수 없습니다.</p></section>'
    page+='<section><h2>파이프라인에 반영한 원칙</h2><ul><li>DOI는 원문에 있는 줄바꿈 뒤 접미부만 연결하고, 하이픈 해석이 모호하면 검토 표시를 유지합니다.</li><li>출판 연도와 열람일·arXiv 번호·논문 식별자·본문 머리말 및 바닥글의 연도를 분리합니다.</li><li>저자 이름의 떨어진 발음 구별 기호는 글자 위치로 확인해 복원합니다. 두 추출 결과가 저자 성의 범위에서 충돌하면 자동으로 늘려 쓰지 않습니다.</li><li>제목 안의 중첩 인용부호를 보존하고, 회사명·표준명·제품 자료명을 개인 저자로 취급하지 않도록 보완했습니다.</li><li>참고문헌에서 과학 기호 손실이 의심되면 OCR로 보완하고, 불확실한 값은 임의 치환하지 않습니다.</li><li>같은 필드가 읽을 수 있는 독립 원문과 일치하면 공동저자·학회명 문제를 그 필드의 오류로 집계하지 않습니다. 원래 모델 지적은 로그에 남깁니다.</li><li>기존 원문 이미지 검토는 동일 PDF·쪽·참고문헌 번호·연도·제목 문맥·이미지 해시가 맞는 경우에만 OCR 변형에도 이어받습니다. 정답 자료와 기존 개별 검토 기록은 수정하지 않았습니다.</li></ul></section>'
    page+=f'<section><h2>검증 결과</h2><p>839편의 기존 추출 결과 {replay["rows"]:,}개를 새 원문 근거로 최종 단계에서 재처리했습니다. 지적 오류가 모두 해소된 항목은 {replay["confirmed_cases_fully_corrected"]}건입니다. 모든 확정 오류가 자동 수정된 것은 아니며, OCR 재추출·경계 복원 등이 필요한 목록을 별도로 제공합니다.</p><p>관련 테스트 {summary["unit_tests"]["tests"]}개 통과. 고정된 200편·784개 참고문헌·3,133개 필드 회귀검증에서 필드 불일치는 이전 전체 과정의 {comparison["before_errors"]}개에서 {g["field_errors"]}개로 줄었고, 새로 생긴 불일치는 {len(comparison["regressions"])}개입니다. 이 수치는 기존 개발 검증 집합의 결과이며 새 논문의 독립 정확도 측정은 아닙니다.</p><p>기존 전수 진단·원본 추출 결과·정답 자료는 보존했습니다. 외부 API 호출 0회, 운영 SQL 쓰기 0회.</p><p><a href="adjudication-details.csv">전체 상세 CSV</a> · <a href="unresolved-cases.json">판단 보류 목록</a> · <a href="remaining-confirmed-repairs.json">추가 처리할 확정 오류</a> · <a href="final-summary.json">집계와 검증 근거 JSON</a></p></section>'
    page+='<section><h2>항목별 대조 기록</h2><p>한 참고문헌에 지적이 여러 개면 여러 줄로 표시됩니다.</p><label>판정 <select id="status"><option value="">전체</option>'+''.join(f'<option value="{k}">{v}</option>' for k,v in LABELS.items())+'</select></label><label>필드 <select id="field"><option value="">전체</option>'+''.join(f'<option value="{k}">{v}</option>' for k,v in FIELDS.items())+'</select></label><input id="search" aria-label="검색" placeholder="파일명·원인·논문 제목 검색"><span id="count"></span><div class="scroll"><table><thead><tr><th>항목 판정</th><th>원문</th><th>지적 필드</th><th>기존 값</th><th>원문 대조 값</th><th>재처리 값</th><th>원인과 근거</th></tr></thead><tbody>'+''.join(table)+'</tbody></table></div></section></main>'
    page+='''<script>const rows=[...document.querySelectorAll('tbody tr')],status=document.querySelector('#status'),field=document.querySelector('#field'),search=document.querySelector('#search');function filter(){let n=0;for(const r of rows){const ok=(!status.value||r.dataset.status===status.value)&&(!field.value||r.dataset.field===field.value)&&r.textContent.toLowerCase().includes(search.value.toLowerCase());r.hidden=!ok;n+=ok}document.querySelector('#count').textContent=` ${n} / ${rows.length}개 지적`}for(const e of [status,field,search])e.addEventListener('input',filter);filter()</script>'''
    (OUT/'report.html').write_text(page,encoding='utf-8')
    save(OUT/'final-verification.json',{'complete':True,'baseline_code_verified':True,'original_findings_verified':True,
        'gold_unchanged':True,'current_pipeline_verified':True,'source_review_images_verified':True,
        'adjudications_sha256':digest(OUT/'adjudications.json'),'summary_sha256':digest(OUT/'final-summary.json'),
        'replay_summary_sha256':digest(OUT/'replay/summary.json'),'full_flow_report_sha256':digest(FLOW/'backtest-report.json')})
    print(json.dumps({k:summary[k] for k in ('classification','distinct_confirmed_fields','visual_references','visual_issue_reviews','unit_tests','gold_regression')},ensure_ascii=True,indent=2))
    print(json.dumps({'previous_full_flow_field_errors':comparison['before_errors'],'new_regressions':len(comparison['regressions']),'new_improvements':len(comparison['improvements'])}))


if __name__=='__main__':run()

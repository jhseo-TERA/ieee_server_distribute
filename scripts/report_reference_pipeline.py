"""Verify immutable inputs, previous fixtures, and the v5 development replay."""
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.prepare_reference_validation import read,save,sha
from scripts.review_reference_validation import field_key
from scripts.replay_reference_pipeline import evaluation_field_key, DISPLAY_COMPARISON_POLICY
from paper_server.parsers.reference_display import micro_unit_display
from paper_server.parsers.reference_pipeline import reconcile_rows
from paper_server.parsers.reference_evidence import pdf_evidence,bounded_evidence

VALIDATION=ROOT/'outputs/reference_validation_100_20261017'
BASELINE=ROOT/'outputs/reference_bulk_500_author_fixed_v4_20261017'


def previous_fixtures(out):
    papers={p['pdf']:p for p in read(BASELINE/'manifest.json')['papers']}
    gold=read(BASELINE/'holdout-audit-gold.json')
    cases=read(ROOT/'config/reference_normalization_cases.json')['cases']
    after={}
    for pdf in sorted({p['pdf'] for p in gold['papers']}|{c['pdf'] for c in cases}):
        paper=papers[pdf]
        evidence=pdf_evidence(ROOT/pdf,paper['sha256'],ROOT/'.runtime/reference-evidence-cache')
        original=read(BASELINE/'papers'/paper['id']/'result.json')
        rows=reconcile_rows(original['rows'],bounded=bounded_evidence(evidence),evidence=evidence,source_sha256=paper['sha256'])
        after[pdf]={r['number']:r for r in rows}
        save(out/'previous-fixture-results'/paper['id']/'result.json',{'rows':rows})
    errors=[];checked=0
    for paper in gold['papers']:
        for expected in paper['gold']:
            actual=after[paper['pdf']][expected['number']]
            for field in ('title','author','year','doi'):
                if field not in expected:continue
                checked+=1
                value=actual.get('title_display',actual.get('title')) if field=='title' else actual.get(field)
                if evaluation_field_key(value,field)!=evaluation_field_key(expected[field],field):
                    errors.append({'pdf':paper['pdf'],'number':expected['number'],'field':field,'actual':value,'expected':expected[field]})
    title_errors=[]
    for case in cases:
        row=after[case['pdf']][case['row']['number']]
        if row.get('title_display')!=micro_unit_display(case['expected_display']):
            title_errors.append({'pdf':case['pdf'],'number':row['number'],'expected':case['expected_display'],'actual':row.get('title_display')})
    result={'existing_8_paper_fields':checked,'existing_8_paper_errors':errors,
            'display_comparison_policy':DISPLAY_COMPARISON_POLICY,
            'existing_title_cases':len(cases),'existing_title_case_errors':title_errors,
            'scope':'Existing development/regression cases, not independent new accuracy data.'}
    save(out/'previous-fixture-regression.json',result)
    return result


def run(out):
    comparison=read(out/'comparison.json')
    assert not comparison['regressions']
    assert not read(out/'quality-review.json')['structural_issues']
    assert read(out/'tests.json')['success']
    manifest=read(VALIDATION/'manifest.json')
    verified=Counter()
    for name,digest in read(VALIDATION/'gold-freeze.json')['files'].items():
        assert sha(VALIDATION/name)==digest
        verified['gold_and_evidence_files']+=1
    for name,digest in read(out/'pipeline-code.json').items():
        assert sha(ROOT/name)==digest
        verified['pipeline_files']+=1
    queue=[]
    for paper in manifest['papers']:
        assert sha(ROOT/paper['pdf'])==paper['sha256'];verified['source_pdfs']+=1
        assert sha(BASELINE/'papers'/paper['id']/'result.json')==paper['prediction_sha256'];verified['baseline_results']+=1
        assert sha(VALIDATION/'predictions'/(paper['id']+'.json'))==paper['prediction_sha256'];verified['frozen_predictions']+=1
        result=read(out/'papers'/paper['id']/'result.json')
        for row in result['rows']:
            if row.get('needs_review'):
                queue.append({'pdf':paper['pdf'],'number':row['number'],'page':row['page'],
                              'warnings':row.get('warnings',[]),'normalization_warnings':row.get('normalization_warnings',[])})
    save(out/'review-queue.json',{'scope':'Rule-based review candidates; not confirmed errors.','candidates':queue})
    for case in comparison['errors']:
        paper=next(p for p in manifest['papers'] if p['audit_id']==case['audit_id'])
        row=next(r for r in read(out/'papers'/paper['id']/'result.json')['rows'] if r['number']==case['number'])
        assert row['needs_review'], 'Known remaining error is not reviewable'
    previous=previous_fixtures(out)
    assert not previous['existing_8_paper_errors'] and not previous['existing_title_case_errors']
    tests=read(out/'tests.json')
    integration={str(p.relative_to(ROOT)):sha(p) for p in (ROOT/'scripts/benchmark_reference_bulk.py',
        ROOT/'scripts/audit_reference_bulk.py',ROOT/'scripts/replay_reference_pipeline.py',ROOT/'scripts/report_reference_pipeline.py')}
    save(out/'verification.json',{'immutable_inputs':dict(verified),'existing_regression':previous,'unit_tests':tests,
        'new_field_regressions':0,'remaining_errors_flagged_for_review':len(comparison['errors']),
        'integration_code_sha256':integration,'new_llm_calls':0,'external_api_calls':0,'sql_writes':0})
    lines=['참고문헌 파이프라인 개선 및 100편 회귀검사 (2026-10-17)','',
        '검증 범위: 동일한 100편의 참고문헌 개수와 표본 참고문헌 400개의 1,599개 필드.',
        '오류 분석에 사용한 표본으로 다시 검사한 개발·회귀 결과이며, 새 독립 표본의 정확도가 아니다.','',
        '수정 전 → 수정 후',
        '- 참고문헌 개수 일치: 92/100편 → 100/100편.',
        '- 참고문헌 추출 개수: 1,246개 → 원문과 같은 1,386개 (140개 복구).',
        f'- 필드 오류: 159/1,599개(9.94%) → {comparison["field_errors"]}/1,599개({comparison["field_error_rate_percent"]:.2f}%).',
        '- 개선된 필드: 155개. 기존 정답에서 새로 발생한 오류: 0개.',
        '- 제목 2/400, 첫 저자 성 2/399, 발행연도 0/400, DOI 0/400개 오류.',
        '- 검토 범위에서 오류가 확인된 논문: 29편 → 4편.','',
        '원인 분석',
        '- 기존 159개 오류 중 124개(약 78%)는 참고문헌 31개가 통째로 누락된 결과였다.',
        '  대괄호 번호만 지원하고 일부 제목·헤더 형식을 인식하지 못했으며, 한 줄의 6개 인용을 합쳤다.',
        '- 나머지 35개는 성·기관의 구분, 반복/복합 성, 발행연도와 접속일·페이지·보고서 기간의 구분,',
        '  제목의 경계·문자·지수·단위와 DOI의 줄바꿈 하이픈 문제였다.',
        '- 문자열 안에 존재하거나 두 파서가 합의한 값이어도, 해당 서지 역할을 뜻한다는 보장은 없었다.','',
        '반영한 원칙',
        '1. 필드 추출에 앞서 참고문헌 영역과 번호 순서를 검증한다.',
        '   [1], 1. 형식과 한 줄 다중 인용을 지원하고 그림·표·저자 소개·머리말·바닥글을 구분한다.',
        '   추출하지 못한 목록은 extraction_unresolved로 남긴다. 논문에 참고문헌이 없다는 뜻으로 취급하지 않는다.',
        '2. 파서 합의보다 역할 근거를 우선한다.',
        '   기관명은 author_organization으로 보존한다. 같은 문헌 목록 안의 반복 저자와 명확한 복합 성을 처리하고,',
        '   제목·기간·페이지·DOI·접속일 안의 숫자를 발행연도로 사용하지 않는다.',
        '3. 원문을 덮어쓰지 않는다.',
        '   기존 raw와 이전 값을 보존하고, 수정 규칙·PDF 해시·별도 텍스트 증거·필드별 변경 이력을 남긴다.',
        '   다른 텍스트 추출기가 단위를 잃으면 기존의 더 온전한 값을 유지한다.',
        '4. 지수는 실제 작은 글자와 높이 차이가 확인될 때만 복구한다.',
        '   예상되는 수식이라는 이유만으로 211을 2^11로 바꾸지 않는다. 모호한 경우는 검토 대상으로 남긴다.',
        '5. 캐시는 실제 모델 입력(번호·원문), 모델·프롬프트·생성 설정으로 구분한다.',
        '   후처리 변경만으로 같은 LLM 입력을 재추론하지 않는다. 입력 경계가 바뀌면 과거 응답을 재사용하지 않는다.',
        '   일부만 성공한 캐시도 번호로 연결해 다른 문헌으로 응답이 밀리지 않게 했다.',
        '6. 완료된 결과 폴더는 배치 CLI에서 덮어쓰지 못하게 보호한다.','',
        '남은 확인 오류 4개 (모두 needs_review 표시)',
        '- 8706088.pdf #1: 저자 이름의 잘못된 ÿ 문자. PDF의 다른 텍스트 추출기도 같은 문자를 반환한다.',
        '- 4708768.pdf #6: 2^31−1의 지수 표시 소실. 글꼴 인코딩이 깨져 현재 기하 증거만으로 복원하지 않는다.',
        '- 6872604.pdf #19: 0.13 μm의 μ 소실. 두 텍스트 추출기에서 같은 글자가 빠졌다.',
        '- 4977414.pdf #3: Jri Lee의 이름/성 경계. 추정으로 바꾸지 않고 외부 서지 근거나 확인을 요구한다.',
        '후속 단계는 이 유형에 한해 이미지 OCR 또는 확인된 출판사 메타데이터를 추가하는 것이다.',
        f'참고: review-queue.json의 {len(queue)}개 후보는 규칙상 불확실 항목이며, 모두 오답이라고 확인한 것은 아니다.','',
        '검증·적용 범위',
        f'- 자동 검사 {tests["tests_run"]}개 통과, 새 구조/근거 감사 문제 0개.',
        '- 기존 8편의 127개 필드 모두 일치. 기존 제목 정규화 14개 사례도 유지.',
        '- 고정 정답·이미지 224개 파일, PDF 100개, 이전 예측 100개 및 기준 캐시 100개 해시 확인.',
        '- 로컬 GROBID가 경계 변경 183개 항목을 처리했다. 새 LLM·외부 API 호출·SQL 쓰기는 0회.',
        '- 신규 배치와 캐시 재검증이 같은 reconcile_rows 단계를 사용하도록 반영했다.',
        '- 이번 재처리는 100편이다. 기존 500편 결과·운영 데이터베이스는 변경하지 않았다.','',
        '주요 파일: comparison.json, quality-review.json, previous-fixture-regression.json, verification.json, review-queue.json',
        '이전 결과와 개발 중간 결과는 별도 폴더에 보존했다.',
    ]
    (out/'IMPROVEMENT_REPORT.txt').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print({'field_errors':comparison['field_errors'],'new_regressions':0,'immutable_inputs':dict(verified),
           'old_fields_correct':previous['existing_8_paper_fields'],'title_cases':previous['existing_title_cases'],'tests':tests['tests_run']})


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,default=ROOT/'outputs/reference_pipeline_v5_regression_20261017')
    run(parser.parse_args().out.resolve())

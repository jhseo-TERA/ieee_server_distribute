"""Summarize the fixed second source-review sample without changing predictions."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.prepare_reference_validation import read,save,sha


def percent(errors,total):
    return round(100*errors/total,3) if total else None


def run(validation,out):
    manifest=read(validation/'manifest.json')
    comparison=read(out/'comparison.json')
    frozen=read(validation/'gold-freeze.json')
    policy=read(validation/'evaluation-policy.json')
    checks=Counter()
    assert sha(validation/'manifest.json')==frozen['manifest_sha256']
    assert sha(validation/'gold-freeze.json')==comparison['gold_freeze_sha256']
    for name,digest in frozen['files'].items():
        assert sha(validation/name)==digest,name
        checks['gold_and_review_images']+=1
    for name,digest in policy['source_transcripts'].items():
        assert sha(validation/name)==digest,name
        checks['source_transcripts']+=1
    for name,digest in manifest['pipeline_code_sha256'].items():
        assert sha(ROOT/name)==digest,name
        checks['frozen_pipeline_files']+=1
    for name,digest in read(out/'pipeline-code.json').items():
        assert sha(ROOT/name)==digest,name
    for name,digest in manifest['exclusion_sources'].items():
        assert sha(ROOT/name)==digest,name
    assert sha(Path(manifest['baseline'])/'manifest.json')==manifest['baseline_manifest_sha256']
    for name,digest in policy['evaluation_code'].items():
        if sha(ROOT/name)!=digest:
            change=read(out/'sample-counter-adjudication.json')
            assert name=='scripts/replay_reference_pipeline.py'
            assert digest==change['before_code_sha256'] and sha(ROOT/name)==change['after_code_sha256']
            original=read(out/'first-comparison.json')
            assert original['errors']==comparison['errors'] and original['fields']==comparison['fields']
        checks['evaluation_files']+=1
    assert sha(validation/'source-review-decisions.json')==policy['decisions_sha256']
    previous=read(ROOT/'outputs/reference_validation_100_20261017/manifest.json')
    assert not ({p['sha256'] for p in previous['papers']}&{p['sha256'] for p in manifest['papers']})
    old_validation=ROOT/'outputs/reference_validation_100_20261017'
    for name,digest in read(old_validation/'gold-freeze.json')['files'].items():
        assert sha(old_validation/name)==digest
        checks['previous_sample_gold_and_images']+=1
    papers={p['audit_id']:p for p in manifest['papers']}
    reference_total=predicted_total=baseline_total=baseline_count_exact=0
    unscored=[];zero=[];positive_doi=0;result_rows={}
    for paper in manifest['papers']:
        aid=paper['audit_id'];gold=read(validation/'gold'/(aid+'.json'))
        assert sha(ROOT/paper['pdf'])==paper['sha256']
        assert sha(validation/'predictions'/(paper['id']+'.json'))==paper['prediction_sha256']
        assert sha(Path(manifest['baseline'])/'papers'/paper['id']/'result.json')==paper['prediction_sha256']
        checks['source_pdfs']+=1;checks['baseline_results']+=1;checks['frozen_baseline_copies']+=1
        result=read(out/'papers'/paper['id']/'result.json')
        rows=result['rows'];result_rows[aid]={r['number']:r for r in rows}
        assert [r['number'] for r in rows]==list(range(1,len(rows)+1))
        reference_total+=gold['expected_count'];predicted_total+=len(rows)
        old=read(validation/'predictions'/(paper['id']+'.json'))
        baseline_total+=len(old['rows']);baseline_count_exact+=len(old['rows'])==gold['expected_count']
        if not gold['expected_count']:zero.append({'audit_id':aid,'pdf':paper['pdf'],'reason':gold['review_notes']})
        for r in gold['references']:
            positive_doi+=bool(r.get('doi'))
            for field in ('title','author','year','doi'):
                if field not in r:unscored.append({'audit_id':aid,'number':r['number'],'field':field,'reason':r['unscored_reason']})
    sampled=sum(len(read(validation/'gold'/(p['audit_id']+'.json'))['references']) for p in manifest['papers'])
    assert comparison['references_sampled']==sampled
    errors=comparison['errors'];missing=[e for e in errors if e['row_missing']]
    returned=[e for e in errors if not e['row_missing']]
    keys={(e['audit_id'],e['number']) for e in errors}
    returned_keys={(e['audit_id'],e['number']) for e in returned}
    unflagged=[]
    for aid,n in sorted(returned_keys):
        row=result_rows[aid][n]
        if not (row.get('needs_review') or row.get('normalization_needs_review')):
            unflagged.append({'audit_id':aid,'number':n,'fields':[e['field'] for e in returned if (e['audit_id'],e['number'])==(aid,n)]})
    quality=read(out/'quality-review.json')
    assert not quality['structural_issues']
    tests=read(validation/'unit-tests.json');assert tests['success']
    baseline_errors=len(errors)+len(comparison['improvements'])-len(comparison['regressions'])
    counts=[r for r in comparison['paper_results'] if not r['count_match']]
    families=[{'audit_id':'p037','number':4,'note':'Source citation A. H. Talkhooncheh and Caltech library identify Talkhooncheh; lab publication formats A. Hashemi Talkhooncheh. Retain the strict frozen-source mismatch, flag the family boundary as disputed rather than certify a real-name error.',
        'library':'https://authors.library.caltech.edu/records/0fstm-jw173',
        'lab':'https://www.mics.caltech.edu/a-100-gb-s-pam-4-sip-tx-using-eams-in-mzi-structure/'}]
    report={'created_at':datetime.now(timezone.utc).isoformat(),'scope':comparison['scope'],
        'review_method':'Assistant source text and rendered-image review, draft assisted, not independently human certified.',
        'papers':len(papers),'publisher_quotas':manifest['publisher_quotas'],'previously_reviewed_excluded':len(manifest['excluded_in_500']),
        'source_pdf_overlap_with_previous_100':0,'source_references':reference_total,'predicted_references':predicted_total,
        'count_exact_papers':comparison['count_exact_papers'],'count_mismatches':counts,'zero_reference_documents':zero,
        'sample_references':sampled,'fields_checked':comparison['fields_checked'],'field_mismatches':len(errors),
        'field_mismatch_rate_percent':percent(len(errors),comparison['fields_checked']),
        'sample_references_with_mismatch':len(keys),'sample_reference_mismatch_rate_percent':percent(len(keys),sampled),
        'papers_with_detected_mismatch':comparison['papers_with_detected_error'],
        'fields':{field:{**values,'mismatch_rate_percent':percent(values['errors'],values['checked'])} for field,values in comparison['fields'].items()},
        'missing_sample_references':len({(e['audit_id'],e['number']) for e in missing}),'missing_row_field_mismatches':len(missing),
        'returned_sample_field_mismatches':len(returned),'returned_sample_fields_checked':comparison['fields_checked']-len(missing),
        'returned_sample_field_mismatch_rate_percent':percent(len(returned),comparison['fields_checked']-len(missing)),
        'returned_field_mismatches':dict(Counter(e['field'] for e in returned)),
        'unflagged_returned_references_with_mismatch':unflagged,'unscored_fields':unscored,
        'printed_doi_present_fields':positive_doi,'disputed_name_boundaries':families,
        'baseline_v4':{'references':baseline_total,'count_exact_papers':baseline_count_exact,'field_mismatches':baseline_errors,
            'field_mismatch_rate_percent':percent(baseline_errors,comparison['fields_checked'])},
        'new_mismatches_vs_v4':comparison['regressions'],'improved_fields_vs_v4':len(comparison['improvements']),
        'new_grobid_references':read(out/'replay-summary.json')['new_grobid_references'],
        'pipeline_calls':{'new_local_llm':0,'external_scholarly_api':0,'sql_writes':0},
        'separate_public_web_checks':{'purpose':'One disputed personal family boundary','search_queries':1,'official_pages_opened':2},
        'structural_audit_issues':0,'tests':tests,'integrity':dict(checks),
        'gold_freeze_sha256':sha(validation/'gold-freeze.json'),'limitations':[
            'Counts cover every bibliography; field comparison covers at most four references per document, not all 1323 references.',
            'Three sampled documents have no bibliography; zero counts were reviewed rather than excluded.',
            'Printed source citations may themselves have spelling/date errors; do not silently replace them with outside metadata.',
            'One strict family-boundary mismatch has conflicting official institutional representations; 71 includes it.',
            'Distinct source PDFs can cite the same target papers; this is a source-document holdout, not disjoint cited works.',
            'The earlier development sample had zero remaining mismatches after tuning and curated repairs; it was not a general accuracy estimate.']}
    save(out/'holdout-summary.json',report)
    save(out/'error-review.json',{'scope':'No pipeline correction applied; strict frozen-source mismatches retained.',
        'errors':errors,'returned_error_evidence':[{
            **e,'needs_review':result_rows[e['audit_id']][e['number']].get('needs_review'),
            'warnings':result_rows[e['audit_id']][e['number']].get('warnings',[]),
            'gold_file':str(validation/'gold'/(e['audit_id']+'.json')),
            'source_images':read(validation/'gold'/(e['audit_id']+'.json'))['source_images_viewed']
        } for e in returned]})
    lines=['별도 100편 참고문헌 검토 결과 (2026-10-17)','',
        '기존 500편 중 이전 검토 117편을 제외한 383편에서 출판사 비율에 맞춰 새 100편을 선정했다.',
        'IEEE 88편, Optica 11편, Nature 1편. 이전 100편과 PDF 해시 중복 0건.',
        '원문 텍스트와 렌더링 이미지로 검토 기준을 먼저 기록·고정한 뒤 현재 v7 파이프라인과 비교했다.',
        '평가 중 추출·복구 파이프라인이나 개별 보정 레지스트리는 수정하지 않았다.','',
        '검토 범위',
        f'- 문서 100편의 참고문헌 개수 확인: 원문 {reference_total:,}개, 추출 {predicted_total:,}개.',
        f'- 논문당 처음·1/3·2/3·마지막에서 최대 4개: 표본 참고문헌 {sampled}개.',
        f'- 제목·첫 저자 성·발행연도·원문 DOI {comparison["fields_checked"]:,}개 필드 비교.',
        '- 성 경계 1개, 충돌 연도 1개는 원문으로 확정할 수 없어 점수에서 제외했다.',
        '- 세션 소개 2편과 교육 칼럼 1편에는 참고문헌 목록이 실제로 없다. 실패 표본으로 빼지 않았다.',
        '- 원문 대조를 보조한 AI 검토이며, 사람의 독립 확정 정답은 아니다.','',
        '결과',
        f'- 참고문헌 개수 일치: {comparison["count_exact_papers"]}/100편. 누락 참고문헌 40개.',
        f'- 필드 불일치: {len(errors)}/{comparison["fields_checked"]:,}개 ({report["field_mismatch_rate_percent"]:.2f}%).',
        f'- 표본 참고문헌 중 하나 이상 불일치: {len(keys)}/{sampled}개 ({report["sample_reference_mismatch_rate_percent"]:.2f}%).',
        f'- 검토 범위에서 불일치 발견: {comparison["papers_with_detected_error"]}/100편.',
        f'- 항목 통째 누락에 따른 필드 불일치: {len(missing)}개 (표본 참고문헌 10개).',
        f'- 실제 반환된 항목의 필드 불일치: {len(returned)}/{report["returned_sample_fields_checked"]:,}개 ({report["returned_sample_field_mismatch_rate_percent"]:.2f}%).','',
        '필드별 불일치 (누락 포함)']
    for field,label in [('title','제목'),('author','첫 저자 성'),('year','발행연도'),('doi','원문 DOI')]:
        f=report['fields'][field];lines.append(f'- {label}: {f["errors"]}/{f["checked"]}개 ({f["mismatch_rate_percent"]:.2f}%).')
    lines+=['- 원문 DOI 있음은 23개뿐이다. DOI 없음까지 합친 점수는 외부 DOI 검색 재현율이 아니다.','',
        '참고문헌 누락 원인',
        '- optica-10-7-832.pdf: 텍스트가 없는 PDF. 원문 27개 → 추출 0개.',
        '- OFC-2023-Th2A.2.pdf: 제목·연도·[Online] 표시 없이 URL만 있는 4번 참고문헌 때문에 전체 목록이 거부됨. 9개 → 0개.',
        '- 10347980.pdf: 1쪽 References 3개만 읽고, 3쪽 Additional References 4개를 놓침. 7개 → 3개.','',
        '반환된 필드의 문제와 우선순위',
        '- 제목 11개: 단위·곱셈·깨진 글자 6개, 무인용부호 웹/표준 제목 누락 3개, 제목 경계·잘림 2개.',
        '- 저자 10개: et. al.을 성으로 반환, Gautam을 Gangasani 대신 반환, 기관·표준명을 개인 성으로 분류, URL 파일명의 Zhang을 저자로 추정 등.',
        '- 연도 9개: 접속일을 발행연도로 채우거나, 문헌 뒤 소개글·DOI 연도·표준 개정 연도와 섞여 정상 연도를 삭제함.',
        '- DOI 2개: 10.1109/ 다음 줄 또는 단을 넘어 이어지는 DOI를 끊음.',
        f'- 불일치가 있는 반환 참고문헌 28개 중 {len(unflagged)}개는 needs_review도 켜지지 않았다. 검토 표시만으로 정확도를 보장하지 못한다.',
        '- 우선 이미지 PDF 경로와 여러 위치의 참고문헌 목록 병합을 보완하고, 그 뒤 서지 필드별 근거 범위를 좁히는 것이 좋다.','',
        '성 경계 1건의 해석 주의',
        '- p037 #4: 원문 A. H. Talkhooncheh와 Caltech 도서관 표기는 Talkhooncheh, 연구실은 A. Hashemi Talkhooncheh로 쓴다.',
        '  원문 기준 엄격 비교의 불일치 71개에 포함하되, 실제 개인 성의 오류로 단정하지 않고 별도 기록했다.',
        '- https://authors.library.caltech.edu/records/0fstm-jw173',
        '- https://www.mics.caltech.edu/a-100-gb-s-pam-4-sip-tx-using-eams-in-mzi-structure/','',
        '이전 버전과 비교',
        f'- 동일한 새 표본에서 v4: 개수 {baseline_count_exact}/100편, 필드 {baseline_errors}/{comparison["fields_checked"]}개 불일치 ({report["baseline_v4"]["field_mismatch_rate_percent"]:.2f}%).',
        '- 현재 v7은 181개 필드가 개선됐고, 기존 v4에서 맞았던 8개 필드는 새로 불일치했다.',
        '- 이전 개발용 100편의 오류 0개는 이번 새 100편에서 재현되지 않았다. 새 표본의 결과와 구분해야 한다.','',
        '검증·보존',
        f'- 자동 검사 {tests["tests_run"]}개 통과, 구조·원문 근거 감사 문제 0개. 이는 의미상 불일치 71개가 없다는 뜻이 아니다.',
        '- 고정 정답·검토 이미지 239개, 원본 PDF 100개, 이전 예측·캐시 각각 100개 해시 검증.',
        '- 이전 검토 100편의 정답·이미지 224개도 변경되지 않았음을 확인했다.',
        '- 첫 비교의 표본 개수 메타데이터가 400으로 고정돼 있던 집계 오류를 384로 고쳤다. 71개 불일치·1,534개 분모는 동일하다.',
        '- 최초 비교와 집계 수정 이력은 first-comparison.json, sample-counter-adjudication.json에 보존했다.',
        '- 로컬 GROBID 218개 항목 재파싱. 새 로컬 LLM·외부 학술 API·SQL 쓰기는 0회.',
        '- 성 경계 확인을 위해 공개된 공식 웹 자료 2개를 별도 확인했다. IEEE/OpenAlex 키는 사용하지 않았다.',
        '- 기존 500편 결과와 운영 데이터베이스는 변경하지 않았다.','',
        '파일: holdout-summary.json, comparison.json, error-review.json, quality-review.json',
        '원문·고정 검토 기준: ../reference_validation_fresh100_20261017/']
    (out/'FRESH100_REVIEW_REPORT.txt').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print({k:report[k] for k in ('papers','source_references','predicted_references','sample_references','fields_checked','field_mismatches','field_mismatch_rate_percent','count_exact_papers')})


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--validation',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args();run(args.validation.resolve(),args.out.resolve())

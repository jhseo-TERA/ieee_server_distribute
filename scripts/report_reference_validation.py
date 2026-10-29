"""Summarize a frozen source-review comparison without model or SQL access."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path

from scripts.prepare_reference_validation import DEFAULT, ROOT, read, save, sha


def percentage(errors, checked):
    return round(100 * errors / checked, 3) if checked else None


def summarize(out):
    result = read(out / 'validation-summary.json')
    manifest = read(out / 'manifest.json')
    frozen = read(out / 'gold-freeze.json')
    if sha(out / 'gold-freeze.json') != result['gold_freeze_sha256']:
        raise ValueError('Score and gold freeze differ')
    if sha(out / 'manifest.json') != frozen['manifest_sha256']:
        raise ValueError('Manifest changed')
    for name, digest in frozen['files'].items():
        if sha(out / name) != digest:
            raise ValueError('Reviewed gold/evidence changed')
    checks = {}
    checks['pipeline_files_verified'] = 0
    for name, digest in manifest['pipeline_code_sha256'].items():
        if sha(ROOT / name) != digest:
            raise ValueError('Pipeline code changed')
        checks['pipeline_files_verified'] += 1
    baseline = Path(manifest['baseline'])
    if sha(baseline / 'manifest.json') != manifest['baseline_manifest_sha256']:
        raise ValueError('Baseline manifest changed')
    checks['source_pdfs_verified'] = checks['frozen_predictions_verified'] = checks['baseline_predictions_verified'] = 0
    for paper in manifest['papers']:
        if sha(ROOT / paper['pdf']) != paper['sha256']:
            raise ValueError('Source PDF changed')
        checks['source_pdfs_verified'] += 1
        for path, key in ((out / 'predictions' / (paper['id'] + '.json'), 'frozen_predictions_verified'),
                          (baseline / 'papers' / paper['id'] / 'result.json', 'baseline_predictions_verified')):
            if sha(path) != paper['prediction_sha256']:
                raise ValueError('Prediction changed')
            checks[key] += 1
    errors = result['errors']
    missing_fields = sum(e['row_missing'] for e in errors)
    returned_checked = result['fields_checked'] - missing_fields
    returned_errors = len(errors) - missing_fields
    missing_refs = len({(e['audit_id'], e['number']) for e in errors if e['row_missing']})
    error_refs = len({(e['audit_id'], e['number']) for e in errors})
    publishers = {}
    for publisher in manifest['publisher_quotas']:
        rows = [p for p in result['papers'] if p['publisher'] == publisher]
        checked = sum(p['fields_checked'] for p in rows)
        bad = sum(p['field_errors'] for p in rows)
        publishers[publisher] = {'papers': len(rows), 'count_exact': sum(p['count_match'] for p in rows),
            'empty_extractions': sum(p['actual_count'] == 0 for p in rows),
            'fields_checked': checked, 'field_errors': bad, 'field_error_rate_percent': percentage(bad, checked)}
    details = {'created_at': datetime.now(timezone.utc).isoformat(),
        'summary_sha256': sha(out / 'validation-summary.json'),
        'count_mismatch_papers': [p for p in result['papers'] if not p['count_match']],
        'empty_extraction_papers': sum(p['actual_count'] == 0 for p in result['papers']),
        'missing_sample_references': missing_refs, 'missing_sample_field_errors': missing_fields,
        'returned_sample_references': result['references_field_sample'] - missing_refs,
        'returned_fields_checked': returned_checked, 'returned_field_errors': returned_errors,
        'returned_field_error_rate_percent': percentage(returned_errors, returned_checked),
        'sample_references_with_error': error_refs,
        'sample_reference_error_rate_percent': percentage(error_refs, result['references_field_sample']),
        'publishers': publishers, 'integrity': checks,
        'reviewed_source_images': sum(name.endswith('.png') for name in frozen['files']),
        'initial_field_errors': read(out / 'first-comparison.json')['field_errors'],
        'annotation_corrections': len(read(out / 'gold-adjudications.json')['changes']),
        'evaluation_calls': {'local_llm': 0, 'external_api': 0, 'sql_writes': 0}}
    save(out / 'validation-details.json', details)
    lines = [
        '논문 100편 참고문헌 검증 결과 (2026-10-17)', '',
        '기존 500편 중 앞서 검토한 17편을 제외한 483편에서 출판사 비율에 맞춰 100편을 고정 선정했다.',
        '구성: IEEE 88편, Optica 11편, Nature 1편. 추출 성공 여부로 표본을 걸러내지 않았다.',
        '기존 v4 파이프라인 결과를 사용했고, 이번 평가 중 파이프라인은 수정하지 않았다.', '',
        '검증 범위',
        '- 100편의 원문 참고문헌 개수: 총 1,386개. 추출 결과: 1,246개.',
        '- 각 논문의 처음·1/3·2/3·마지막 위치에서 최대 4개, 총 400개 참고문헌을 필드 대조했다.',
        '- 제목·첫 저자의 성·발행연도·원문에 적힌 DOI: 1,599개 필드. 성 경계가 모호한 1개는 제외했다.',
        '- 참고문헌의 모든 필드를 전수 대조한 것은 아니다. AI 보조 원문 검토이며 사람의 독립 확정 정답은 아니다.', '',
        '주요 결과',
        f'- 참고문헌 개수 일치: {result["count_exact_papers"]}/100편.',
        f'- 검토 범위에서 오류 발견: {result["papers_with_detected_error"]}/100편. 나머지는 해당 범위에서 오류 미발견.',
        f'- 필드 오류: {result["field_errors"]}/{result["fields_checked"]}개 ({result["field_error_rate_percent"]:.2f}%).',
        f'- 표본 참고문헌 중 하나 이상 필드 오류: {error_refs}/400개 ({details["sample_reference_error_rate_percent"]:.2f}%).',
        f'- 항목 누락 때문에 실패한 필드: {missing_fields}개 (표본 참고문헌 {missing_refs}개).',
        f'- 실제 반환된 참고문헌의 필드 오류: {returned_errors}/{returned_checked}개 ({details["returned_field_error_rate_percent"]:.2f}%).', '',
        '필드별 결과 (누락 항목 포함)',
    ]
    labels = {'title': '제목', 'author': '첫 저자 성', 'year': '발행연도', 'doi': 'DOI'}
    for field, label in labels.items():
        item = result['fields'][field]
        lines.append(f'- {label}: {item["errors"]}/{item["checked"]}개 오류 ({item["error_rate_percent"]:.2f}%).')
    doi = result['fields']['doi']
    lines += [f'- 원문 DOI가 있는 21개 중 20개 일치. DOI 없음 379개 중 오류 31개는 모두 항목 누락이다.',
        '  DOI 없음까지 합친 점수를 DOI 검색 재현율로 해석하지 않는다.', '',
        '실패 유형과 후속 우선순위',
        '- 전체 추출 실패 7편: Optica 6편, Nature 1편. 원문에 135개의 참고문헌이 실제 존재한다.',
        '- IEEE 10631489.pdf: 원문 6개가 결과 1개로 합쳐졌고, 그림의 문자열도 제목에 유입됐다.',
        '- 반환된 항목의 오류 35개: 저자 성 16개, 연도 8개, 제목 10개, DOI 1개.',
        '- 저자 성 오류 중 9개는 기관명을 개인의 성 필드에 넣은 스키마 기준 차이다.',
        '  나머지에는 반복 저자 표시, 복합 성의 일부 누락, 이름/성 순서 및 문자 깨짐이 포함된다.',
        '- 제목에는 문헌 경계, 지수·단위·서수 문자 손실이 있고, 연도에는 접속일·보고서 기간의 오인이 있다.',
        '- 우선 번호 형식/배치 지원과 병합·본문 혼입을 보완한 뒤, 성·연도·문자 복원을 다루는 것이 좋다.', '',
        '출판사별 필드 오류 (전체 추출 실패 포함)',
    ]
    for publisher, item in publishers.items():
        lines.append(f'- {publisher}: 개수 일치 {item["count_exact"]}/{item["papers"]}편, '
                     f'필드 오류 {item["field_errors"]}/{item["fields_checked"]}개 ({item["field_error_rate_percent"]:.2f}%).')
    lines += ['', '검증 이력과 보존',
        '- 예측 필드를 열기 전에 PDF 텍스트와 이미지로 정답을 기록하고 고정했다.',
        '- 최초 비교는 163개 오류였다. 원문 재확인으로 정답 초안의 저자 4개 기록 오류를 수정해 159개가 됐다.',
        '  최초 결과 first-comparison.json, 초기 정답 gold-v1/p073.json, 수정 이력 gold-adjudications.json을 보존했다.',
        '- 최종 정답·검토 이미지 224개 파일, 선택 PDF 100개, 예측 결과 100개와 원본 캐시, 파이프라인 6개 파일의 해시를 확인했다.',
        '- 평가 중 로컬 LLM 호출·외부 API 호출·운영 SQL 쓰기: 모두 0회.',
        '- 새 표본이므로 기존 8편 127/127 회귀 점수와 구분해야 한다. 500편 전체의 확정 오답률로 일반화하지 않는다.', '',
        '파일',
        '- manifest.json: 선정 기준과 고정 해시',
        '- gold/: 원문 대조 정답 및 근거 이미지 목록',
        '- sources/: 원문 PDF의 텍스트·이미지',
        '- predictions/: 고정된 v4 결과',
        '- validation-summary.json: 논문·필드별 점수 및 전체 오류 목록',
        '- validation-details.json: 누락/반환 항목 구분, 출판사별 집계, 무결성 검사',
        '- gold-freeze.json / gold-freeze-v1.json: 최종/최초 정답 해시',
    ]
    (out / 'VALIDATION_REPORT.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(json.dumps({k: v for k, v in details.items() if k != 'count_mismatch_papers'}, ensure_ascii=True))
    return details


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=DEFAULT)
    summarize(parser.parse_args().out)

"""Build a reviewable Markdown report from local experiment artifacts."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'outputs/recommendation_quality'


def clean(value):
    return str(value or '').replace('|', '\\|').replace('\n', ' ').replace('[', '\\[').replace(']', '\\]')


def main():
    baseline = json.loads((OUT/'baseline.json').read_text(encoding='utf-8'))
    candidate = json.loads((OUT/'candidate.json').read_text(encoding='utf-8'))
    ablation = json.loads((OUT/'ablation.json').read_text(encoding='utf-8'))
    ai = json.loads((OUT/'ai_preview.json').read_text(encoding='utf-8'))
    regression = json.loads((OUT/'ai_grounding_regression.json').read_text(encoding='utf-8'))
    result, initial_items = ai['result'], ai['modes']['match']
    items = regression['items']
    old_display = baseline['cached_display']
    old_ai = sum('ai_score' in p for p in old_display)
    names = ['기존', '1. 출처 영향 제한', '2. 관련성·중복 검사', '3. 전체 추천 한도']
    share = ablation['source_weight_shares']['DesignCon']
    lines = ['# 즐겨찾기 추천 개선 실험 — 2026-09-15', '',
        '## 결과', '',
        f"논문 {baseline['corpus']['n']:,}편, 즐겨찾기 {len(baseline['favorites']):,}편으로 비교했습니다. "
        f"SQL 전후 입력 동일: {candidate['same_inputs']}. AI 평가 전후 입력 동일: {ai['same_inputs']}.", '',
        '| 단계 | 표시 후보 | 출처 | 제목 프로필 키워드 겹침 2개 이하 |',
        '|---|---:|---:|---:|']
    for name, stage in zip(names, ablation['stages']):
        lines.append(f"| {name} | {stage['items']} | {stage['sources']} | {stage['overlap_lte2']} |")
    lines += [f"| 4. 첫 AI 평가 후(근거 연결 오류 발견) | {len(initial_items)} | {len({p['source_name'] for p in initial_items})} | {sum(p.get('keyword_overlap',0)<=2 for p in initial_items)} |", '',
        f"- DesignCon의 전체 프로필 입력 가중치 비중: {share['before_percent']}% → {share['after_percent']}%.",
        '- 출처 제한만으로는 저겹침 후보가 336→331편으로 소폭 감소했습니다. 앞선 진단의 출처 편향 설명은 단독 주원인으로 단정할 수 없습니다.',
        '- 같은 즐겨찾기 한 편과의 공통 기술 용어·가중 유사도를 검사하고, 거의 같은 제목을 제외했습니다.',
        '- 전체 60편·출처당 20편은 최대값이며 최소 할당이나 미달분 채우기는 없습니다.',
        f"- 모델 평가: 후보 {result['candidate_count']}편 중 {result['attempted_count']}편 시도, {result['evaluated_count']}편 근거 검증 통과, "
        f"{result['qualified_count']}편이 관련성 70점 이상. 검증 거절 {result['rejected_count']}건, 실패 배치 {result['failed_batches']}개.",
        f"- 기존 AI 캐시 표시 {len(old_display)}편 중 AI 평가 {old_ai}편. 새 정책은 AI 평가·인용 검증·70점 이상인 항목만 표시합니다.",
        f"- 실제 AI 실행 시간: {ai['seconds']}초. 새 프로필 생성과 SQL 준비를 포함합니다.", '',
        '## 실추론에서 발견한 추가 오류와 수정', '',
        '첫 AI 결과 10편에서 전기식 die-to-die 논문을 광학 마이크로링과 연결하는 오류를 확인했습니다. '
        '전체 프로필의 표본 즐겨찾기만 모델에 전달하던 것이 원인이었습니다. '
        '각 후보의 가장 가까운 즐겨찾기 ID·제목을 전달하고 그 ID만 근거로 인용하도록 제한했습니다. '
        '검증되지 않은 자유 문장 대신 공통 제목 용어를 추천 이유로 표시합니다.', '',
        f"수정 후 표본 회귀: 이전 통과 10편과 추가 후보 6편, 총 {regression['result']['attempted_count']}편 중 "
        f"{regression['result']['evaluated_count']}편 근거 검증 통과, {len(items)}편 70점 이상. "
        f"실행 {regression['seconds']}초. 통과 항목의 근거 ID가 지정된 즐겨찾기와 일치함을 전부 확인했습니다. "
        '이 검증은 16편 표본이며 최종 코드의 전체 64편 재평가는 아닙니다.', '',
        '## 검증', '',
        '- Python 추천/캐시/권한/피드백 테스트: 53개 통과.',
        '- 실제 프런트엔드 핸들러 테스트: 10개 통과.',
        '- MySQL 세션 전용 임시 테이블에서 제외·검토 완료·복원과 후보 보충 확인.',
        '- 기존 즐겨찾기·인용정보 보존 및 AI 작업 미생성 확인.',
        '- 로컬 실측: 캐시 조회 중앙값 68.2ms, 첫 조회 72.05ms, 변경 직후 캐시 조회 13.26ms.',
        '- 후보 재계산 약 16~17초(측정 조건에 따라 변동). 요청을 기다리게 하지 않는 백그라운드 처리 유지.', '',
        '## 적용 범위와 해석', '',
        '코드와 테스트 구현은 완료했습니다. 이 실험은 운영 서버 재시작이나 영구 피드백 테이블 마이그레이션을 수행하지 않았습니다. '
        '실험 AI 결과는 이 폴더에만 저장하고 운영 AI 캐시에는 게시하지 않았습니다. '
        '운영 적용에는 011_recommendation_feedback.sql 적용과 서버 재시작, 새 정책 AI 갱신이 필요합니다.', '',
        '70점은 모델의 주관적 관련성 기준이며 정답 확률이 아닙니다. 제목 키워드 겹침 개선과 AI 평가 비율은 '
        '추천 과정의 검증 지표이며, 실제 즐겨찾기 등록률 향상은 사용자의 검토로 확인해야 합니다. '
        '제목만으로 학회판·저널판의 내용 중복을 완전히 구별할 수는 없습니다. '
        '일반 후보 목록은 단어가 다른 관련 논문을 놓칠 수 있어 실제 피드백에 따라 100편·0.25·70점 기준을 보정해야 합니다.', '',
        '## 검토용 실제 추천 목록', '',
        '아래는 최종 근거 연결 회귀 검증에서 통과한 표본 논문입니다. 전체 64편의 최종 결과는 아닙니다. '
        '즐겨찾기 등록이나 제외 기록은 자동으로 수행하지 않았습니다.', '']
    for index, paper in enumerate(items, 1):
        lines += [f"### {index}. {clean(paper['title'])}", '',
            f"{paper['source_name']} · {paper.get('year','')} · AI {paper['ai_score']} · {clean(paper.get('ai_topic'))}", '',
            f"{clean(paper.get('ai_reason'))}", '',
            f"관련 즐겨찾기: {clean(paper.get('ai_favorite_title'))}", '',
            f"후보 제목/초록 근거: {clean(paper.get('ai_evidence'))}", '',
            f"논문 식별자: `{paper['article_number']}`", '']
    target = OUT/'RECOMMENDATION_QUALITY_REPORT.md'
    target.write_text('\n'.join(lines), encoding='utf-8')
    print(target)


if __name__ == '__main__':
    main()

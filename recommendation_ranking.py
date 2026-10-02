"""Compact wire format; paper identity and evidence still require validation."""
from copy import deepcopy

FORMAT_VERSION = 'compact-rank-1'


def compact_request(body):
    """Short aliases are request-local, never stable paper or favorite identities."""
    candidates = {f'p{i}': row['id'] for i, row in enumerate(body['candidates'])}
    favorites = {f'f{i}': row['id'] for i, row in enumerate(body['favorites'])}
    topics = {f't{i}': topic['name'] for i, topic in enumerate(body['profile']['topics'])}
    reverse_f = {value: key for key, value in favorites.items()}
    result = deepcopy(body)
    for alias, row in zip(candidates, result['candidates']):
        row['id'] = alias
        row['related_favorite_id'] = reverse_f.get(row.get('related_favorite_id'), row.get('related_favorite_id'))
    for alias, row in zip(favorites, result['favorites']):
        row['id'] = alias
    for alias, topic in zip(topics, result['profile']['topics']):
        topic['topic_id'] = alias
        topic['favorite_ids'] = [reverse_f.get(key, key) for key in topic.get('favorite_ids', [])]
    result['task'] = (
        '각 후보를 같은 절대 기준으로 평가하세요. i=후보 id, s=관심사 관련성(0~100), '
        'n=관련은 있지만 수집이 적은 방향(0~100), t=프로필 topic_id, '
        'f=후보의 related_favorite_id, e=후보 제목/초록의 연속 원문 4~70자입니다. '
        '점수 기준: 동일 연구 문제/회로 기능 80~100, 구체적 기술 응용 65~79, 인접 기술 40~64, '
        '일반 용어만 공유 0~39. CMOS, power, optical 같은 일반 용어만 공유하면 60점 미만입니다. '
        '전체 관심 프로필 및 지정된 개별 즐겨찾기와 직접 비교하세요. '
        'Electrical die-to-die와 optical microring을 동일 기술로 간주하지 마세요. '
        '제목만 있을 때 성능을 추정하지 마세요. 모든 후보를 정확히 한 번씩 평가하세요. '
        '낮은 점수나 0점 항목도 e에는 실제 제목 구절을 넣으세요. 빈 값, N/A, /n/0 같은 대체 문자열은 금지합니다. '
        '설명 문장은 생성하지 말고 r 배열만 반환하세요.'
    )
    fields = {'i': {'type': 'string', 'enum': list(candidates)},
              's': {'type': 'integer', 'minimum': 0, 'maximum': 100},
              'n': {'type': 'integer', 'minimum': 0, 'maximum': 100},
              't': {'type': 'string', 'enum': list(topics)},
              'f': {'type': 'string', 'enum': list(favorites)},
              'e': {'type': 'string', 'minLength': 4, 'maxLength': 100}}
    schema = {'type': 'object', 'properties': {'r': {'type': 'array',
              'minItems': len(candidates), 'maxItems': len(candidates),
              'items': {'type': 'object', 'properties': fields, 'required': list(fields),
                        'additionalProperties': False}}}, 'required': ['r'], 'additionalProperties': False}
    return result, schema, (candidates, favorites, topics)


def expand_ranks(answer, aliases):
    candidates, favorites, topics = aliases
    return {'rankings': [dict(id=candidates.get(row.get('i')), score=row.get('s'), novelty=row.get('n'),
                             topic=topics.get(row.get('t')), favorite_id=favorites.get(row.get('f')),
                             evidence=row.get('e'), reason='지정된 즐겨찾기와의 기술적 관련성 평가')
                         for row in answer.get('r', [])]}

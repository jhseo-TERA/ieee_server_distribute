"""Validated technical pair scores, independent of the changing user profile."""
from collections import Counter
from datetime import datetime, timedelta
import hashlib
import json

from sqlalchemy import bindparam, text
from sqlalchemy.exc import ProgrammingError
from recommendation_policy import INTEREST_INSTRUCTIONS

PAIR_VERSION = 'technical-pair-1'
CACHE_DAYS = 30


def pair_input(row, favorite):
    # Exactly the technical text the model sees. No profile, rank, or coverage.
    return {'paper_id': str(row['article_number']), 'title': str(row['title'])[:350],
            'abstract': str(row.get('abstract') or '')[:550],
            'favorite_id': str(favorite['article_number']), 'favorite_title': str(favorite['title'])[:350]}


def pair_key(pair, model, digest):
    body = [PAIR_VERSION, INTEREST_INSTRUCTIONS, model, digest, pair]
    return hashlib.sha256(json.dumps(body,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


class PairCache:
    def __init__(self, engine):
        self.engine = engine

    def read(self, keys):
        if not keys:
            return {}
        stmt = text('SELECT cache_key,payload_json FROM ai_recommendation_pair_cache '
                    'WHERE cache_key IN :keys AND expires_at>:now').bindparams(bindparam('keys',expanding=True))
        try:
            with self.engine.connect() as conn:
                rows = conn.execute(stmt,{'keys':list(keys),'now':datetime.now()}).mappings()
                return {r['cache_key']:json.loads(r['payload_json']) if isinstance(r['payload_json'],str)
                        else r['payload_json'] for r in rows}
        except ProgrammingError as exc:
            if getattr(exc.orig,'args',(None,))[0] != 1146:
                raise
            return {}

    def write(self, values):
        if not values:
            return
        # Only values already checked against the actual candidate and favorite
        # reach this method. Failed/missing responses are never negative scores.
        with self.engine.begin() as conn:
            for key,payload in values.items():
                conn.execute(text('INSERT INTO ai_recommendation_pair_cache '
                    '(cache_key,payload_json,expires_at) VALUES (:key,:payload,:expires) '
                    'ON DUPLICATE KEY UPDATE payload_json=VALUES(payload_json),expires_at=VALUES(expires_at)'),
                    {'key':key,'payload':json.dumps(payload,ensure_ascii=False),
                     'expires':datetime.now()+timedelta(days=CACHE_DAYS)})


class MemoryPairCache:
    def __init__(self):
        self.values={}

    def read(self,keys):
        return {k:self.values[k] for k in keys if k in self.values}

    def write(self,values):
        self.values.update(values)


def pair_request(rows, favorites):
    pairs=[pair_input(row,favorites[str(row['related_favorite_id'])]) for row in rows]
    aliases={f'p{i}':pair for i,pair in enumerate(pairs)}
    body={'pairs':[{'i':alias, 'f':f'f{i}', 'paper':{'title':pair['title'],'abstract':pair['abstract']},
                    'favorite':{'title':pair['favorite_title']}}
                   for i,(alias,pair) in enumerate(aliases.items())],
          'task': '각 paper와 그 항목에 지정된 favorite만 독립적으로 비교하세요. 다른 항목이나 수집량은 점수에 영향을 주지 않습니다. '
                  's=기술적 관련성(0~100). 동일 연구 문제/회로 기능에 직접 도움 80~100, 같은 구체적 기술 응용 65~79, '
                  '인접 기술 40~64, 일반 용어만 공유 0~39. CMOS, power, optical 같은 일반 용어만 공유하면 60점 미만입니다. '
                  'Electrical die-to-die와 optical microring을 동일 방식으로 간주하지 마세요. '
                  'i와 f는 각 항목에 지정된 값을 그대로 반환하고, e는 paper 제목/초록의 연속 원문 4~70자입니다. '
                  '0점이나 낮은 점수여도 e에는 실제 제목 구절을 넣으세요. N/A, /n/0 같은 대체 문자열은 금지합니다. '
                  '모든 항목을 정확히 한 번 포함하고 설명 문장은 생성하지 마세요.'}
    fields={'i':{'type':'string','enum':list(aliases)},'f':{'type':'string','enum':[f'f{i}' for i in range(len(rows))]},
            's':{'type':'integer','minimum':0,'maximum':100},'e':{'type':'string','minLength':4,'maxLength':100}}
    schema={'type':'object','properties':{'r':{'type':'array','minItems':len(rows),'maxItems':len(rows),
             'items':{'type':'object','properties':fields,'required':list(fields),'additionalProperties':False}}},
            'required':['r'],'additionalProperties':False}
    return body,schema,aliases


def validated_pairs(answer, rows, favorites):
    from local_ai_recommendations import validated_ranks
    ranks=[]
    for record in answer.get('r',[]):
        alias=record.get('i','')
        try:
            index=int(alias[1:]) if alias.startswith('p') and alias[1:].isdigit() else -1
            row=rows[index] if 0<=index<len(rows) and alias==f'p{index}' else None
        except (ValueError,TypeError):
            row=None
        ranks.append({'id':str(row['article_number']) if row else None,
                      'favorite_id':str(row['related_favorite_id']) if row and record.get('f')==f'f{index}' else None,
                      'topic':'pair','score':record.get('s'),'novelty':0,'evidence':record.get('e'),
                      'reason':'지정된 즐겨찾기와의 기술적 관련성 평가'})
    profile={'topics':[{'name':'pair','favorite_ids':list(favorites)}]}
    supplied=[{**row,'title':str(row['title'])[:350],'abstract':str(row.get('abstract') or '')[:550]} for row in rows]
    return validated_ranks({'rankings':ranks},supplied,profile,list(favorites.values()))


def contextualize(ratings, rows, favorites, profile, tokenize):
    """Recompute topic and collection coverage; never cache these dynamic fields."""
    topics=profile.get('topics',[])
    topic_terms=[set(tokenize(' '.join([t['name'],*t.get('keywords',[])]))) for t in topics]
    def topic_for(paper):
        words=set(tokenize(paper.get('title','')))
        scores=[len(words&terms)+ (10 if str(paper.get('article_number')) in t.get('favorite_ids',[]) else 0)
                for t,terms in zip(topics,topic_terms)]
        return topics[max(range(len(scores)),key=lambda i:scores[i])]['name'] if scores and max(scores)>0 else '기타 관련 연구'
    coverage=Counter(topic_for(p) for p in favorites)
    # Unclassified titles are not evidence that every known topic is novel.
    # Compare known topics only; unknown candidates receive no novelty bonus.
    known_topics={t['name'] for t in topics}
    most=max((coverage[name] for name in known_topics),default=0) or 1
    def novelty(name):
        return round(100*(1-coverage[name]/most)) if name in known_topics else 0
    row_map={str(row['article_number']):row for row in rows}
    return {key:{**rating,'ai_topic':topic_for(row_map[key]),
                 'ai_novelty':novelty(topic_for(row_map[key])),
                 'novelty_basis':'current_favorite_coverage'} for key,rating in ratings.items()}


class PairRanker:
    def __init__(self, worker, cache):
        self.worker, self.cache = worker, cache

    def run(self, rows, favorites, request, event, digest='', progress=lambda message:None, retry_missing=True):
        from local_ai_recommendations import SYSTEM, dump
        ratings, keys = {}, {}
        for row in rows:
            anchor=favorites.get(str(row.get('related_favorite_id')))
            if anchor and digest:
                keys[str(row['article_number'])]=pair_key(pair_input(row,anchor),request['model'],digest)
        cached=self.cache.read(keys.values()) if keys else {}
        for row in rows:
            value=cached.get(keys.get(str(row['article_number'])))
            if not isinstance(value,dict):
                continue
            answer={'r':[{'i':'p0','f':'f0' if value.get('favorite_id')==str(row.get('related_favorite_id')) else '',
                          's':value.get('score'),'e':value.get('evidence')}]}
            valid,_=validated_pairs(answer,[row],favorites)
            ratings.update(valid)
        cached_count=len(ratings)
        missing=[row for row in rows if str(row['article_number']) not in ratings
                 and str(row.get('related_favorite_id')) in favorites]
        usage,failures,rejected=[],0,0
        for start in range(0,len(missing),8):
            if event.is_set():
                raise ValueError('추천 갱신이 취소되었습니다.')
            progress(f'평가 재사용 {cached_count}편 · 신규 평가 {start}/{len(missing)}편')
            batch=missing[start:start+8]
            body,schema,_=pair_request(batch,favorites)
            try:
                answer,stats=self.worker._chat(request,[{'role':'system','content':SYSTEM},
                                                        {'role':'user','content':dump(body)}],schema,event)
                valid,bad=validated_pairs(answer,batch,favorites)
                rejected+=bad
                usage.append(stats)
            except (RuntimeError,ValueError):
                if event.is_set():
                    raise
                failures+=1
                if failures>=2 and not ratings:
                    raise ValueError('모델이 평가를 완료하지 못했습니다. 기존 추천 목록을 유지합니다.')
                continue
            ratings.update(valid)
            if digest and not event.is_set():
                self.cache.write({keys[key]:{'score':r['ai_score'],'evidence':r['ai_evidence'],
                                            'favorite_id':r['ai_favorite_id']} for key,r in valid.items()})
        retried_count=0
        unresolved=[row for row in missing if str(row['article_number']) not in ratings]
        if retry_missing and unresolved and not failures:
            progress(f'검증 미완료 {len(unresolved)}편만 한 번 재평가합니다.')
            retried,extra=self.run(unresolved,favorites,request,event,digest,progress,retry_missing=False)
            ratings.update(retried)
            usage.extend(extra['usage'])
            rejected+=extra['rejected_count']
            failures+=extra['failed_batches']
            retried_count=extra['new_attempted_count']
        if event.is_set():
            raise ValueError('추천 갱신이 취소되었습니다.')
        return ratings,{'cached_count':cached_count,'new_attempted_count':len(missing),
                        'new_evaluated_count':len(ratings)-cached_count,'evaluated_count':len(ratings),
                        'missing_count':len(rows)-len(ratings),'rejected_count':rejected,
                        'failed_batches':failures,'usage':usage,'retried_count':retried_count}

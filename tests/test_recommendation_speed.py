from copy import deepcopy
import unittest
from jsonschema import validate, ValidationError
from recommendation_ranking import compact_request, expand_ranks
from local_ai_recommendations import validated_ranks
from recommendation_pair_cache import (pair_input, pair_key, pair_request, validated_pairs,
                                       contextualize, MemoryPairCache, PairRanker, PairCache)
from unittest.mock import Mock, patch
import json
import threading
from sqlalchemy import create_engine, text, event
from local_ai_recommendations import RecommendationService, AUTO_DELAY_SECONDS


class CompactTests(unittest.TestCase):
    def setUp(self):
        self.body = {'profile': {'topics': [{'name':'RX','favorite_ids':['f']} ]},
                     'favorites':[{'id':'f','title':'PAM4 receiver'}],
                     'candidates':[{'id':'p','title':'PAM4 receiver with DFE','abstract':'',
                                    'related_favorite_id':'f','shared_terms':['pam','receiver']}],
                     'task':'legacy'}

    def test_aliases_round_trip_and_input_is_unchanged(self):
        before=deepcopy(self.body)
        body,schema,aliases=compact_request(self.body)
        answer={'r':[{'i':'p0','s':85,'n':20,'t':'t0','f':'f0','e':'receiver with DFE'}]}
        validate(answer,schema)
        rows=[{**self.body['candidates'][0],'article_number':'p'}]
        seeds=[{'article_number':'f','title':'PAM4 receiver'}]
        ratings,rejected=validated_ranks(expand_ranks(answer,aliases),rows,self.body['profile'],seeds)
        self.assertEqual(ratings['p']['ai_favorite_id'],'f')
        self.assertEqual(ratings['p']['ai_score'],85)
        self.assertEqual(rejected,0)
        self.assertEqual(before,self.body)
        self.assertNotIn('reason',schema['properties']['r']['items']['properties'])

    def test_missing_rows_unknown_ids_and_fabricated_evidence_are_rejected(self):
        _,schema,aliases=compact_request(self.body)
        with self.assertRaises(ValidationError):validate({'r':[]},schema)
        answer={'r':[{'i':'unknown','s':85,'n':20,'t':'t0','f':'f0','e':'receiver'}]}
        with self.assertRaises(ValidationError):validate(answer,schema)
        answer['r'][0].update(i='p0',e='not present in supplied paper')
        rows=[{**self.body['candidates'][0],'article_number':'p'}]
        ratings,rejected=validated_ranks(expand_ranks(answer,aliases),rows,self.body['profile'],
                                        [{'article_number':'f','title':'PAM4 receiver'}])
        self.assertEqual(ratings,{})
        self.assertEqual(rejected,1)


class PairTests(unittest.TestCase):
    def setUp(self):
        self.favorite={'article_number':'f','title':'PAM4 receiver'}
        self.row={'article_number':'p','title':'PAM4 receiver with DFE','abstract':'Measured receiver',
                  'related_favorite_id':'f','shared_terms':['receiver','pam']}

    def test_cache_key_tracks_every_technical_input_and_model_revision(self):
        pair=pair_input(self.row,self.favorite)
        key=pair_key(pair,'model','digest')
        for field in pair:
            changed={**pair,field:pair[field]+' changed'}
            self.assertNotEqual(key,pair_key(changed,'model','digest'),field)
        self.assertNotEqual(key,pair_key(pair,'other','digest'))
        self.assertNotEqual(key,pair_key(pair,'model','other'))
        self.assertEqual(key,pair_key(pair_input({**self.row,'ai_novelty':99,'year':2025},self.favorite),'model','digest'))

    def test_low_scores_are_valid_but_missing_and_wrong_anchor_are_not(self):
        rows=[self.row]
        favorites={'f':self.favorite}
        body,schema,aliases=pair_request(rows,favorites)
        self.assertNotIn('profile',body)
        answer={'r':[{'i':'p0','f':'f0','s':25,'e':'receiver with DFE'}]}
        validate(answer,schema)
        ratings,rejected=validated_pairs(answer,rows,favorites)
        self.assertEqual(ratings['p']['ai_score'],25)
        self.assertEqual(rejected,0)
        self.assertEqual(validated_pairs({'r':[]},rows,favorites)[0],{})
        answer['r'][0]['f']='f1'
        self.assertEqual(validated_pairs(answer,rows,favorites)[0],{})
        answer['r'][0].update(f='f0',e='invented measurement')
        self.assertEqual(validated_pairs(answer,rows,favorites)[0],{})

    def test_coverage_is_recomputed_without_mutating_cached_technical_score(self):
        ratings={'p':{'ai_score':85,'ai_evidence':'receiver'}}
        before=deepcopy(ratings)
        profile={'topics':[{'name':'receiver','keywords':['receiver']},{'name':'optical','keywords':['optical']}]}
        tokenize=lambda value:value.lower().split()
        a=contextualize(ratings,[self.row],[self.favorite],profile,tokenize)
        b=contextualize(ratings,[self.row],[self.favorite,*[{'title':'optical'} for _ in range(9)]],profile,tokenize)
        self.assertEqual(a['p']['ai_score'],b['p']['ai_score'])
        self.assertNotEqual(a['p']['ai_novelty'],b['p']['ai_novelty'])
        self.assertEqual(ratings,before)

    def test_identical_pairs_reuse_low_scores_and_only_changed_text_is_reevaluated(self):
        worker=Mock()
        def chat(req,messages,schema,event):
            body=json.loads(messages[-1]['content'])
            return {'r':[{'i':p['i'],'f':p['f'],'s':25,'e':'receiver'} for p in body['pairs']]},{}
        worker._chat.side_effect=chat
        cache=MemoryPairCache()
        ranker=PairRanker(worker,cache)
        rows=[self.row,{**self.row,'article_number':'p2'}]
        req={'model':'model'}
        first,stats=ranker.run(rows,{'f':self.favorite},req,threading.Event(),'digest')
        self.assertEqual(stats['new_evaluated_count'],2)
        self.assertEqual(first['p']['ai_score'],25)
        second,stats=ranker.run(rows,{'f':self.favorite},req,threading.Event(),'digest')
        self.assertEqual(stats['cached_count'],2)
        self.assertEqual(worker._chat.call_count,1)
        self.assertEqual(first,second)
        rows[1]['abstract']='updated receiver'
        _,stats=ranker.run(rows,{'f':self.favorite},req,threading.Event(),'digest')
        self.assertEqual(stats['cached_count'],1)
        self.assertEqual(stats['new_attempted_count'],1)
        self.assertEqual(worker._chat.call_count,2)

    def test_unclassified_favorites_do_not_inflate_novelty(self):
        profile={'topics':[{'name':'receiver','keywords':['receiver']},
                           {'name':'optical','keywords':['optical']}]}
        rows=[{'article_number':'rx','title':'receiver'}, {'article_number':'op','title':'optical'},
              {'article_number':'unknown','title':'unclassified'}]
        favorites=[*({'title':'receiver'} for _ in range(9)), {'title':'optical'},
                   *({'title':'unclassified'} for _ in range(100))]
        scores=contextualize({r['article_number']:{'ai_score':85} for r in rows},rows,favorites,
                             profile,lambda value:value.lower().split())
        self.assertEqual(scores['rx']['ai_novelty'],0)
        self.assertEqual(scores['op']['ai_novelty'],89)
        self.assertEqual(scores['unknown']['ai_novelty'],0)

    def test_missing_digest_does_not_reuse_or_write_cache(self):
        worker=Mock()
        worker._chat.return_value=({'r':[]},{})
        cache=Mock()
        _,stats=PairRanker(worker,cache).run([self.row],{'f':self.favorite},{'model':'model'},threading.Event())
        cache.read.assert_not_called()
        cache.write.assert_not_called()
        self.assertEqual(stats['missing_count'],1)

    def test_retry_only_missing_pair_and_preserve_valid_first_score(self):
        worker=Mock();cache=MemoryPairCache()
        rows=[self.row,{**self.row,'article_number':'second'}]
        worker._chat.side_effect=[({'r':[{'i':'p0','f':'f0','s':85,'e':'receiver'},
                                        {'i':'p1','f':'f1','s':0,'e':'/n/0'}]},{}),
                                  ({'r':[{'i':'p0','f':'f0','s':25,'e':'receiver'}]}, {})]
        ratings,stats=PairRanker(worker,cache).run(rows,{'f':self.favorite},{'model':'model'},
                                                 threading.Event(),'digest')
        self.assertEqual(ratings['p']['ai_score'],85)
        self.assertEqual(ratings['second']['ai_score'],25)
        self.assertEqual(stats['retried_count'],1)
        self.assertEqual(stats['missing_count'],0)
        self.assertEqual(len(cache.values),2)
        retry_body=json.loads(worker._chat.call_args_list[1].args[1][-1]['content'])
        self.assertEqual(len(retry_body['pairs']),1)

    def test_bad_cached_evidence_is_reevaluated_and_cancelled_run_never_writes(self):
        worker=Mock();cache=MemoryPairCache()
        key=pair_key(pair_input(self.row,self.favorite),'model','digest')
        cache.write({key:{'score':99,'evidence':'fabricated','favorite_id':'f'}})
        worker._chat.return_value=({'r':[]},{})
        _,stats=PairRanker(worker,cache).run([self.row],{'f':self.favorite},{'model':'model'},threading.Event(),'digest')
        self.assertEqual(stats['cached_count'],0)
        self.assertEqual(worker._chat.call_count,2)
        self.assertEqual(stats['retried_count'],1)
        cancelled=threading.Event();cancelled.set()
        with self.assertRaises(ValueError):
            PairRanker(worker,cache).run([self.row],{'f':self.favorite},{'model':'model'},cancelled,'digest')

    def test_sql_cache_upsert_and_expiry(self):
        engine=create_engine('sqlite://')
        self.addCleanup(engine.dispose)
        @event.listens_for(engine,'before_cursor_execute',retval=True)
        def adapt(conn,cursor,statement,params,context,many):
            return statement.replace('ON DUPLICATE KEY UPDATE payload_json=VALUES(payload_json),expires_at=VALUES(expires_at)',
                                     'ON CONFLICT(cache_key) DO UPDATE SET payload_json=excluded.payload_json,expires_at=excluded.expires_at'),params
        with engine.begin() as conn:
            conn.execute(text('CREATE TABLE ai_recommendation_pair_cache(cache_key TEXT PRIMARY KEY,payload_json TEXT,expires_at DATETIME)'))
        store=PairCache(engine)
        store.write({'a':{'score':25}})
        self.assertEqual(store.read(['a'])['a']['score'],25)
        store.write({'a':{'score':80}})
        self.assertEqual(store.read(['a'])['a']['score'],80)
        with engine.begin() as conn:
            conn.execute(text("UPDATE ai_recommendation_pair_cache SET expires_at='2000-01-01'"))
        self.assertEqual(store.read(['a']),{})


class PipelineTests(unittest.TestCase):
    def test_compact_pipeline_retries_only_invalid_rows_without_pair_cache(self):
        engine,worker=Mock(),Mock()
        service=RecommendationService(engine,worker,Mock())
        self.addCleanup(service.close)
        service.pair_cache=Mock()
        favorite={'article_number':'f','title':'PAM4 receiver','source_name':'JSSC'}
        service.snapshot=Mock(return_value={'signature':'sig','favorites':[favorite]})
        service.profile=Mock(return_value={'topics':[{'name':'RX','keywords':['receiver'],'favorite_ids':['f']}]})
        row={'id':1,'article_number':'p','title':'PAM4 receiver with DFE','abstract':'',
             'year':'2026','source_name':'JSSC','related_favorite_id':'f','survey_missing':[]}
        second={**row,'id':2,'article_number':'second','title':'Receiver with analog equalizer'}
        service.sql_builder.return_value={'items':[row,second],'groups':[{'name':'JSSC'}]}
        service.enrich=Mock(side_effect=deepcopy)
        worker._chat.side_effect=[({'r':[{'i':'p0','f':'f0','t':'t0','s':85,'n':20,'e':'receiver with DFE'},
                                         {'i':'p1','f':'f0','t':'t0','s':0,'n':0,'e':'/n/0'}]},{}),
                                  ({'r':[{'i':'p0','f':'f0','t':'t0','s':25,'n':0,'e':'analog equalizer'}]}, {})]
        with patch('local_ai_recommendations.RANKING_MODE','compact'):
            result=service.run('test',{'signature':'sig','model':'gpt-oss:20b'},threading.Event())
        self.assertEqual(result['evaluated_count'],2)
        self.assertEqual(result['retried_count'],1)
        self.assertEqual([r['article_number'] for r in result['items']],['p'])
        self.assertEqual(worker._chat.call_count,2)
        self.assertEqual(len(json.loads(worker._chat.call_args_list[1].args[1][-1]['content'])['candidates']),1)
        service.pair_cache.read.assert_not_called()
        service.pair_cache.write.assert_not_called()

    def test_pair_pipeline_reuses_rating_after_unrelated_favorite_change(self):
        engine,worker=Mock(),Mock()
        worker.runtime.status.return_value={'models':[{'name':'gpt-oss:20b','digest':'model-file'}]}
        worker._chat.return_value=({'r':[{'i':'p0','f':'f0','s':85,'e':'receiver with DFE'}]}, {})
        service=RecommendationService(engine,worker,Mock())
        self.addCleanup(service.close)
        service.pair_cache=MemoryPairCache()
        favorite={'article_number':'f','title':'PAM4 receiver','source_name':'JSSC'}
        snap={'signature':'one','favorites':[favorite]}
        service.snapshot=Mock(side_effect=lambda:deepcopy(snap))
        service.profile=Mock(return_value={'topics':[{'name':'receiver','keywords':['receiver'],'favorite_ids':['f']}]})
        candidate={'id':1,'article_number':'p','title':'PAM4 receiver with DFE','abstract':'',
                   'year':'2026','source_name':'JSSC','related_favorite_id':'f',
                   'shared_terms':['receiver'],'favorite_similarity':.7,'survey_missing':[]}
        service.sql_builder.return_value={'items':[candidate],'groups':[{'name':'JSSC'}]}
        service.enrich=Mock(side_effect=deepcopy)
        with patch('local_ai_recommendations.RANKING_MODE','pair'):
            first=service.run('test',{'signature':'one','model':'gpt-oss:20b'},threading.Event())
            snap['signature']='two'
            snap['favorites'].append({'article_number':'new','title':'Optical modulator','source_name':'OFC'})
            second=service.run('test',{'signature':'two','model':'gpt-oss:20b'},threading.Event())
        self.assertEqual(first['cached_count'],0)
        self.assertEqual(second['cached_count'],1)
        self.assertEqual(second['new_attempted_count'],0)
        worker._chat.assert_called_once()
        engine.begin.assert_not_called()

    def test_quiet_delay_and_one_continuation_use_latest_snapshot(self):
        service=RecommendationService(Mock(),Mock(),Mock())
        self.addCleanup(service.close)
        service.refresh=Mock(side_effect=[{'status':'running'}, {'status':'fresh'}])
        timers=[]
        def timer(delay,callback):
            obj=Mock(callback=callback,delay=delay);timers.append(obj);return obj
        with patch('local_ai_recommendations.threading.Timer',side_effect=timer):
            service.schedule_refresh({'signature':'a'})
            service.schedule_refresh({'signature':'b'})
            timers[0].callback()  # cancelled generation cannot start work
            service.refresh.assert_not_called()
            timers[1].callback()
            self.assertEqual(len(timers),3)
            self.assertEqual(timers[-1].delay,AUTO_DELAY_SECONDS)
            timers[2].callback()
            self.assertIsNone(service.refresh_timer)
        self.assertEqual(service.refresh.call_count,2)

    def test_manual_refresh_cancels_quiet_timer(self):
        service=RecommendationService(Mock(),Mock(),Mock())
        self.addCleanup(service.close)
        timer=Mock();service.refresh_timer=timer
        service.snapshot=Mock(return_value={'favorites':[],'signature':'x'})
        self.assertEqual(service.refresh(force=True)['status'],'empty')
        timer.cancel.assert_called_once()
        self.assertIsNone(service.refresh_timer)

    def test_queued_snapshot_is_rebased_only_for_claimed_recommendation_job(self):
        engine=create_engine('sqlite://')
        self.addCleanup(engine.dispose)
        with engine.begin() as conn:
            conn.execute(text('CREATE TABLE ai_jobs(id TEXT,kind TEXT,owner TEXT,status TEXT,request_json TEXT)'))
            conn.execute(text('INSERT INTO ai_jobs VALUES(:id,:kind,:owner,:status,:request)'),
                         {'id':'test','kind':'recommendations','owner':'__recommendation_worker__',
                          'status':'running','request':json.dumps({'signature':'old','kind':'recommendations'})})
        worker=Mock()
        service=RecommendationService(engine,worker,Mock())
        self.addCleanup(service.close)
        service.snapshot=Mock(return_value={'signature':'new','favorites':[]})
        service.run('test',{'signature':'old'},threading.Event())
        with engine.begin() as conn:
            stored=json.loads(conn.execute(text('SELECT request_json FROM ai_jobs')).scalar())
            self.assertEqual(stored['signature'],'new')
            self.assertEqual(stored['requested_signature'],'old')
            conn.execute(text("UPDATE ai_jobs SET status='cancelled'"))
        with self.assertRaises(ValueError):
            service.run('test',{'signature':'old'},threading.Event())
        worker._chat.assert_not_called()


if __name__=='__main__':unittest.main()

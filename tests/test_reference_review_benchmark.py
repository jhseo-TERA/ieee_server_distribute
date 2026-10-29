"""Quality/identity guards for an isolated optimization benchmark."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from scripts import benchmark_reference_review as bench


class ReferenceReviewBenchmarkTests(unittest.TestCase):
    def test_baseline_system_prompt_is_the_production_prompt(self):
        self.assertEqual(bench.prompt(False),bench.original_prompt())
        self.assertIn('Quote an EXACT substring',bench.prompt(True))
        self.assertIn('cover',bench.prompt(True).lower())

    def test_compact_roundtrip_preserves_every_exception_and_identity(self):
        inputs=[{'number':1,'raw':'First source'},{'number':2,'raw':'Second source'},{'number':3,'raw':'Third source'}]
        issue={'field':'title','kind':'wrong_role','evidence':'Second source','reason':'x'}
        answer={'ok':[1,3],'review':[{'number':2,'verdict':'needs_review','issues':[issue]}]}
        expanded=bench.expand(answer,inputs,True)
        rows=bench.corpus.validate_reviews(expanded,inputs)
        self.assertEqual([r['number'] for r in rows],[1,2,3])
        self.assertEqual(rows[1]['issues'],[issue])
        for bad in ({'ok':[1,1,3],'review':answer['review']},
                    {'ok':[1],'review':answer['review']},
                    {'ok':[1,4],'review':answer['review']},
                    {'ok':[3,1],'review':answer['review']}):
            with self.assertRaises(ValueError):bench.expand(bad,inputs,True)

    def test_compact_does_not_bypass_per_reference_evidence_validation(self):
        inputs=[{'number':1,'raw':'First source'},{'number':2,'raw':'Other source'}]
        answer={'ok':[2],'review':[{'number':1,'verdict':'needs_review','issues':[
            {'field':'author','kind':'wrong_role','evidence':'Other source','reason':'x'}]}]}
        rows=bench.corpus.validate_reviews(bench.expand(answer,inputs,True),inputs)
        self.assertEqual(rows[0]['verdict'],'unverifiable')
        self.assertEqual(len(rows[0]['rejected_ungrounded_issues']),1)

    def test_batching_preserves_full_inputs_and_has_both_bounds(self):
        inputs=[{'number':i,'raw':'source '*15} for i in range(21)]
        fixed=list(bench.pack(inputs,5));adaptive=list(bench.pack(inputs,10))
        self.assertEqual([len(x) for x in fixed],[5,5,5,5,1])
        self.assertEqual([len(x) for x in adaptive],[10,10,1])
        self.assertEqual([r for batch in adaptive for r in batch],inputs)
        long=[{'number':i,'raw':'x'*3100} for i in range(3)]
        self.assertEqual([len(x) for x in bench.pack(long,10)],[1,1,1])
        with self.assertRaises(ValueError):list(bench.pack([{'number':1,'raw':'x'*12001}],10))

    def test_messages_never_contain_evaluation_metadata(self):
        cases=[{'input':{'number':1,'raw':'literal source','candidate':{}},
                'expected':{'title':'SECRET_EVALUATION_LABEL'},'bad_fields':['title']}]
        payload=bench.messages([c['input'] for c in cases],True)
        self.assertNotIn('SECRET_EVALUATION_LABEL',str(payload))
        self.assertNotIn('bad_fields',str(payload))

    def test_scoring_distinguishes_errors_from_legitimate_uncertainty(self):
        cases=[{'kind':'natural','input':{'number':1},'expected':{'title':'gold','author':'Lee'},'bad_fields':['title']},
               {'kind':'natural','input':{'number':2},'expected':{'author':'Kim'},'bad_fields':[]},
               {'kind':'unlabelled_transfer','input':{'number':3},'expected':{},'bad_fields':[]}]
        rows=[{'number':1,'verdict':'needs_review','issues':[{'field':'title','kind':'wrong_role'}]},
              {'number':2,'verdict':'needs_review','issues':[{'field':'author','kind':'ambiguous'}]},
              {'number':3,'verdict':'no_issue_seen','issues':[]}]
        score=bench.score(cases,rows)
        self.assertEqual(score['groups']['natural']['localized_bad_fields'],1)
        self.assertEqual(score['groups']['natural']['actionable_claims_on_matching_fields'],0)
        self.assertEqual(score['groups']['natural']['review_marked_correct_references'],1)
        self.assertEqual(score['groups']['unlabelled_transfer']['known_matching_fields'],0)

    def test_schedule_gives_all_arms_identical_cases_and_rotates_order(self):
        cases=[{'input':{'number':i}} for i in range(40)]
        policy={'block_size':20,'seed':42,'timing_repeat_blocks':2,'timing_repeat_seeds':[53,67]}
        jobs=bench.schedule(cases,policy)
        self.assertEqual(len(jobs),24)
        for offset in range(0,len(jobs),4):
            group=jobs[offset:offset+4]
            self.assertEqual(set(j['arm'] for j in group),set(bench.ARMS))
            self.assertTrue(all(j['numbers']==group[0]['numbers'] for j in group))
        self.assertNotEqual(jobs[0]['arm'],jobs[4]['arm'])

    def test_seed_adapter_preserves_all_other_model_request_options(self):
        client=bench.SeededClient(42)
        session=Mock();session.post=Mock(return_value='response')
        original=session.post
        with patch.object(bench.corpus.bulk.OllamaClient,'_session',return_value=session):
            adapted=client._session()
        response=adapted.post('http://127.0.0.1:11434/api/chat',json={'model':'same','options':{'num_ctx':8192,'temperature':.1}})
        self.assertEqual(response,'response')
        payload=original.call_args.kwargs['json']
        self.assertEqual(payload['options'],{'num_ctx':8192,'temperature':.1,'seed':42})


if __name__=='__main__':unittest.main()

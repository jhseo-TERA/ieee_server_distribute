import unittest
from unittest.mock import Mock, patch

import web.app as app_module
from local_ai_service import validate_job


class LocalAIRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = app_module.app
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()
        self.service = Mock()
        self.state = self.app.blueprints['local_ai'].ai_state
        self.previous_state = dict(self.state)
        self.state.clear()
        self.state['service'] = self.service
        self.environment = patch.dict('os.environ', {'LOCAL_AI_ENABLED': '1'})
        self.environment.start()

    def tearDown(self):
        self.state.clear()
        self.state.update(self.previous_state)
        self.environment.stop()

    def login(self, role='viewer', username='alice'):
        with self.client.session_transaction() as session:
            session.update(authenticated=True, username=username, role=role, csrf_token='csrf-test')

    def post(self, path, data=None):
        return self.client.post(path, json=data or {}, headers={'X-CSRF-Token': 'csrf-test'})

    def test_unauthenticated_api_is_not_available(self):
        response = self.client.get('/api/ai/jobs')
        self.assertEqual(response.status_code, 401)
        self.service.repo.list_jobs.assert_not_called()

    def test_jobs_require_csrf(self):
        self.login()
        response = self.client.post('/api/ai/jobs', json={'question': 'test'})
        self.assertEqual(response.status_code, 403)
        self.service.submit.assert_not_called()

    def test_get_job_is_owner_scoped(self):
        self.login()
        self.service.repo.get_job.return_value = None
        response = self.client.get('/api/ai/jobs/other-users-job')
        self.assertEqual(response.status_code, 404)
        self.service.repo.get_job.assert_called_once_with('other-users-job', 'alice')

    def test_job_list_is_owner_scoped(self):
        self.login()
        self.service.repo.list_jobs.return_value = []
        response = self.client.get('/api/ai/jobs')
        self.assertEqual(response.status_code, 200)
        self.service.repo.list_jobs.assert_called_once_with('alice', limit=20)
        self.assertEqual(response.headers['Cache-Control'], 'private, no-store')

    def test_cancel_is_owner_scoped(self):
        self.login()
        self.service.cancel.side_effect = KeyError('job')
        response = self.post('/api/ai/jobs/job/cancel')
        self.assertEqual(response.status_code, 404)
        self.service.cancel.assert_called_once_with('job', 'alice')

    def test_viewer_can_submit_read_only_question(self):
        self.login()
        self.service.submit.return_value = {'id': 'job', 'status': 'queued', 'result': None}
        response = self.post('/api/ai/jobs', {'question': 'Explain this receiver'})
        self.assertEqual(response.status_code, 202)
        self.service.submit.assert_called_once_with({'question': 'Explain this receiver'}, 'alice', 'viewer')

    def test_extract_permission_checked_by_service(self):
        self.login()
        self.service.submit.side_effect = lambda payload, owner, role: validate_job(payload, role)
        response = self.post('/api/ai/jobs', {'kind': 'extract', 'article_numbers': ['123']})
        self.assertEqual(response.status_code, 403)

    def test_viewer_cannot_list_or_approve_proposals(self):
        self.login()
        self.assertEqual(self.client.get('/api/ai/proposals').status_code, 403)
        response = self.post('/api/ai/proposals/decision', {'ids': [1], 'decision': 'approve'})
        self.assertEqual(response.status_code, 403)
        self.service.repo.decide_proposals.assert_not_called()

    def test_admin_explicit_visual_confirmation_forwarded(self):
        self.login(role='admin')
        self.service.repo.decide_proposals.return_value = {'decision': 'approved'}
        response = self.post('/api/ai/proposals/decision', {'ids': [1], 'decision': 'approve', 'confirm_visual': True})
        self.assertEqual(response.status_code, 200)
        self.service.repo.decide_proposals.assert_called_once_with([1], 'approve', 'alice', confirm_visual=True)

    def test_bad_confirmation_or_ids_cannot_reach_repository(self):
        self.login(role='admin')
        for payload in ({'ids': [True], 'decision': 'approve'}, {'ids': [1], 'decision': 'execute'},
                        {'ids': [1], 'decision': 'approve', 'confirm_visual': 'true'}):
            with self.subTest(payload=payload):
                self.assertEqual(self.post('/api/ai/proposals/decision', payload).status_code, 400)
        self.service.repo.decide_proposals.assert_not_called()

    def test_proposal_job_lookup_uses_owner(self):
        self.login(role='admin')
        self.service.repo.get_job.return_value = None
        response = self.client.get('/api/ai/proposals?job_id=other')
        self.assertEqual(response.status_code, 404)
        self.service.repo.get_job.assert_called_once_with('other', 'alice')
        self.service.repo.list_proposals.assert_not_called()

    def test_database_exception_is_sanitized(self):
        from sqlalchemy.exc import SQLAlchemyError
        self.login()
        self.service.repo.list_jobs.side_effect = SQLAlchemyError('secret-host:password')
        response = self.client.get('/api/ai/jobs')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(b'secret-host', response.data)

    def test_survey_requires_login_and_csrf(self):
        self.assertEqual(self.client.get('/api/ai/survey/gaps').status_code, 401)
        self.assertEqual(self.post('/api/ai/survey/jobs').status_code, 401)
        self.login()
        self.assertEqual(self.client.post('/api/ai/survey/jobs', json={}).status_code, 403)
        self.service.survey.submit.assert_not_called()

    def test_viewer_can_analyze_and_read_gap_queue(self):
        self.login()
        self.service.survey.submit.return_value = {'id': 'survey-job', 'status': 'queued'}
        self.assertEqual(self.post('/api/ai/survey/jobs', {'kind': 'analyze'}).status_code, 202)
        self.service.survey.submit.assert_called_once_with({'kind': 'analyze'}, 'alice', 'viewer')
        self.service.survey.gaps.return_value = {'items': [], 'returned_count': 0}
        response = self.client.get('/api/ai/survey/gaps')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'private, no-store')

    def test_survey_validation_is_reported_without_enqueuing(self):
        self.login()
        self.service.survey.submit.side_effect = ValueError('invalid filter')
        self.assertEqual(self.post('/api/ai/survey/jobs').status_code, 400)


if __name__ == '__main__':
    unittest.main()

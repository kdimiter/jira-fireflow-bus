"""Jira boundary and mutation outcome contracts."""
import unittest
from unittest.mock import patch

from algosec_jira_bus.jira import Jira, JiraError, JiraMutationUnknown


class RequestFailure(Exception):
    def __init__(self, code=None):
        self.code = code


class JiraContract(unittest.TestCase):
    def client(self, error):
        def request(*_args, **_kwargs):
            raise error
        return Jira({'base_url': 'https://EXAMPLE.atlassian.net:443/',
                     'email': 'test@example.test', 'token_ref': 'env:UNUSED'},
                    request=request)

    def test_jira_origin_is_canonicalized_once(self):
        jira = self.client(RequestFailure())
        self.assertEqual(jira.config['base_url'], 'https://example.atlassian.net')

    @patch('algosec_jira_bus.jira.resolve_secret', return_value='test-only')
    def test_non_idempotent_post_with_unknown_outcome_has_a_distinct_error(self, _):
        with self.assertRaises(JiraMutationUnknown):
            self.client(TimeoutError('lost response')).comment('NET-1', 'hello')
        with self.assertRaises(JiraMutationUnknown):
            self.client(RequestFailure(500)).comment('NET-1', 'hello')

    @patch('algosec_jira_bus.jira.resolve_secret', return_value='test-only')
    def test_definitive_post_rejection_and_idempotent_put_remain_retryable(self, _):
        with self.assertRaises(JiraError) as rejected:
            self.client(RequestFailure(400)).comment('NET-1', 'hello')
        self.assertNotIsInstance(rejected.exception, JiraMutationUnknown)
        with self.assertRaises(JiraError) as update:
            self.client(TimeoutError('lost response')).update_fields(
                'NET-1', {'customfield_10000': '42'})
        self.assertNotIsInstance(update.exception, JiraMutationUnknown)

    @patch('algosec_jira_bus.jira.resolve_secret', return_value='test-only')
    def test_result_update_accepts_only_a_valid_assignee_account_id(self, _):
        calls = []
        jira = Jira({'base_url': 'https://example.atlassian.net',
                     'email': 'test@example.test', 'token_ref': 'env:UNUSED'},
                    request=lambda *args, **kwargs: calls.append((args, kwargs)) or {})
        jira.update_fields('NET-1', {
            'customfield_10000': 'Alice',
            'assignee': {'accountId': '712020:abc-123'},
        })
        self.assertEqual(calls[0][1]['body']['fields']['assignee'],
                         {'accountId': '712020:abc-123'})
        for invalid in ({'accountId': ''}, {'name': 'alice'}, None, 'alice'):
            with self.subTest(invalid=invalid), self.assertRaises(JiraError):
                jira.update_fields('NET-1', {'assignee': invalid})

    @patch('algosec_jira_bus.jira.resolve_secret', return_value='test-only')
    def test_assignable_user_search_is_project_scoped_and_bounded(self, _):
        calls = []
        jira = Jira({'base_url': 'https://example.atlassian.net',
                     'email': 'test@example.test', 'token_ref': 'env:UNUSED'},
                    request=lambda *args, **kwargs: calls.append((args, kwargs)) or [{
                        'accountId': '712020:abc-123', 'displayName': 'Alice'}])
        users = jira.assignable_users('NET', query='alice@example.test')
        self.assertEqual(users[0]['accountId'], '712020:abc-123')
        self.assertEqual(calls[0][0][1], '/rest/api/3/user/assignable/search')
        self.assertEqual(calls[0][1]['query'], {
            'project': 'NET', 'maxResults': 50, 'query': 'alice@example.test'})
        with self.assertRaises(JiraError):
            jira.assignable_users('NET', max_results=1001)


if __name__ == '__main__':
    unittest.main()

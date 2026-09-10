"""Search must not hide eligible issues behind an already handled first page."""
import unittest
from unittest.mock import patch
from algosec_jira_bus.jira import Jira, JiraError


class Pagination(unittest.TestCase):
    @staticmethod
    def issue(number):
        return {'id': str(1000 + number), 'key': 'NET-%d' % number, 'fields': {}}

    def client(self, replies):
        calls = []
        def request(config, path, **kwargs):
            calls.append(dict(kwargs['query']))
            return replies[len(calls)-1]
        return Jira({'base_url': 'https://example.atlassian.net', 'email': 'test',
                     'token_ref': 'env:UNUSED'}, request=request), calls

    @patch('algosec_jira_bus.jira.resolve_secret', return_value='test-only')
    def test_all_51_issues_are_returned(self, _):
        client, calls = self.client([
            {'issues': [self.issue(i) for i in range(1, 51)],
             'nextPageToken': 'next', 'isLast': False},
            {'issues': [self.issue(51)], 'isLast': True}])
        issues = client.search('project = NET', {'summary'})
        self.assertEqual(len(issues), 51)
        self.assertEqual(issues[-1]['key'], 'NET-51')
        self.assertEqual(calls[1]['nextPageToken'], 'next')

    @patch('algosec_jira_bus.jira.resolve_secret', return_value='test-only')
    def test_repeated_token_fails_instead_of_looping_or_silently_truncating(self, _):
        client, _calls = self.client([{'issues': [], 'nextPageToken': 'same', 'isLast': False}] * 3)
        with self.assertRaises(JiraError):
            client.search('project = NET', {'summary'})

    @patch('algosec_jira_bus.jira.resolve_secret', return_value='test-only')
    def test_nonfinal_page_without_token_is_not_success(self, _):
        client, _calls = self.client([{'issues': [], 'isLast': False}])
        with self.assertRaises(JiraError):
            client.search('project = NET', {'summary'})

    @patch('algosec_jira_bus.jira.resolve_secret', return_value='test-only')
    def test_failed_later_page_does_not_return_partial_results(self, _):
        client, _calls = self.client([{'issues': [self.issue(1)], 'nextPageToken': 'next'}, {}])
        with self.assertRaises(JiraError):
            client.search('project = NET', {'summary'})

    @patch('algosec_jira_bus.jira.resolve_secret', return_value='test-only')
    def test_scan_limit_rejects_an_overbroad_query(self, _):
        client, _calls = self.client([
            {'issues': [self.issue(1), self.issue(2)], 'isLast': True}
        ])
        client.config['scan_limit'] = 1
        with self.assertRaisesRegex(JiraError, 'scan_limit'):
            client.search('project = NET', {'summary'})

    @patch('algosec_jira_bus.jira.resolve_secret', return_value='test-only')
    def test_scan_limit_accepts_the_exact_boundary(self, _):
        client, _calls = self.client([
            {'issues': [self.issue(1), self.issue(2)], 'isLast': True}
        ])
        client.config['scan_limit'] = 2
        self.assertEqual(len(client.search('project = NET', {'summary'})), 2)

    @patch('algosec_jira_bus.jira.resolve_secret', return_value='test-only')
    def test_invalid_scan_limits_fail_before_network(self, _):
        for value in (True, 0, 10001, '1000'):
            with self.subTest(value=value):
                client, calls = self.client([])
                client.config['scan_limit'] = value
                with self.assertRaisesRegex(JiraError, 'scan_limit'):
                    client.search('project = NET', {'summary'})
                self.assertEqual(calls, [])

    @patch('algosec_jira_bus.jira.resolve_secret', return_value='test-only')
    def test_malformed_issue_fails_the_whole_search_before_returning_partial_data(self, _):
        malformed = (
            None,
            {'id': '1234', 'key': 'net-1', 'fields': {}},
            {'id': 'NET-1', 'key': 'NET-1', 'fields': {}},
            {'id': '1234', 'key': 'NET-1', 'fields': []},
        )
        for value in malformed:
            client, _calls = self.client([{'issues': [self.issue(1), value], 'isLast': True}])
            with self.subTest(value=value), self.assertRaisesRegex(JiraError,
                                                                    'Unexpected Jira issue'):
                client.search('project = NET', {'summary'})

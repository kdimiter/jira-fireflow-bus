"""Jira event ingestion must never silently return a partial history."""
import unittest
from unittest.mock import patch

from algosec_jira_bus.jira import Jira, JiraError, plain


class JiraEvents(unittest.TestCase):
    def client(self, pages):
        self.calls = []
        def request(_transport, path, **kwargs):
            self.calls.append((path, kwargs))
            return pages[len(self.calls) - 1]
        return Jira({'base_url': 'https://example.atlassian.net',
                     'email': 'test@example.test', 'token_ref': 'env:UNUSED'}, request=request)

    def comment(self, identifier='1'):
        return {'id': identifier, 'author': {'displayName': 'Requester'},
                'created': '2026-09-11T10:00:00.000+0000', 'body': 'Any update?',
                'properties': [{'key': 'example', 'value': {'x': 1}}]}

    @patch('algosec_jira_bus.jira.resolve_secret', return_value='test-only')
    def test_comments_read_all_pages_by_immutable_id_and_keep_metadata(self, _):
        first = self.comment()
        second = self.comment('2')
        jira = self.client([{'startAt': 0, 'total': 2, 'comments': [first]},
                            {'startAt': 1, 'total': 2, 'comments': [second]},
                            {'startAt': 0, 'total': 2, 'isLast': True,
                             'values': [first, second]}])
        result = jira.comments('10001', limit=1)
        self.assertEqual([item['id'] for item in result], ['1', '2'])
        self.assertEqual(result[0], dict(first, text='Any update?'))
        self.assertEqual([path for path, _ in self.calls[:2]],
                         ['/rest/api/3/issue/10001/comment'] * 2)
        self.assertEqual(self.calls[2][0], '/rest/api/3/comment/list')
        self.assertEqual(self.calls[1][1]['query']['startAt'], 1)
        self.assertEqual(self.calls[0][1]['query']['orderBy'], '-created')

    @patch('algosec_jira_bus.jira.resolve_secret', return_value='test-only')
    def test_properties_are_hydrated_using_documented_bulk_read_contract(self, _):
        comment = self.comment()
        del comment['properties']
        hydrated = dict(comment, properties=[
            {'key': 'algosec-jira-bus.origin', 'value': {'source': 'fireflow'}}])
        jira = self.client([{'startAt': 0, 'total': 1, 'comments': [comment]},
                            {'startAt': 0, 'total': 1, 'values': [hydrated]}])
        self.assertEqual(jira.comments('10001')[0]['properties'], hydrated['properties'])
        self.assertEqual(self.calls[1][0], '/rest/api/3/comment/list')
        self.assertEqual(self.calls[1][1]['body'], {'ids': [1]})
        self.assertEqual(self.calls[1][1]['query'], {'expand': 'properties'})

    @patch('algosec_jira_bus.jira.resolve_secret', return_value='test-only')
    def test_empty_unexpanded_properties_cannot_hide_the_bus_origin(self, _):
        comment = dict(self.comment(), properties=[])
        hydrated = dict(comment, properties=[
            {'key': 'algosec-jira-bus.origin', 'value': {'source': 'fireflow'}}])
        jira = self.client([{'startAt': 0, 'total': 1, 'comments': [comment]},
                            {'startAt': 0, 'total': 1, 'isLast': True,
                             'values': [hydrated]}])
        self.assertEqual(jira.comments('10001')[0]['properties'], hydrated['properties'])

    @patch('algosec_jira_bus.jira.resolve_secret', return_value='test-only')
    def test_incomplete_property_hydration_fails_closed(self, _):
        comment = self.comment()
        del comment['properties']
        jira = self.client([{'startAt': 0, 'total': 1, 'comments': [comment]},
                            {'startAt': 0, 'total': 0, 'values': []}])
        with self.assertRaises(JiraError):
            jira.comments('10001')

    @patch('algosec_jira_bus.jira.resolve_secret', return_value='test-only')
    def test_changelog_reads_non_status_pages_and_returns_status_events(self, _):
        author = {'displayName': 'Requester'}
        jira = self.client([
            {'startAt': 0, 'total': 2, 'values': [{'id': '8', 'created': 'time',
              'author': author, 'items': [{'field': 'summary', 'toString': 'Title'}]}]},
            {'startAt': 1, 'total': 2, 'isLast': True, 'values': [{'id': '9',
              'created': 'later', 'author': author, 'items': [{'fieldId': 'status',
              'from': '6', 'to': '1', 'fromString': 'Closed', 'toString': 'Open'}]}]}])
        self.assertEqual(jira.changelog('10001', limit=1), [
            {'id': '8', 'created': 'time', 'author': author, 'kind': 'other'},
            {'id': '9', 'created': 'later', 'author': author, 'kind': 'status',
             'from': 'Closed', 'to': 'Open', 'from_id': '6', 'to_id': '1'}])
        self.assertEqual(self.calls[1][0], '/rest/api/3/issue/10001/changelog')

    @patch('algosec_jira_bus.jira.resolve_secret', return_value='test-only')
    def test_changelog_cursor_does_not_stop_before_newer_pages(self, _):
        def rows(first, last):
            return [{'id': str(value), 'created': 'time', 'items': [
                {'field': 'summary', 'toString': str(value)}]}
                    for value in range(first, last + 1)]
        jira = self.client([
            {'startAt': 0, 'total': 150, 'values': rows(1, 100)},
            {'startAt': 100, 'total': 150, 'isLast': True, 'values': rows(101, 150)},
        ])
        result = jira.changelog('10001', after_id='90')
        self.assertEqual(result[0]['id'], '1')
        self.assertEqual(result[-1]['id'], '150')
        self.assertEqual([call[1]['query']['startAt'] for call in self.calls], [0, 100])

    @patch('algosec_jira_bus.jira.resolve_secret', return_value='test-only')
    def test_changelog_preserves_bus_origin_metadata(self, _):
        metadata = {'extraData': {'algosec-jira-bus.origin': 'fireflow'}}
        history = {'id': '8', 'created': 'time', 'historyMetadata': metadata,
                   'items': [{'field': 'status', 'toString': 'Closed'}]}
        jira = self.client([{'startAt': 0, 'total': 1, 'values': [history]}])
        self.assertEqual(jira.changelog('1')[0]['historyMetadata'], metadata)

    @patch('algosec_jira_bus.jira.resolve_secret', return_value='test-only')
    def test_changelog_rejects_ambiguous_status_events(self, _):
        for changes in ([{'field': 'status'}],
                        [{'field': 'status', 'toString': 'Closed'}] * 2):
            jira = self.client([{'startAt': 0, 'total': 1, 'values': [
                {'id': '8', 'created': 'time', 'items': changes}]}])
            with self.subTest(changes=changes), self.assertRaises(JiraError):
                jira.changelog('1')

    def test_only_immutable_identifiers_and_bounded_integer_limits_are_accepted(self):
        jira = self.client([])
        for name in ('comments', 'changelog'):
            method = getattr(jira, name)
            for identifier in ('NET-1', '../1', '0', 1, None):
                with self.subTest(method=name, identifier=identifier), self.assertRaises(JiraError):
                    method(identifier)
            for kwargs in ({'limit': True}, {'limit': 0}, {'limit': 101},
                           {'max_items': 0}, {'max_items': 10001}):
                with self.subTest(kwargs=kwargs), self.assertRaises(JiraError):
                    method('1', **kwargs)
        self.assertEqual(self.calls, [])

    @patch('algosec_jira_bus.jira.resolve_secret', return_value='test-only')
    def test_malformed_or_incomplete_comment_pages_fail_closed(self, _):
        cases = [
            [{'startAt': 0, 'total': 2, 'comments': []}],
            [{'startAt': 1, 'total': 1, 'comments': [self.comment()]}],
            [{'startAt': 0, 'total': 2, 'isLast': True, 'comments': [self.comment()]}],
            [{'startAt': 0, 'total': 1, 'comments': [None]}],
            [{'startAt': 0, 'total': 2, 'comments': [self.comment()]},
             {'startAt': 1, 'total': 2, 'comments': [self.comment()]}],
        ]
        for pages in cases:
            with self.subTest(pages=pages), self.assertRaises(JiraError):
                self.client(pages).comments('1')

    @patch('algosec_jira_bus.jira.resolve_secret', return_value='test-only')
    def test_scan_ceiling_returns_the_recent_tail_for_initial_baseline(self, _):
        row = self.comment()
        jira = self.client([{'startAt': 0, 'total': 2, 'comments': [row]},
                            {'startAt': 0, 'total': 1, 'isLast': True,
                             'values': [row]}])
        result = jira.comments('1', max_items=1)
        self.assertEqual([item['id'] for item in result], ['1'])
        self.assertEqual(len(self.calls), 2)

    @patch('algosec_jira_bus.jira.resolve_secret', return_value='test-only')
    def test_cursor_outside_the_recent_tail_fails_instead_of_losing_events(self, _):
        jira = self.client([{'startAt': 0, 'total': 3,
                             'comments': [self.comment('3'), self.comment('2')]}])
        with self.assertRaisesRegex(JiraError, 'cursor fell outside'):
            jira.comments('1', after_id='1', limit=2, max_items=2)

    @patch('algosec_jira_bus.jira.resolve_secret', return_value='test-only')
    def test_deleted_comment_cursor_is_safe_when_full_remaining_history_is_read(self, _):
        old, new = self.comment('8'), self.comment('10')
        jira = self.client([
            {'startAt': 0, 'total': 2, 'comments': [new, old]},
            {'startAt': 0, 'total': 2, 'isLast': True, 'values': [new, old]},
        ])
        result = jira.comments('1', after_id='9')
        self.assertEqual([row['id'] for row in result], ['8', '10'])

    @patch('algosec_jira_bus.jira.resolve_secret', return_value='test-only')
    def test_deleted_cursor_without_bounded_overlap_still_fails_closed(self, _):
        jira = self.client([{'startAt': 0, 'total': 3,
                             'comments': [self.comment('12'), self.comment('11')]}])
        with self.assertRaisesRegex(JiraError, 'cursor fell outside'):
            jira.comments('1', after_id='9', limit=2, max_items=2)

    @patch('algosec_jira_bus.jira.resolve_secret', return_value='test-only')
    def test_outbound_comment_has_origin_property_for_loop_prevention(self, _):
        jira = self.client([{'id': '1'}])
        jira.comment('NET-1', 'FireFlow update')
        self.assertEqual(self.calls[0][1]['body']['properties'], [
            {'key': 'algosec-jira-bus.origin', 'value': {'source': 'fireflow'}}])

    @patch('algosec_jira_bus.jira.resolve_secret', return_value='test-only')
    def test_outbound_transition_marks_its_changelog_origin(self, _):
        jira = self.client([{'transitions': [{'id': '3', 'name': 'Closed'}]}, {}])
        jira.transition('NET-1', 'Closed')
        metadata = self.calls[1][1]['body']['historyMetadata']
        self.assertEqual(metadata['extraData']['algosec-jira-bus.origin'], 'fireflow')

    def test_adf_retains_paragraphs_hardbreaks_and_inline_spacing(self):
        document = {'type': 'doc', 'content': [
            {'type': 'paragraph', 'content': [
                {'type': 'text', 'text': 'What '},
                {'type': 'text', 'text': 'status', 'marks': [{'type': 'strong'}]},
                {'type': 'text', 'text': '?'}, {'type': 'hardBreak'},
                {'type': 'text', 'text': 'Please check.'}]},
            {'type': 'paragraph', 'content': [{'type': 'text', 'text': 'Thanks.'}]}]}
        self.assertEqual(plain(document), 'What status?\nPlease check.\nThanks.')

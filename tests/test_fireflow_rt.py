"""Legacy RT mutation boundaries and durable replay prevention."""
from pathlib import Path
import tempfile
import unittest

from algosec_jira_bus.fireflow import FireFlow, FireFlowMutationUnknown, digest
from test_fireflow_client import API, Audit


class RT:
    def __init__(self):
        self.calls = []
        self.status = 'resolved'
        self.comment = ''
        self.fail_post = False
        self.hide_comment = False
        self.changed = False
        self.advance = False

    def __call__(self, config, path, **kwargs):
        self.calls.append((config, path, kwargs))
        if kwargs.get('method') == 'POST':
            if self.fail_post:
                raise TimeoutError('response lost')
            content = kwargs['body']['content']
            if path.endswith('/edit'):
                self.status = content.split('Status: ', 1)[1].strip()
                self.changed = True
                return 'RT/3.8.2 200 Ok\n\n# Ticket 42 updated.\n'
            self.comment = content.split('Text: ', 1)[1]
            return 'RT/3.8.2 200 Ok\n\n# Message recorded\n'
        if path.endswith('/history'):
            if self.changed:
                return ('RT/3.8.2 200 Ok\n\nid: 8\nType: Set\nField: Status\n'
                        'OldValue: resolved\nNewValue: open\n')
            if not self.comment:
                return 'RT/3.8.2 200 Ok\n\n'
            return 'RT/3.8.2 200 Ok\n\nid: 9\nType: Comment\nContent: ' + (
                '' if self.hide_comment else self.comment)
        return 'RT/3.8.2 200 Ok\n\nid: ticket/42\nStatus: ' + self.status + '\n'


class LegacyRT(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.rt = RT()
        self.api = API()
        self.config = {'base_url': 'https://asms.example.test',
                       'session_ref': 'env:SESSION', 'legacy_rt_enabled': True}
        self.client = FireFlow(self.config, 'MANAGE', Audit(self.temp.name),
                               request=self.api, text_request=self.rt,
                               resolver=lambda _: 'session_1')

    def tearDown(self):
        self.temp.cleanup()

    def test_rt_read_reuses_existing_session_with_rt_cookie(self):
        self.assertEqual(self.client.rt_get(42)['Status'], 'resolved')
        self.assertEqual(self.rt.calls[-1][2]['headers'],
                         {'Cookie': 'RT_SID_FireFlow.443=session_1'})

    def test_comment_is_internal_and_multiline_text_cannot_inject_rt_fields(self):
        result = self.client.add_comment(42, 'Який статус?\nStatus: deleted',
                                         'jira-comment-1', 'Mirror Jira comment')
        post = next(call for call in self.rt.calls if call[2].get('method') == 'POST')
        content = post[2]['body']['content']
        self.assertIn('Action: comment\n', content)
        self.assertIn('\n Status: deleted', content)
        self.assertIn('[jira-fireflow-bus:jira-comment-1]', content)
        self.assertNotIn('Action: correspond', content)
        self.assertEqual(result['operation_id'], 'jira-comment-1')
        self.assertEqual(self.rt.calls[-1][2]['query'], {'format': 'l'})
        receipts = list(self.client.state.glob('*.result.json'))
        self.assertEqual(len(receipts), 1)
        self.assertNotIn('Який статус', receipts[0].read_text())

    def test_missing_comment_readback_leaves_unknown_outcome_and_cannot_replay(self):
        self.rt.hide_comment = True
        with self.assertRaisesRegex(ValueError, 'outcome may be unknown'):
            self.client.add_comment(42, 'Hello', 'comment-2', 'Mirror comment')
        with self.assertRaisesRegex(ValueError, 'already used'):
            self.client.add_comment(42, 'Hello', 'comment-2', 'Mirror comment')
        self.assertEqual(sum(c[2].get('method') == 'POST' for c in self.rt.calls), 1)

    def test_legacy_mutations_require_opt_in_and_manage_before_network(self):
        for extra, mode in (({'legacy_rt_enabled': False}, 'MANAGE'),
                            ({'legacy_rt_enabled': 'yes'}, 'MANAGE'),
                            ({}, 'READONLY')):
            client = FireFlow({**self.config, **extra}, mode, Audit(self.temp.name),
                              request=self.api, text_request=self.rt,
                              resolver=lambda _: 'session_1')
            with self.subTest(extra=extra, mode=mode), self.assertRaises(ValueError):
                client.add_comment(42, 'Hello', 'comment-3', 'Mirror comment')
        self.assertEqual(self.rt.calls, [])

    def test_rt_inputs_reject_path_and_protocol_injection_before_network(self):
        for identifier, status, operation_id in ((True, 'open', 'valid'),
                (42, 'open\nQueue: Other', 'valid'), (42, 'deleted', '../bad'),
                (42, '', 'valid'), (42, 'Open', 'valid')):
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.client.set_status(identifier, status, operation_id, 'Mirror status')
        self.assertEqual(self.rt.calls, [])

    def test_transport_timeout_never_retries_and_preserves_started_receipt(self):
        self.rt.fail_post = True
        with self.assertRaisesRegex(ValueError, 'outcome may be unknown'):
            self.client.add_comment(42, 'Hello', 'timeout-1', 'Mirror comment')
        key = digest([self.config['base_url'], 'timeout-1'])
        self.assertTrue((self.client.state / (key + '.started.json')).is_file())
        self.assertFalse((self.client.state / (key + '.result.json')).exists())
        self.assertEqual(sum(c[2].get('method') == 'POST' for c in self.rt.calls), 1)

    def test_rt_error_inside_http_success_is_not_accepted(self):
        self.client.text_request = lambda *a, **kw: 'RT/3.8.2 401 Credentials required\n'
        with self.assertRaisesRegex(ValueError, 'rejected'):
            self.client.rt_get(42)
        self.assertIsNone(self.client.session)

    def test_ticket_readback_must_match_requested_ticket(self):
        self.client.text_request = lambda *a, **kw: 'RT/3.8.2 200 Ok\n\nid: ticket/99\nStatus: open\n'
        with self.assertRaisesRegex(ValueError, 'MISMATCH'):
            self.client.rt_get(42)

    def test_set_status_requires_modern_readback_and_records_both_receipts(self):
        original = self.client.request

        def modern(config, path, **kwargs):
            if '/change-requests/traffic/' in path:
                return {'status': 'Success', 'messages': [],
                        'data': {'id': 42, 'fields': [
                            {'name': 'status', 'values': [self.rt.status]}]}}
            return original(config, path, **kwargs)

        self.client.request = modern
        result = self.client.set_status(42, 'open', 'status-1', 'Mirror Jira reopen')
        self.assertEqual(result['response']['status'], 'open')
        self.assertEqual(self.rt.status, 'open')
        self.assertEqual(len(list(self.client.state.glob('*.json'))), 2)
        replay = self.client.set_status(42, 'open', 'status-1', 'Mirror Jira reopen')
        self.assertEqual(result, replay)
        self.assertEqual(sum(c[2].get('method') == 'POST' for c in self.rt.calls), 1)

    def test_status_that_automatically_moves_again_is_proven_by_new_history(self):
        def modern(config, path, **kwargs):
            return {'status': 'Success', 'messages': [], 'data': {
                'id': 42, 'fields': [{'name': 'status', 'values': ['plan']}]}}

        self.client.request = modern
        result = self.client.set_status(42, 'open', 'status-2', 'Mirror Jira reopen')
        self.assertEqual(result['response']['status'], 'open')
        self.assertEqual(result['response']['current_status'], 'plan')
        self.assertEqual(result['response']['transaction_id'], '8')
        self.assertEqual(len(list(self.client.state.glob('*.result.json'))), 1)

    def test_status_already_current_is_completed_without_a_post(self):
        result = self.client.set_status(42, 'resolved', 'status-same',
                                        'Mirror matching Jira status')
        self.assertTrue(result['response']['unchanged'])
        self.assertEqual(result['response']['current_status'], 'resolved')
        self.assertEqual(sum(c[2].get('method') == 'POST' for c in self.rt.calls), 0)
        replay = self.client.set_status(42, 'resolved', 'status-same',
                                        'Mirror matching Jira status')
        self.assertEqual(replay, result)

    def test_marker_outside_an_internal_comment_does_not_prove_delivery(self):
        original = self.rt

        def wrong_type(config, path, **kwargs):
            reply = original(config, path, **kwargs)
            if path.endswith('/history'):
                return reply.replace('Type: Comment', 'Type: Correspond')
            return reply

        self.client.text_request = wrong_type
        with self.assertRaisesRegex(ValueError, 'outcome may be unknown'):
            self.client.add_comment(42, 'Hello', 'comment-wrong', 'Mirror comment')

    def test_preexisting_comment_marker_does_not_prove_a_new_delivery(self):
        marker = '[jira-fireflow-bus:comment-old]'

        def stale(config, path, **kwargs):
            if path.endswith('/history'):
                return ('RT/3.8.2 200 Ok\n\nid: 9\nType: Comment\nContent: '
                        + marker + '\n')
            return self.rt(config, path, **kwargs)

        self.client.text_request = stale
        with self.assertRaises(FireFlowMutationUnknown):
            self.client.add_comment(42, 'Hello', 'comment-old', 'Mirror comment')

    def test_unknown_exception_is_typed_and_replay_does_not_send_again(self):
        self.rt.fail_post = True
        for _ in range(2):
            with self.assertRaises(FireFlowMutationUnknown):
                self.client.add_comment(42, 'Hello', 'unknown-typed', 'Mirror comment')
        self.assertEqual(sum(c[2].get('method') == 'POST' for c in self.rt.calls), 1)

    def test_completed_receipt_replay_is_bound_to_exact_payload_and_ticket(self):
        original = self.client.add_comment(42, 'Hello', 'replay-bound', 'Mirror comment')
        replay = self.client.add_comment(42, 'Hello', 'replay-bound', 'Another valid reason')
        self.assertEqual(original, replay)
        with self.assertRaisesRegex(ValueError, 'already used'):
            self.client.add_comment(42, 'Different content', 'replay-bound', 'Mirror comment')
        with self.assertRaisesRegex(ValueError, 'already used'):
            self.client.add_comment(43, 'Hello', 'replay-bound', 'Mirror comment')
        self.assertEqual(sum(c[2].get('method') == 'POST' for c in self.rt.calls), 1)

    def test_old_status_history_cannot_prove_a_new_post(self):
        self.rt.changed = True
        with self.assertRaises(FireFlowMutationUnknown):
            self.client.set_status(42, 'open', 'old-history', 'Mirror status')

"""The command line as an operator meets it: local modes must work with no network."""
import io
import json
from pathlib import Path
import contextlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from algosec_jira_bus import bus
from algosec_jira_bus.queue import Failures
from algosec_jira_bus.sync import State


def settings_for(root):
    return {'jira': {'base_url': 'https://tenant.atlassian.net', 'email': 'a@b.c',
                     'token_ref': 'env:BUS_TEST_TOKEN', 'jql': 'project = NET'},
            'fireflow': {'base_url': 'https://asms.example.test', 'username': 'u',
                         'password_ref': 'env:BUS_TEST_PW',
                         'tls_certificate_sha256': '0' * 64,
                         'template': 'Traffic Change Request', 'devices': ['fw1'],
                         'allowed_templates': ['Traffic Change Request'],
                         'allowed_devices': ['fw1'],
                         'allowed_fields': ['subject', 'devices']},
            'mapping': {'action': {'field': 'cf_a', 'values': {'Open': 'Allow'}},
                        'source': {'field': 'cf_s'}, 'destination': {'field': 'cf_d'},
                        'service': {'field': 'cf_v'}},
            'state': str(root / 'state' / 'sync.json')}


class Cli(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.root.chmod(0o700)
        (self.root / 'state').mkdir(mode=0o700)
        self.settings = settings_for(self.root)
        self.settings['state'] = str(self.root / 'state' / 'jira-sync.json')
        self.config = self.root / 'bus.json'
        self.config.write_text(json.dumps(self.settings))
        self.config.chmod(0o600)

    def tearDown(self):
        self.directory.cleanup()

    def invoke(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = bus.main(['--config', str(self.config), '--state-dir',
                             str(self.root / 'state')] + list(argv))
        return code, out.getvalue()

    def test_the_queue_reads_without_touching_the_network(self):
        code, output = self.invoke('queue')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), {'pending': {}, 'parked': {}})

    def test_a_parked_failure_is_visible_in_the_queue(self):
        state = State(Path(self.settings['state']))
        Failures(state).record('NET-12', 'create', ValueError('unknown'), retryable=False)
        code, output = self.invoke('queue')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)['parked']['NET-12']['reason'], 'outcome_unknown')

    def test_releasing_an_unknown_outcome_is_refused_and_says_so_in_the_exit_code(self):
        state = State(Path(self.settings['state']))
        Failures(state).record('NET-12', 'create', ValueError('unknown'), retryable=False)
        code, output = self.invoke('release', 'NET-12')
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(output)['released'])

    def test_an_exhausted_failure_can_be_released_from_the_command_line(self):
        state = State(Path(self.settings['state']))
        failures = Failures(state)
        for _ in range(20):
            failures.record('NET-12', 'read', OSError('timeout'))
        code, output = self.invoke('release', 'NET-12')
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output)['released'])
        self.assertEqual(Failures(State(Path(self.settings['state']))).parked(), {})

    def test_exact_parked_jira_update_can_be_acknowledged_after_inspection(self):
        state = State(Path(self.settings['state']))
        state.record('NET-12', {'jira_sync': {'comments': {'seen': ['8'], 'pending': {
            'event_id': '9', 'action': 'comment', 'value': 'already verified',
            'operation_id': 'jira-update-x', 'reason': 'test', 'parked': True}}}})
        code, output = self.invoke('ack-jira-update', 'NET-12', 'comments', '9')
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output)['acknowledged'])
        saved = State(Path(self.settings['state'])).entries()['NET-12']['jira_sync']['comments']
        self.assertEqual(saved, {'seen': ['9']})

    def test_queue_shows_parked_jira_update_without_comment_content(self):
        state = State(Path(self.settings['state']))
        state.record('NET-12', {'jira_sync': {'comments': {'seen': ['8'], 'pending': {
            'event_id': '9', 'action': 'comment', 'value': 'private comment text',
            'operation_id': 'jira-update-x', 'reason': 'test', 'parked': True}}}})
        code, output = self.invoke('queue')
        self.assertEqual(code, 0)
        parsed = json.loads(output)
        self.assertEqual(parsed['jira_to_fireflow']['NET-12'], [{
            'stream': 'comments', 'event_id': '9', 'action': 'comment',
            'operation_id': 'jira-update-x', 'parked': True}])
        self.assertNotIn('private comment text', output)

    def test_jira_update_ack_refuses_wrong_or_unparked_event(self):
        state = State(Path(self.settings['state']))
        state.record('NET-12', {'jira_sync': {'comments': {'seen': [], 'pending': {
            'event_id': '9', 'action': 'comment', 'value': 'retryable',
            'operation_id': 'jira-update-x', 'reason': 'test'}}}})
        code, output = self.invoke('ack-jira-update', 'NET-12', 'comments', '9')
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(output)['acknowledged'])

    def test_status_ack_requires_the_exact_visible_target(self):
        state = State(Path(self.settings['state']))
        state.record('NET-12', {'jira_sync': {'statuses': {'seen': ['8'], 'pending': {
            'event_id': '9', 'action': 'status', 'value': 'resolved',
            'operation_id': 'jira-update-y', 'reason': 'test', 'parked': True}}}})
        code, output = self.invoke('queue')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)['jira_to_fireflow']['NET-12'][0]
                         ['target_status'], 'resolved')
        code, _ = self.invoke('ack-jira-update', 'NET-12', 'statuses', '9')
        self.assertEqual(code, 1)
        code, output = self.invoke('ack-jira-update', 'NET-12', 'statuses', '9',
                                   '--expected-value', 'resolved')
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output)['acknowledged'])

    def test_reconciliation_runs_on_local_receipts_when_jira_is_unreachable(self):
        code, output = self.invoke('reconcile')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.splitlines()[-1]), {'findings': 0, 'healed': 0})

    def test_reconciliation_exits_non_zero_when_it_found_something(self):
        state = State(Path(self.settings['state']))
        Failures(state).record('NET-12', 'create', ValueError('unknown'), retryable=False)
        code, _ = self.invoke('reconcile')
        self.assertEqual(code, 1, 'a timer should be able to notice a finding from the exit code')

    def test_a_state_directory_that_is_not_private_names_itself_in_the_error(self):
        loose = self.root / 'loose'
        loose.mkdir(mode=0o755)
        with self.assertRaises(SystemExit) as caught:
            bus.build(self.settings, loose)
        self.assertIn(str(loose), str(caught.exception))
        self.assertIn('0700', str(caught.exception))

    def test_reading_the_queue_is_not_itself_a_journalled_event(self):
        self.invoke('queue')
        self.assertFalse((self.root / 'state' / 'jira-bus.jsonl').exists(),
                         'looking at the queue changes nothing and belongs in no journal')

    def test_the_journal_lands_in_the_private_directory(self):
        self.invoke('reconcile')
        self.assertTrue((self.root / 'state' / 'jira-bus.jsonl').exists())
        self.assertEqual((self.root / 'state' / 'jira-bus.jsonl').stat().st_mode & 0o077, 0)

    def test_approval_capture_journals_the_canonical_operator_and_issue_id(self):
        self.settings['mapping']['structured'] = {'field': 'customfield_10000'}
        self.config.write_text(json.dumps(self.settings))
        journal = bus.Journal(self.root / 'state')

        @contextlib.contextmanager
        def local_session(*_args, **_kwargs):
            yield None, SimpleNamespace(path=self.root / 'state' / 'jira-sync.json'), None, journal

        ledger = SimpleNamespace(capture=lambda *_args, **_kwargs: {'captured_by': 'alice'})
        with patch.object(bus, 'session', local_session), \
             patch('algosec_jira_bus.approval_gate.ApprovalLedger', return_value=ledger):
            code, _output = self.invoke('capture-approval', '1234', '--operator', ' alice ')
        self.assertEqual(code, 0)
        record = json.loads(journal.path.read_text().splitlines()[-1])
        self.assertEqual(record['kind'], 'approval_captured')
        self.assertIsNone(record['key'])
        self.assertEqual(record['detail'], {'captured_by': 'alice', 'issue_id': '1234'})

    def test_the_field_listing_puts_custom_fields_first_and_filters_by_name(self):
        class Client:
            def fields(self):
                return [{'id': 'summary', 'name': 'Summary', 'custom': False,
                         'schema': {'type': 'string'}},
                        {'id': 'customfield_10051', 'name': 'Access source', 'custom': True,
                         'schema': {'type': 'string'}},
                        {'id': 'customfield_10050', 'name': 'Access action', 'custom': True,
                         'schema': {'type': 'option'}}]
        rows = bus.show_fields(self.settings, client=Client())
        self.assertEqual([row['id'] for row in rows],
                         ['customfield_10050', 'customfield_10051', 'summary'])
        self.assertEqual(rows[0]['type'], 'option')
        narrowed = bus.show_fields(self.settings, like='source', client=Client())
        self.assertEqual([row['id'] for row in narrowed], ['customfield_10051'])
        self.assertEqual([row['id'] for row in bus.show_fields(self.settings, like='10050',
                                                               client=Client())],
                         ['customfield_10050'])

    def test_the_preflight_survives_both_systems_being_unreachable(self):
        # Nothing in this test can reach a network. The point is that the checks that need
        # one fail with a sentence instead of a traceback, and the local ones still run.
        code, output = self.invoke('doctor')
        self.assertEqual(code, 1)
        self.assertIn('failed', output)

    def test_the_preflight_can_report_machine_readably(self):
        code, output = self.invoke('doctor', '--json')
        self.assertEqual(code, 1)
        findings = json.loads(output)
        self.assertTrue(all({'level', 'check', 'section'} <= set(f) for f in findings))

    def test_a_mode_is_required_rather_than_guessed(self):
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                bus.main(['--config', str(self.config)])

    def test_poll_runs_jira_to_fireflow_before_the_existing_mirror(self):
        events = []
        client = object()
        state = SimpleNamespace(entries=lambda: {})

        @contextlib.contextmanager
        def local_session(*_args, **_kwargs):
            yield object(), state, object(), object()

        empty_intake = {'failed': [], 'refused': []}
        empty_reverse = {'failed': [], 'parked': []}
        empty_mirror = {'failed': [], 'parked': []}
        with patch.object(bus, 'session', local_session), \
             patch.object(bus, 'Jira', return_value=client), \
             patch.object(bus, 'run', side_effect=lambda *a, **k: events.append('intake') or empty_intake), \
             patch.object(bus, 'sync_updates', side_effect=lambda *a, **k: events.append('reverse') or empty_reverse), \
             patch.object(bus, 'mirror', side_effect=lambda *a, **k: events.append('mirror') or empty_mirror), \
             patch.object(bus, 'unresolved', return_value=([], [])):
            result = bus.once(self.settings, apply_changes=True)
        self.assertEqual(events, ['intake', 'reverse', 'mirror'])
        self.assertIs(result['jira_to_fireflow'], empty_reverse)


if __name__ == '__main__':
    unittest.main()

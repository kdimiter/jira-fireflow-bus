"""Reconciliation repairs the bus's own state and reports everything else."""
from pathlib import Path
import json
import tempfile
import unittest

from algosec_mcp.fireflow import digest

from algosec_jira_bus.journal import Journal
from algosec_jira_bus.queue import Failures
from algosec_jira_bus.reconcile import operation_for, reconcile
from algosec_jira_bus.sync import State


class Fireflow:
    def __init__(self, directory, statuses=None):
        self.config = {'base_url': 'https://asms.example.test'}
        self.state = Path(directory)
        self.statuses = statuses or {}

    def get(self, identifier):
        value = self.statuses.get(identifier)
        if isinstance(value, Exception):
            raise value
        return {'response': {'status': value}}

    def _name(self, key, suffix):
        return self.state / (digest([self.config['base_url'], operation_for(key)]) + suffix)

    def began(self, key):
        self._name(key, '.started.json').write_text('{}')

    def finished(self, key, identifier=42):
        self.began(key)
        self._name(key, '.result.json').write_text(json.dumps({'id': identifier}))


class Reconcile(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / 'receipts').mkdir()
        self.state = State(self.root / 'state' / 'sync.json')
        self.failures = Failures(self.state)
        self.journal = Journal(self.root / 'state')
        self.fireflow = Fireflow(self.root / 'receipts')

    def tearDown(self):
        self.directory.cleanup()

    def call(self, keys=(), heal=True):
        return reconcile(keys, self.fireflow, self.state, failures=self.failures,
                         heal=heal, journal=self.journal, log=lambda *a: None)

    def findings(self, report):
        return {finding['key']: finding['finding'] for finding in report['findings']}

    def test_structured_intent_recovers_result_after_create_crash(self):
        operation = 'jira-abcdef012345-1234'
        self.state.data['intents']['NET-1'] = {'operation_id': operation, 'jira_issue_id': '1234'}
        self.state.save()
        prefix = digest([self.fireflow.config['base_url'], operation])
        (self.fireflow.state / (prefix + '.started.json')).write_text('{}')
        (self.fireflow.state / (prefix + '.result.json')).write_text(json.dumps({'id': 88}))
        self.failures.record('NET-1', 'create', 'lost response', retryable=False)
        report = self.call()
        restored = self.state.entries()['NET-1']
        self.assertEqual(restored['change_request_id'], 88)
        self.assertEqual(restored['operation_id'], operation)
        self.assertEqual(restored['jira_issue_id'], '1234')
        self.assertIsNone(self.failures.entry('NET-1'))
        self.assertEqual(report['healed'][0]['action'], 'restored')

    def test_structured_started_without_result_is_parked(self):
        operation = 'jira-abcdef012345-1234'
        self.state.data['intents']['NET-1'] = {'operation_id': operation, 'jira_issue_id': '1234'}
        prefix = digest([self.fireflow.config['base_url'], operation])
        (self.fireflow.state / (prefix + '.started.json')).write_text('{}')
        self.assertEqual(self.findings(self.call())['NET-1'], 'unfinished')
        self.assertFalse(self.state.seen('NET-1'))
        self.assertTrue(self.failures.entry('NET-1')['parked'])

    def test_intent_without_receipt_does_not_mark_issue_created(self):
        self.state.data['intents']['NET-1'] = {'operation_id': 'jira-abc-1234', 'jira_issue_id': '1234'}
        self.assertEqual(self.call()['findings'], [])
        self.assertFalse(self.state.seen('NET-1'))

    def test_structured_identity_missing_never_reads_legacy_key_receipt(self):
        self.state.record('NET-1', {'jira_issue_id': '1234'})
        self.fireflow.finished('NET-1', 99)
        self.assertEqual(self.findings(self.call())['NET-1'], 'identity_missing')
        self.assertNotIn('change_request_id', self.state.entries()['NET-1'])

    def test_a_clean_bus_reports_nothing(self):
        self.assertEqual(self.call(['NET-12'])['findings'], [])

    def test_a_request_the_state_file_forgot_is_restored_from_its_receipt(self):
        self.fireflow.finished('NET-12', 42)
        self.fireflow.statuses[42] = 'Plan'
        report = self.call(['NET-12'])
        self.assertEqual(self.findings(report)['NET-12'], 'state_lost')
        self.assertEqual(self.state.entries()['NET-12']['change_request_id'], 42)
        self.assertEqual(report['healed'][0]['action'], 'restored')

    def test_without_heal_it_reports_the_same_thing_and_changes_nothing(self):
        self.fireflow.finished('NET-12', 42)
        report = self.call(['NET-12'], heal=False)
        self.assertEqual(self.findings(report)['NET-12'], 'state_lost')
        self.assertEqual(report['healed'], [])
        self.assertEqual(self.state.entries(), {})

    def test_a_created_request_whose_id_was_never_written_down_is_backfilled(self):
        self.fireflow.finished('NET-12', 77)
        self.state.record('NET-12', {'change_request_id': None, 'status': None})
        report = self.call(['NET-12'])
        self.assertEqual(self.findings(report)['NET-12'], 'id_missing')
        self.assertEqual(self.state.entries()['NET-12']['change_request_id'], 77)

    def test_a_change_that_began_and_never_finished_is_parked_not_repaired(self):
        self.fireflow.began('NET-12')
        report = self.call(['NET-12'])
        self.assertEqual(self.findings(report)['NET-12'], 'unfinished')
        entry = self.failures.entry('NET-12')
        self.assertTrue(entry['parked'])
        self.assertEqual(entry['reason'], 'outcome_unknown')
        self.assertNotIn('NET-12', self.state.entries(),
                         'an unknown outcome is never turned into a recorded request')

    def test_parking_an_unfinished_change_is_not_repeated_on_every_pass(self):
        self.fireflow.began('NET-12')
        self.call(['NET-12'])
        self.call(['NET-12'])
        self.assertEqual(self.failures.entry('NET-12')['attempts'], 1)

    def test_a_request_fireflow_will_not_return_is_reported_but_never_called_missing(self):
        self.fireflow.finished('NET-12', 42)
        self.fireflow.statuses[42] = ValueError('FireFlow unavailable')
        self.state.record('NET-12', {'change_request_id': 42, 'status': 'Plan'})
        report = self.call(['NET-12'])
        self.assertEqual(self.findings(report)['NET-12'], 'unreadable')
        self.assertIn('unreachable', report['findings'][0]['detail'])
        self.assertEqual(self.state.entries()['NET-12']['status'], 'Plan')

    def test_a_status_the_issue_has_not_been_told_yet_is_named_as_drift(self):
        self.fireflow.finished('NET-12', 42)
        self.fireflow.statuses[42] = 'Implement'
        self.state.record('NET-12', {'change_request_id': 42, 'status': 'Plan'})
        report = self.call(['NET-12'])
        self.assertEqual(self.findings(report)['NET-12'], 'drift')
        self.assertEqual(self.state.entries()['NET-12']['status'], 'Plan',
                         'reporting drift is not mirroring it')

    def test_an_entry_with_no_id_and_no_receipt_says_so_rather_than_claiming_a_creation(self):
        self.state.record('NET-12', {'change_request_id': None, 'status': None})
        report = self.call(['NET-12'])
        self.assertEqual(self.findings(report)['NET-12'], 'id_missing')
        self.assertIn('no receipt', report['findings'][0]['detail'])
        self.assertEqual(report['healed'], [])

    def test_a_parked_failure_is_surfaced_even_when_nothing_else_diverged(self):
        self.failures.record('NET-13', 'create', ValueError('unknown'), retryable=False)
        report = self.call([])
        self.assertEqual(self.findings(report)['NET-13'], 'parked')

    def test_issues_known_only_to_the_state_file_are_checked_too(self):
        self.fireflow.finished('NET-12', 42)
        self.fireflow.statuses[42] = 'Plan'
        self.state.record('NET-12', {'change_request_id': 42, 'status': 'Plan'})
        self.assertEqual(self.call([])['findings'], [], 'no keys passed, still checked')

    def test_the_pass_is_journalled(self):
        self.fireflow.began('NET-12')
        self.call(['NET-12'])
        records = [json.loads(line) for line in self.journal.path.read_text().splitlines()]
        summary = [record for record in records if record['kind'] == 'reconcile'][0]
        self.assertEqual(summary['detail']['findings'], 1)
        self.assertEqual(summary['detail']['healed'], 1)


if __name__ == '__main__':
    unittest.main()

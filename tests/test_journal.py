import json
import os
from pathlib import Path
import tempfile
import unittest

from algosec_jira_bus.runtime import Audit
from algosec_jira_bus.journal import KINDS, Journal, Silent


class JournalFile(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.journal = Journal(Path(self.directory.name) / 'state')

    def tearDown(self):
        self.directory.cleanup()

    def lines(self):
        return [json.loads(line) for line in self.journal.path.read_text().splitlines()]

    def test_it_writes_one_json_line_per_event(self):
        self.journal.write('created', 'NET-12', change_request_id=7)
        self.journal.write('skipped', 'NET-13')
        records = self.lines()
        self.assertEqual([record['kind'] for record in records], ['created', 'skipped'])
        self.assertEqual(records[0]['key'], 'NET-12')
        self.assertEqual(records[0]['detail'], {'change_request_id': 7})
        self.assertIsNone(records[1]['detail'])

    def test_it_lives_beside_the_connector_audit_without_overwriting_it(self):
        Audit(self.journal.directory).record('op-1', 'received', {'op': 'fireflow_create'})
        self.journal.write('created', 'NET-12')
        self.assertEqual(self.journal.path.name, 'jira-bus.jsonl')
        self.assertTrue((self.journal.directory / 'audit.jsonl').exists())
        self.assertEqual(len(self.lines()), 1)

    def test_an_unknown_kind_is_recorded_as_invalid_rather_than_echoed(self):
        self.journal.write('<script>whatever', 'NET-12')
        self.assertEqual(self.lines()[0]['kind'], 'invalid')

    def test_a_key_that_is_not_a_jira_key_is_dropped(self):
        for bad in ('', 'not a key', '../../etc/passwd', 'net-12'):
            self.journal.write('skipped', bad)
        self.assertEqual({record['key'] for record in self.lines()}, {None})

    def test_secrets_in_a_detail_are_redacted(self):
        self.journal.write('error', 'NET-12', error='Jira said token=abcd1234 was rejected',
                           password='hunter2')
        detail = self.lines()[0]['detail']
        self.assertNotIn('abcd1234', json.dumps(detail))
        self.assertEqual(detail['password'], '[REDACTED]')

    def test_the_journal_is_private_to_its_owner(self):
        self.journal.write('pass')
        self.assertEqual(self.journal.path.stat().st_mode & 0o077, 0)

    def test_the_file_is_append_only_across_instances(self):
        self.journal.write('pass', None, phase='first')
        Journal(self.journal.directory).write('pass', None, phase='second')
        self.assertEqual([record['detail']['phase'] for record in self.lines()], ['first', 'second'])

    def test_approval_capture_keeps_immutable_id_and_operator(self):
        record = self.journal.write('approval_captured', None, issue_id='1234', captured_by='alice')
        self.assertEqual(record['kind'], 'approval_captured')
        self.assertIsNone(record['key'])
        self.assertEqual(record['detail'], {'issue_id': '1234', 'captured_by': 'alice'})


class Coupling(unittest.TestCase):
    """The journal shares the bus-owned bounded, locked append implementation."""

    def test_the_bus_runtime_offers_the_append_path_the_journal_relies_on(self):
        self.assertTrue(callable(getattr(Audit, '_append', None)),
                        'Audit._append is gone: give Journal its own append or add a public one')
        self.assertTrue(callable(getattr(Audit, '_rotate', None)))


class NoJournal(unittest.TestCase):
    def test_the_silent_journal_accepts_every_call_and_records_nothing(self):
        silent = Silent()
        self.assertIsNone(silent.write('created', 'NET-12', change_request_id=1))
        self.assertIsNone(silent.write('pass'))

    def test_every_kind_the_bus_writes_is_in_the_closed_set(self):
        self.assertIn('parked', KINDS)
        self.assertIn('retry', KINDS)
        self.assertIn('approval_captured', KINDS)


if __name__ == '__main__':
    unittest.main()

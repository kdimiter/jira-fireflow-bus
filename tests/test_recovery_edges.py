"""Recovery must use consistent positive IDs and preserve unfinished work."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from algosec_jira_bus.queue import BASE, Failures
from algosec_jira_bus.reconcile import reconcile
from algosec_jira_bus.sync import State, mirror
from test_resilience import Clock, Fireflow, Jira, SETTINGS
from test_reconcile import Fireflow as ReceiptFireflow


class RecoveryEdges(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / 'receipts').mkdir()
        self.state = State(self.root / 'state' / 'sync.json')
        self.clock = Clock()
        self.failures = Failures(self.state, now=self.clock)
        self.receipts = ReceiptFireflow(self.root / 'receipts')

    def park_create(self, key='NET-12'):
        self.failures.record(key, 'create', OSError('outcome unknown'), retryable=False)

    def reconcile(self):
        return reconcile([], self.receipts, self.state, failures=self.failures,
                         log=lambda *a: None)

    def test_matching_receipt_clears_lingering_create_failure_with_existing_id(self):
        self.state.record('NET-12', {'change_request_id': 42, 'status': 'Plan'})
        self.receipts.finished('NET-12', 42)
        self.receipts.statuses[42] = 'Plan'
        self.park_create()
        self.reconcile()
        self.assertIsNone(self.failures.entry('NET-12'))
        self.assertEqual(self.state.entries()['NET-12']['change_request_id'], 42)

    def test_invalid_receipt_ids_never_unblock_create(self):
        for index, identifier in enumerate([-1, 0, True, False, 'not-an-id', None]):
            key = 'NET-%s' % (index + 12)
            self.receipts.finished(key, identifier)
            self.park_create(key)
        self.reconcile()
        for index in range(6):
            key = 'NET-%s' % (index + 12)
            with self.subTest(key=key):
                self.assertTrue(self.failures.blocked(key), 'invalid ID is not proof of recovery')

    def test_conflicting_receipt_and_state_ids_do_not_unblock_or_overwrite(self):
        self.state.record('NET-12', {'change_request_id': 99, 'status': 'Plan'})
        self.receipts.finished('NET-12', 42)
        self.park_create()
        self.reconcile()
        self.assertTrue(self.failures.blocked('NET-12'))
        self.assertEqual(self.state.entries()['NET-12']['change_request_id'], 99)

    def test_empty_success_receipt_cannot_unblock_create(self):
        self.receipts.began('NET-12')
        self.receipts._name('NET-12', '.result.json').write_text(
            '{"status":"Success","messages":[],"data":{}}')
        self.park_create()
        self.reconcile()
        self.assertTrue(self.failures.blocked('NET-12'))
        self.assertIsNone(self.state.entries().get('NET-12', {}).get('change_request_id'))

    def test_legacy_transition_without_mapping_stays_pending_before_new_status(self):
        self.state.record('NET-12', {'change_request_id': 42, 'status': 'Resolved'})
        self.failures.record('NET-12', 'transition', OSError('old queue format'))
        self.clock.advance(BASE + 1)
        settings = deepcopy(SETTINGS)
        settings['mirror']['transitions'] = {}
        fireflow = Fireflow(self.root / 'receipts', statuses={42: 'Audit'})
        jira = Jira()
        mirror(settings, fireflow, self.state, jira=jira, dry_run=False,
               failures=self.failures, log=lambda *a: None)
        self.assertEqual(jira.comments, [], 'new status cannot erase an unresolved legacy transition')
        self.assertIsNotNone(self.failures.entry('NET-12'))
        self.assertEqual(self.state.entries()['NET-12']['status'], 'Resolved')

    def test_durable_pending_target_survives_mapping_change(self):
        self.state.record('NET-12', {'change_request_id': 42, 'status': 'Resolved',
                                     'pending_transition': 'Done'})
        self.failures.record('NET-12', 'transition', OSError('temporary failure'))
        self.clock.advance(BASE + 1)
        settings = deepcopy(SETTINGS)
        settings['mirror']['transitions']['Resolved'] = 'Different target'
        jira = Jira()
        mirror(settings, Fireflow(self.root / 'receipts', statuses={42: 'Resolved'}),
               self.state, jira=jira, dry_run=False, failures=self.failures, log=lambda *a: None)
        self.assertEqual(jira.transitions, [('NET-12', 'Done')])
        self.assertEqual(jira.comments, [])


if __name__ == '__main__':
    unittest.main()

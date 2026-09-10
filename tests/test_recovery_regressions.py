"""Recovery contracts at persisted state and observable Jira side effects."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from algosec_jira_bus.journal import Journal
from algosec_jira_bus.queue import BASE, Failures
from algosec_jira_bus.reconcile import reconcile
from algosec_jira_bus.sync import State, mirror
from test_resilience import Clock, Fireflow, Jira, SETTINGS
from test_reconcile import Fireflow as ReceiptFireflow


class RecoveryRegressions(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / 'receipts').mkdir()
        self.state = State(self.root / 'state' / 'sync.json')
        self.clock = Clock()
        self.failures = Failures(self.state, now=self.clock)
        self.journal = Journal(self.root / 'state')

    def seed(self, status=None):
        self.state.record('NET-12', {'change_request_id': 42, 'status': status})

    def mirror(self, fireflow, jira, dry_run=False, settings=SETTINGS):
        return mirror(settings, fireflow, self.state, jira=jira, dry_run=dry_run,
                      failures=self.failures, journal=self.journal, log=lambda *a: None)

    def reload(self):
        self.state = State(self.state.path)
        self.failures = Failures(self.state, now=self.clock)

    def test_failed_transition_retries_after_backoff_and_restart_without_duplicate_comment(self):
        self.seed()
        fireflow = Fireflow(self.root / 'receipts', statuses={42: 'Resolved'})
        jira = Jira(fail_transition=OSError('temporarily unavailable'))
        self.mirror(fireflow, jira)
        self.assertEqual(len(jira.comments), 1)
        self.assertEqual(self.failures.entry('NET-12')['stage'], 'transition')
        jira.fail_transition = None
        self.assertEqual(self.mirror(fireflow, jira)['deferred'], ['NET-12'])
        self.assertEqual(jira.transitions, [])
        self.clock.advance(BASE + 1)
        self.reload()
        self.mirror(fireflow, jira)
        self.assertEqual(jira.transitions, [('NET-12', 'Done')])
        self.assertEqual(len(jira.comments), 1)
        self.assertIsNone(self.failures.entry('NET-12'))
        self.mirror(fireflow, jira)
        self.assertEqual(jira.transitions, [('NET-12', 'Done')])

    def test_pending_transition_finishes_before_new_status_is_reported(self):
        self.seed()
        settings = deepcopy(SETTINGS)
        settings['mirror']['transitions']['Audit'] = 'Audited'
        fireflow = Fireflow(self.root / 'receipts', statuses={42: 'Resolved'})
        self.mirror(fireflow, Jira(fail_transition=OSError('unavailable')), settings=settings)
        self.clock.advance(BASE + 1)
        self.reload()
        fireflow.statuses[42] = 'Audit'
        events = []

        class OrderedJira(Jira):
            def transition(self, key, name):
                events.append(('transition', name))
                super().transition(key, name)

            def comment(self, key, text):
                events.append(('comment', text))
                super().comment(key, text)

        jira = OrderedJira()
        # Implementations may finish the old operation and defer reading until next pass.
        self.mirror(fireflow, jira, settings=settings)
        self.mirror(fireflow, jira, settings=settings)
        self.assertTrue(events)
        self.assertEqual(events[0], ('transition', 'Done'))
        self.assertEqual(jira.transitions, [('NET-12', 'Done'), ('NET-12', 'Audited')])
        self.assertEqual(len(jira.comments), 1)
        self.assertIn('Audit', jira.comments[0][1])

    def test_dry_run_read_error_preserves_persisted_issues_and_failures(self):
        self.seed('Plan')
        self.failures.record('NET-13', 'read', OSError('existing failure'))
        before = self.state.path.read_bytes()
        in_memory = deepcopy(self.state.data)
        fireflow = Fireflow(self.root / 'receipts', statuses={42: OSError('read timeout')})
        jira = Jira()
        self.mirror(fireflow, jira, dry_run=True)
        self.assertEqual(self.state.path.read_bytes(), before)
        self.assertEqual(self.state.data, in_memory)
        self.assertEqual(jira.comments, [])
        self.assertEqual(jira.transitions, [])

    def test_successful_same_status_read_clears_previous_read_failure(self):
        self.seed('Plan')
        fireflow = Fireflow(self.root / 'receipts', statuses={42: OSError('timeout')})
        self.mirror(fireflow, Jira())
        self.clock.advance(BASE + 1)
        self.reload()
        fireflow.statuses[42] = 'plan'
        jira = Jira()
        self.mirror(fireflow, jira)
        self.assertIsNone(self.failures.entry('NET-12'))
        self.assertEqual(json.loads(self.state.path.read_text())['failures'], {})
        self.assertEqual(jira.comments, [])
        self.assertEqual(jira.transitions, [])

    def test_receipt_recovery_clears_create_unknown_failure(self):
        fireflow = ReceiptFireflow(self.root / 'receipts')
        fireflow.finished('NET-12', 42)
        self.failures.record('NET-12', 'create', OSError('outcome unknown'), retryable=False)
        reconcile(['NET-12'], fireflow, self.state, failures=self.failures,
                  journal=self.journal, log=lambda *a: None)
        self.reload()
        self.assertEqual(self.state.entries()['NET-12']['change_request_id'], 42)
        self.assertIsNone(self.failures.entry('NET-12'))
        self.assertFalse(self.failures.blocked('NET-12'))

    def test_receipt_recovery_preserves_unrelated_failures(self):
        fireflow = ReceiptFireflow(self.root / 'receipts')
        for key, stage in [('NET-12', 'transition'), ('NET-13', 'read')]:
            fireflow.finished(key, 42)
            self.failures.record(key, stage, OSError('unrelated failure'))
        before = deepcopy(self.failures.store)
        reconcile(['NET-12', 'NET-13'], fireflow, self.state, failures=self.failures,
                  journal=self.journal, log=lambda *a: None)
        self.reload()
        self.assertEqual(self.failures.store, before)
        self.assertEqual(set(self.state.entries()), {'NET-12', 'NET-13'})

    def test_reconcile_recovers_failure_only_key_without_jira(self):
        fireflow = ReceiptFireflow(self.root / 'receipts')
        fireflow.finished('NET-12', 42)
        self.failures.record('NET-12', 'create', 'unknown', retryable=False)
        reconcile([], fireflow, self.state, failures=self.failures, log=lambda *a: None)
        self.assertEqual(self.state.entries()['NET-12']['change_request_id'], 42)
        self.assertIsNone(self.failures.entry('NET-12'))

    def test_dry_run_does_not_clear_recovered_read_failure(self):
        self.seed('Plan')
        self.failures.record('NET-12', 'read', 'temporary')
        self.clock.advance(BASE + 1)
        before = self.state.path.read_bytes()
        self.mirror(Fireflow(self.root / 'receipts', statuses={42: 'Plan'}), Jira(), dry_run=True)
        self.assertEqual(self.state.path.read_bytes(), before)

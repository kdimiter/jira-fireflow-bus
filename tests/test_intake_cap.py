"""One pass must not submit everything a mis-scoped query matched.

Reading every page fixed the starvation, and took the safety valve with it: `limit` used to
bound how many issues a pass handled, and now bounds only the page size. Anything over the
cap has to be *held*, never dropped -- the next pass is five minutes away, and an issue the
bus silently forgot is the failure mode this whole queue exists to prevent.
"""
from pathlib import Path
import tempfile
import unittest

from algosec_jira_bus.journal import Journal
from algosec_jira_bus.queue import Failures
from algosec_jira_bus.sync import DEFAULT_MAX_PER_PASS, MappingError, State, run

from test_resilience import Fireflow, Jira, SETTINGS, issue


class Cap(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / 'receipts').mkdir()
        self.state = State(self.root / 'state' / 'sync.json')
        self.failures = Failures(self.state)
        self.journal = Journal(self.root / 'state')

    def poll(self, settings, issues, fireflow=None):
        return run(settings, fireflow or Fireflow(self.root / 'receipts'), self.state,
                   jira=Jira(issues), dry_run=False, log=lambda *a: None,
                   failures=self.failures, journal=self.journal)

    def issues(self, count):
        return [issue('NET-%d' % n) for n in range(1, count + 1)]

    def test_submissions_stop_at_the_cap_and_the_rest_are_held(self):
        settings = dict(SETTINGS, max_per_pass=2)
        result = self.poll(settings, self.issues(5))
        self.assertEqual([key for key, _ in result['created']], ['NET-1', 'NET-2'])
        self.assertEqual(result['capped'], ['NET-3', 'NET-4', 'NET-5'])

    def test_what_was_held_is_submitted_by_the_next_pass(self):
        settings = dict(SETTINGS, max_per_pass=2)
        self.poll(settings, self.issues(5))
        second = self.poll(settings, self.issues(5))
        self.assertEqual([key for key, _ in second['created']], ['NET-3', 'NET-4'])
        self.assertEqual(second['skipped'], ['NET-1', 'NET-2'])
        third = self.poll(settings, self.issues(5))
        self.assertEqual([key for key, _ in third['created']], ['NET-5'])
        self.assertEqual(third['capped'], [])

    def test_nothing_is_ever_lost_to_the_cap(self):
        settings = dict(SETTINGS, max_per_pass=2)
        for _ in range(4):
            self.poll(settings, self.issues(5))
        self.assertEqual(sorted(self.state.entries()), ['NET-%d' % n for n in range(1, 6)])

    def test_a_refused_issue_does_not_consume_the_cap(self):
        # A refusal never reaches FireFlow, so counting it would hold back real work for
        # nothing. Two bad issues must not stop two good ones from being submitted.
        settings = dict(SETTINGS, max_per_pass=2)
        issues = [issue('NET-1', action='Nonsense'), issue('NET-2', action='Nonsense'),
                  issue('NET-3'), issue('NET-4'), issue('NET-5')]
        result = self.poll(settings, issues)
        self.assertEqual([key for key, _ in result['created']], ['NET-3', 'NET-4'])
        self.assertEqual([key for key, _ in result['refused']], ['NET-1', 'NET-2'])

    def test_the_cap_applies_in_dry_run_so_it_predicts_the_real_pass(self):
        settings = dict(SETTINGS, max_per_pass=2)
        result = run(settings, Fireflow(self.root / 'receipts'), self.state,
                     jira=Jira(self.issues(5)), dry_run=True, log=lambda *a: None,
                     failures=self.failures, journal=self.journal)
        self.assertEqual(len(result['created']), 2)
        self.assertEqual(len(result['capped']), 3)

    def test_the_default_is_a_cap_rather_than_no_cap(self):
        self.assertEqual(DEFAULT_MAX_PER_PASS, 50)
        result = self.poll(SETTINGS, self.issues(DEFAULT_MAX_PER_PASS + 3))
        self.assertEqual(len(result['created']), DEFAULT_MAX_PER_PASS)
        self.assertEqual(len(result['capped']), 3)

    def test_the_cap_can_be_lifted_deliberately(self):
        for lifted in (0, None):
            with self.subTest(value=lifted):
                self.setUp()
                result = self.poll(dict(SETTINGS, max_per_pass=lifted), self.issues(60))
                self.assertEqual(len(result['created']), 60)
                self.assertEqual(result['capped'], [])

    def test_a_nonsense_cap_is_refused_rather_than_guessed(self):
        for bad in ('50', -1, 1.5, True):
            with self.subTest(value=bad):
                with self.assertRaises(MappingError):
                    self.poll(dict(SETTINGS, max_per_pass=bad), self.issues(1))

    def test_any_blocked_legacy_intent_alias_defers_the_same_immutable_issue(self):
        candidate = issue('NET-99')
        candidate['id'] = '1234'
        self.state.data['intents'] = {
            'OLD-1': {'jira_issue_id': '1234'},
            'OLD-2': {'jira_issue_id': '1234'},
        }
        self.failures.record('OLD-1', 'create', RuntimeError('unknown'), retryable=False)
        result = self.poll(dict(SETTINGS, max_per_pass=1), [candidate])
        self.assertEqual(result['deferred'], ['NET-99'])


if __name__ == '__main__':
    unittest.main()

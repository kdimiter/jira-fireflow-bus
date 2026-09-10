"""One failing issue must not lose the event, end the pass, or repeat a change."""
from pathlib import Path
import tempfile
import unittest

from algosec_jira_bus.journal import Journal
from algosec_jira_bus.queue import ATTEMPTS, BASE, Failures
from algosec_jira_bus.sync import State, mirror, run


MAPPING = {'action': {'field': 'cf_action', 'values': {'Open': 'Allow', 'Close': 'Drop'}},
           'source': {'field': 'cf_src'}, 'destination': {'field': 'cf_dst'},
           'service': {'field': 'cf_svc'}}
SETTINGS = {'jira': {'jql': 'project = NET'},
            'mapping': MAPPING,
            'fireflow': {'template': 'Traffic Change Request', 'devices': ['fw1']},
            'mirror': {'transitions': {'Resolved': 'Done'}}}


def issue(key='NET-12', action='Open'):
    return {'key': key, 'fields': {'summary': 'Open access', 'cf_action': action,
                                   'cf_src': '192.0.2.1', 'cf_dst': '192.0.2.2', 'cf_svc': 'tcp/443'}}


class Clock:
    def __init__(self, now=1000.0): self.now = now
    def __call__(self): return self.now
    def advance(self, seconds): self.now += seconds


class Jira:
    def __init__(self, issues=(), fail_search=None, fail_comment=None, fail_transition=None):
        self.issues = list(issues)
        self.fail_search, self.fail_comment, self.fail_transition = fail_search, fail_comment, fail_transition
        self.comments, self.transitions = [], []

    def search(self, jql, fields, limit=50):
        if self.fail_search:
            raise self.fail_search
        return self.issues

    def comment(self, key, text):
        if self.fail_comment:
            raise self.fail_comment
        self.comments.append((key, text))

    def transition(self, key, name):
        if self.fail_transition:
            raise self.fail_transition
        self.transitions.append((key, name))


class Fireflow:
    """A fake adapter that can fail, and can leave the receipt a real one would."""

    def __init__(self, directory, fail_create=None, leaves_receipt=False, statuses=None):
        self.config = {'base_url': 'https://asms.example.test'}
        self.state = Path(directory)
        self.fail_create, self.leaves_receipt = fail_create, leaves_receipt
        self.statuses = statuses or {}
        self.created, self.reads = [], []

    def create(self, request, operation_id, reason):
        if self.fail_create:
            if self.leaves_receipt:
                from algosec_mcp.fireflow import digest
                (self.state / (digest([self.config['base_url'], operation_id]) + '.started.json')).write_text('{}')
            raise self.fail_create
        self.created.append(operation_id)
        return {'operation_id': operation_id, 'receipt': 'r1', 'response': {'id': 42}}

    def get(self, identifier):
        self.reads.append(identifier)
        value = self.statuses.get(identifier)
        if isinstance(value, Exception):
            raise value
        return {'response': {'status': value}}


class Base(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.state = State(self.root / 'state' / 'sync.json')
        self.clock = Clock()
        self.failures = Failures(self.state, now=self.clock)
        self.journal = Journal(self.root / 'state')
        (self.root / 'receipts').mkdir()

    def tearDown(self):
        self.directory.cleanup()

    def kinds(self):
        import json
        return [json.loads(line)['kind'] for line in self.journal.path.read_text().splitlines()]

    def call_run(self, fireflow, jira, dry_run=False):
        return run(SETTINGS, fireflow, self.state, jira=jira, dry_run=dry_run, log=lambda *a: None,
                   failures=self.failures, journal=self.journal)

    def call_mirror(self, fireflow, jira, dry_run=False):
        return mirror(SETTINGS, fireflow, self.state, jira=jira, dry_run=dry_run, log=lambda *a: None,
                      failures=self.failures, journal=self.journal)


class Intake(Base):
    def test_duplicate_immutable_issue_id_is_only_created_once_per_pass(self):
        old, renamed = issue('OLD-1'), issue('NEW-1')
        old['id'] = renamed['id'] = '1234'
        fireflow = Fireflow(self.root / 'receipts')

        result = self.call_run(fireflow, Jira([old, renamed]))

        self.assertEqual(fireflow.created, ['jira-id-1234'])
        self.assertEqual(result['skipped'], ['NEW-1'])

    def test_a_failed_create_does_not_end_the_pass(self):
        fireflow = Fireflow(self.root / 'receipts', fail_create=OSError('connection reset'))
        result = self.call_run(fireflow, Jira([issue('NET-12'), issue('NET-13')]))
        self.assertEqual(result['created'], [])
        self.assertEqual(set(self.failures.pending()), {'NET-12', 'NET-13'},
                         'both issues were attempted; neither event was lost')

    def test_a_create_that_sent_nothing_is_queued_for_another_try(self):
        fireflow = Fireflow(self.root / 'receipts', fail_create=ValueError('Template not allowlisted'))
        self.call_run(fireflow, Jira([issue('NET-12')]))
        entry = self.failures.entry('NET-12')
        self.assertFalse(entry['parked'])
        self.assertIsNotNone(entry['next_attempt'])

    def test_a_create_that_may_have_reached_the_appliance_is_never_retried(self):
        fireflow = Fireflow(self.root / 'receipts', fail_create=ValueError('outcome may be unknown'),
                            leaves_receipt=True)
        self.call_run(fireflow, Jira([issue('NET-12')]))
        entry = self.failures.entry('NET-12')
        self.assertTrue(entry['parked'])
        self.assertEqual(entry['reason'], 'outcome_unknown')
        self.clock.advance(10 ** 6)
        self.assertTrue(self.failures.blocked('NET-12'))
        self.assertIn('parked', self.kinds())

    def test_a_parked_issue_is_deferred_rather_than_attempted_again(self):
        broken = Fireflow(self.root / 'receipts', fail_create=ValueError('unknown'), leaves_receipt=True)
        self.call_run(broken, Jira([issue('NET-12')]))
        healthy = Fireflow(self.root / 'receipts')
        result = self.call_run(healthy, Jira([issue('NET-12')]))
        self.assertEqual(result['deferred'], ['NET-12'])
        self.assertEqual(healthy.created, [], 'the adapter was never called again')
        self.assertEqual(result['parked'], ['NET-12'])

    def test_a_queued_issue_is_attempted_again_once_it_is_due(self):
        fireflow = Fireflow(self.root / 'receipts', fail_create=OSError('timeout'))
        self.call_run(fireflow, Jira([issue('NET-12')]))
        self.assertEqual(self.call_run(fireflow, Jira([issue('NET-12')]))['deferred'], ['NET-12'])
        self.clock.advance(BASE * 100)
        healthy = Fireflow(self.root / 'receipts')
        result = self.call_run(healthy, Jira([issue('NET-12')]))
        self.assertEqual([key for key, _ in result['created']], ['NET-12'])
        self.assertIsNone(self.failures.entry('NET-12'), 'success clears the queue entry')

    def test_a_refused_issue_is_not_queued_because_polling_cannot_fix_it(self):
        fireflow = Fireflow(self.root / 'receipts')
        result = self.call_run(fireflow, Jira([issue('NET-12', action='Maybe')]))
        self.assertEqual([key for key, _ in result['refused']], ['NET-12'])
        self.assertEqual(self.failures.pending(), {})
        self.assertIn('refused', self.kinds())

    def test_a_search_failure_is_recorded_and_raised_rather_than_reported_as_an_empty_poll(self):
        fireflow = Fireflow(self.root / 'receipts')
        with self.assertRaises(OSError):
            self.call_run(fireflow, Jira(fail_search=OSError('dns')))
        self.assertIn('error', self.kinds())

    def test_a_successful_create_is_journalled_with_its_receipt(self):
        import json
        self.call_run(Fireflow(self.root / 'receipts'), Jira([issue('NET-12')]))
        records = [json.loads(line) for line in self.journal.path.read_text().splitlines()]
        created = [record for record in records if record['kind'] == 'created']
        self.assertEqual(created[0]['key'], 'NET-12')
        self.assertEqual(created[0]['detail']['change_request_id'], 42)


class Mirroring(Base):
    def seed(self, key='NET-12', identifier=42, status=None):
        self.state.record(key, {'change_request_id': identifier, 'status': status})

    def test_a_read_failure_is_queued_and_the_pass_continues(self):
        self.seed('NET-12', 42)
        self.seed('NET-13', 43)
        fireflow = Fireflow(self.root / 'receipts', statuses={42: OSError('timeout'), 43: 'Plan'})
        jira = Jira()
        result = self.call_mirror(fireflow, jira)
        self.assertEqual([key for key, _ in result['updated']], ['NET-13'])
        self.assertEqual(set(self.failures.pending()), {'NET-12'})

    def test_a_comment_that_did_not_land_leaves_the_status_unrecorded(self):
        self.seed('NET-12', 42)
        fireflow = Fireflow(self.root / 'receipts', statuses={42: 'Plan'})
        self.call_mirror(fireflow, Jira(fail_comment=OSError('timeout')))
        self.assertIsNone(self.state.entries()['NET-12']['status'],
                          'the next attempt must send the comment, not skip it')
        self.clock.advance(BASE * 10)
        healthy = Jira()
        self.call_mirror(fireflow, healthy)
        self.assertEqual([key for key, _ in healthy.comments], ['NET-12'])

    def test_a_failed_transition_keeps_the_status_so_the_comment_is_never_doubled(self):
        self.seed('NET-12', 42)
        fireflow = Fireflow(self.root / 'receipts', statuses={42: 'Resolved'})
        jira = Jira(fail_transition=ValueError('No transition named'))
        self.call_mirror(fireflow, jira)
        self.assertEqual(self.state.entries()['NET-12']['status'], 'Resolved')
        self.assertEqual(len(jira.comments), 1)
        self.assertEqual(set(self.failures.pending()), {'NET-12'})
        self.clock.advance(BASE * 10)
        again = Jira(fail_transition=ValueError('No transition named'))
        self.call_mirror(fireflow, again)
        self.assertEqual(again.comments, [], 'the status was already reported')

    def test_a_stuck_issue_stops_marching_through_statuses(self):
        self.seed('NET-12', 42)
        fireflow = Fireflow(self.root / 'receipts', statuses={42: OSError('timeout')})
        self.call_mirror(fireflow, Jira())
        fireflow.statuses[42] = 'Implement'
        result = self.call_mirror(fireflow, Jira())
        self.assertEqual(result['deferred'], ['NET-12'])
        self.assertEqual(result['updated'], [])

    def test_a_successful_mirror_clears_an_earlier_failure(self):
        self.seed('NET-12', 42)
        fireflow = Fireflow(self.root / 'receipts', statuses={42: OSError('timeout')})
        self.call_mirror(fireflow, Jira())
        self.clock.advance(BASE * 10)
        fireflow.statuses[42] = 'Plan'
        self.call_mirror(fireflow, Jira())
        self.assertIsNone(self.failures.entry('NET-12'))

    def test_repeated_read_failures_eventually_park_the_issue_for_a_person(self):
        self.seed('NET-12', 42)
        fireflow = Fireflow(self.root / 'receipts', statuses={42: OSError('timeout')})
        for _ in range(ATTEMPTS):
            self.clock.advance(10 ** 5)
            self.call_mirror(fireflow, Jira())
        self.assertEqual(set(self.failures.parked()), {'NET-12'})
        self.assertEqual(self.failures.entry('NET-12')['reason'], 'attempts_exhausted')

    def test_a_dry_run_changes_nothing_and_queues_nothing(self):
        self.seed('NET-12', 42)
        fireflow = Fireflow(self.root / 'receipts', statuses={42: 'Plan'})
        jira = Jira()
        result = self.call_mirror(fireflow, jira, dry_run=True)
        self.assertEqual([key for key, _ in result['updated']], ['NET-12'])
        self.assertEqual(jira.comments, [])
        self.assertIsNone(self.state.entries()['NET-12']['status'])


if __name__ == '__main__':
    unittest.main()

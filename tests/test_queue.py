import json
from pathlib import Path
import tempfile
import unittest

from algosec_jira_bus.queue import (ATTEMPTS, BASE, CAP, Failures, SLOW, delay,
                                    sent_anything, started_receipt)
from algosec_jira_bus.sync import State


class Clock:
    def __init__(self, now=1000.0): self.now = now
    def __call__(self): return self.now
    def advance(self, seconds): self.now += seconds


class Fireflow:
    """Enough of the adapter for the receipt check: a config and a state directory."""
    def __init__(self, directory, base_url='https://asms.example.test'):
        self.config = {'base_url': base_url}
        self.state = Path(directory)


def receipt_for(fireflow, operation_id):
    from algosec_mcp.fireflow import digest
    return fireflow.state / (digest([fireflow.config['base_url'], operation_id]) + '.started.json')


class Backoff(unittest.TestCase):
    def test_the_first_retry_waits_the_base_interval_and_then_doubles(self):
        self.assertEqual(delay(1), BASE)
        self.assertEqual(delay(2), BASE * 2)
        self.assertEqual(delay(3), BASE * 4)

    def test_the_wait_never_grows_past_the_cap(self):
        self.assertEqual(delay(50), CAP)

    def test_a_refused_create_is_retried_on_the_slow_schedule_only(self):
        self.assertEqual(delay(1, slow=True), SLOW)
        self.assertEqual(delay(6, slow=True), SLOW)


class Receipt(unittest.TestCase):
    """The signal that decides whether a failed change may ever be tried again."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.fireflow = Fireflow(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def test_no_receipt_means_nothing_was_sent(self):
        self.assertFalse(sent_anything(self.fireflow, 'jira_NET_12'))

    def test_a_receipt_means_the_outcome_is_unknown(self):
        receipt_for(self.fireflow, 'jira_NET_12').write_text('{}')
        self.assertTrue(sent_anything(self.fireflow, 'jira_NET_12'))

    def test_the_receipt_is_scoped_to_the_server_and_the_operation(self):
        one = started_receipt(self.fireflow, 'jira_NET_12')
        other = started_receipt(Fireflow(self.directory.name, 'https://other.example.test'),
                                'jira_NET_12')
        self.assertNotEqual(one, other)
        self.assertNotEqual(one, started_receipt(self.fireflow, 'jira_NET_13'))

    def test_an_adapter_we_cannot_question_is_treated_as_outcome_unknown(self):
        class Opaque:
            config = {}
        self.assertTrue(sent_anything(Opaque(), 'jira_NET_12'),
                        'without proof that nothing was sent, never retry')


class Queue(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = State(Path(self.directory.name) / 'state' / 'sync.json')
        self.clock = Clock()
        self.queue = Failures(self.state, now=self.clock)

    def tearDown(self):
        self.directory.cleanup()

    def test_a_failure_blocks_the_issue_until_it_is_due(self):
        self.queue.record('NET-12', 'mirror', OSError('timeout'))
        self.assertTrue(self.queue.blocked('NET-12'))
        self.clock.advance(BASE + 1)
        self.assertFalse(self.queue.blocked('NET-12'))

    def test_an_issue_with_no_failure_is_never_blocked(self):
        self.assertFalse(self.queue.blocked('NET-99'))

    def test_repeated_failures_wait_longer_each_time(self):
        waits = []
        for _ in range(3):
            entry = self.queue.record('NET-12', 'mirror', OSError('timeout'))
            waits.append(entry['next_attempt'] - self.clock())
        self.assertEqual(waits, [BASE, BASE * 2, BASE * 4])

    def test_the_queue_gives_up_and_parks_after_the_attempt_limit(self):
        for _ in range(ATTEMPTS):
            entry = self.queue.record('NET-12', 'mirror', OSError('timeout'))
        self.assertTrue(entry['parked'])
        self.assertEqual(entry['reason'], 'attempts_exhausted')
        self.assertIsNone(entry['next_attempt'])
        self.clock.advance(CAP * 100)
        self.assertTrue(self.queue.blocked('NET-12'), 'a parked issue stays blocked by time')

    def test_an_unknown_outcome_is_parked_at_once_and_never_scheduled(self):
        entry = self.queue.record('NET-12', 'intake', ValueError('outcome may be unknown'),
                                  retryable=False)
        self.assertTrue(entry['parked'])
        self.assertEqual(entry['reason'], 'outcome_unknown')
        self.assertIsNone(entry['next_attempt'])
        self.assertEqual(entry['attempts'], 1)

    def test_a_parked_unknown_outcome_cannot_be_released_by_the_bus(self):
        self.queue.record('NET-12', 'intake', ValueError('unknown'), retryable=False)
        self.assertFalse(self.queue.release('NET-12'))
        self.assertTrue(self.queue.blocked('NET-12'))

    def test_an_exhausted_entry_can_be_released_once_a_person_has_looked(self):
        for _ in range(ATTEMPTS):
            self.queue.record('NET-12', 'mirror', OSError('timeout'))
        self.assertTrue(self.queue.release('NET-12'))
        self.assertFalse(self.queue.blocked('NET-12'))

    def test_success_clears_the_entry(self):
        self.queue.record('NET-12', 'mirror', OSError('timeout'))
        self.assertTrue(self.queue.clear('NET-12'))
        self.assertFalse(self.queue.blocked('NET-12'))
        self.assertFalse(self.queue.clear('NET-12'))

    def test_parked_and_pending_are_reported_separately(self):
        self.queue.record('NET-12', 'mirror', OSError('timeout'))
        self.queue.record('NET-13', 'intake', ValueError('unknown'), retryable=False)
        self.assertEqual(set(self.queue.pending()), {'NET-12'})
        self.assertEqual(set(self.queue.parked()), {'NET-13'})

    def test_the_queue_survives_a_restart_in_the_same_file_as_the_issues(self):
        self.state.record('NET-11', {'status': 'Plan'})
        self.queue.record('NET-12', 'mirror', OSError('timeout'))
        stored = json.loads(self.state.path.read_text())
        self.assertIn('NET-11', stored['issues'])
        self.assertIn('NET-12', stored['failures'])
        again = Failures(State(self.state.path), now=self.clock)
        self.assertTrue(again.blocked('NET-12'))
        self.assertEqual(again.entry('NET-12')['attempts'], 1)

    def test_a_state_file_written_before_the_queue_existed_still_loads(self):
        path = Path(self.directory.name) / 'state' / 'old.json'
        path.write_text(json.dumps({'issues': {'NET-1': {'status': 'Plan'}}}))
        state = State(path)
        self.assertEqual(state.entries(), {'NET-1': {'status': 'Plan'}})
        self.assertEqual(Failures(state).pending(), {})

    def test_the_message_is_kept_but_bounded(self):
        entry = self.queue.record('NET-12', 'mirror', OSError('x' * 900))
        self.assertEqual(entry['error'], 'OSError')
        self.assertEqual(len(entry['message']), 500)


if __name__ == '__main__':
    unittest.main()

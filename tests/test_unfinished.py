"""A pass that left work undone must not report success.

Two of these are silences rather than errors, and those are the dangerous ones: a
transition still owed leaves a card half-moved, and a request created without an id can
never be mirrored back to whoever asked for it. Neither raises anything. Without an exit
code that notices them, a timer unit goes green over a bus that has quietly stopped
doing half its job.
"""
import io
import json
from pathlib import Path
import contextlib
import tempfile
import unittest

from algosec_jira_bus import bus
from algosec_jira_bus.sync import State

from test_bus import settings_for


class Unresolved(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.root.chmod(0o700)
        (self.root / 'state').mkdir(mode=0o700)
        self.state = State(self.root / 'state' / 'sync.json')

    def test_a_transition_still_owed_is_reported_as_pending(self):
        self.state.record('NET-12', {'change_request_id': 42, 'pending_transition': 'Done'})
        self.state.record('NET-13', {'change_request_id': 43, 'pending_transition': None})
        pending, missing = bus.unresolved(self.state)
        self.assertEqual(pending, ['NET-12'])
        self.assertEqual(missing, [])

    def test_a_created_request_with_no_id_is_reported_as_missing(self):
        self.state.record('NET-12', {'operation_id': 'jira-NET_12', 'change_request_id': None})
        self.state.record('NET-13', {'operation_id': 'jira-NET_13', 'change_request_id': 43})
        pending, missing = bus.unresolved(self.state)
        self.assertEqual(missing, ['NET-12'])
        self.assertEqual(pending, [])

    def test_an_issue_never_submitted_is_not_counted_as_missing(self):
        self.state.record('NET-12', {'status': 'plan'})
        self.assertEqual(bus.unresolved(self.state), ([], []))


class ExitCode(unittest.TestCase):
    def clean(self):
        return {'intake': {'created': [], 'skipped': [], 'refused': [], 'failed': [],
                           'deferred': [], 'capped': [], 'parked': []},
                'mirror': {'updated': [], 'unchanged': [], 'deferred': [], 'failed': [],
                           'parked': []},
                'pending': [], 'missing_ids': []}

    def test_a_pass_with_nothing_outstanding_is_clean(self):
        self.assertEqual(bus.outstanding(self.clean()), [])

    def test_every_kind_of_unfinished_work_is_counted(self):
        for section, name in bus.UNFINISHED:
            with self.subTest(section=section, name=name):
                summary = self.clean()
                (summary[section] if section else summary)[name] = ['NET-1']
                self.assertEqual(bus.outstanding(summary), ['NET-1'])

    def test_work_merely_deferred_or_capped_is_not_a_failure(self):
        # Both are held on purpose and the next pass takes them. Treating them as failures
        # would make a healthy busy bus look broken every five minutes.
        for name in ('deferred', 'capped', 'skipped'):
            with self.subTest(name=name):
                summary = self.clean()
                summary['intake'][name] = ['NET-1']
                self.assertEqual(bus.outstanding(summary), [])

    def test_a_summary_missing_sections_does_not_crash(self):
        self.assertEqual(bus.outstanding({}), [])
        self.assertEqual(bus.outstanding({'intake': None, 'mirror': None}), [])


class ApplyMode(unittest.TestCase):
    class Args:
        def __init__(self, apply=False, dry_run=False):
            self.apply, self.dry_run = apply, dry_run

    def test_the_configuration_carries_the_mode_across_an_upgrade(self):
        self.assertTrue(bus.apply_mode({'apply': True}, self.Args()))
        self.assertFalse(bus.apply_mode({'apply': False}, self.Args()))
        self.assertFalse(bus.apply_mode({}, self.Args()))

    def test_the_command_line_overrides_the_configuration_in_both_directions(self):
        self.assertTrue(bus.apply_mode({}, self.Args(apply=True)))
        self.assertFalse(bus.apply_mode({'apply': True}, self.Args(dry_run=True)))

    def test_dry_run_wins_over_apply_so_the_safe_answer_is_never_the_loser(self):
        self.assertFalse(bus.apply_mode({'apply': True}, self.Args(apply=True, dry_run=True)))

    def test_anything_that_is_not_a_boolean_is_refused(self):
        for bad in ('false', 'true', 1, 0, None, [], 'yes'):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    bus.apply_mode({'apply': bad}, self.Args())

    def test_apply_and_dry_run_cannot_be_asked_for_together_on_the_command_line(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            (root / 'state').mkdir(mode=0o700)
            config = root / 'bus.json'
            config.write_text(json.dumps(settings_for(root)))
            config.chmod(0o600)
            with self.assertRaises(SystemExit) as caught:
                with contextlib.redirect_stderr(io.StringIO()):
                    bus.main(['--config', str(config), 'poll', '--apply', '--dry-run'])
            self.assertEqual(caught.exception.code, 2)


if __name__ == '__main__':
    unittest.main()

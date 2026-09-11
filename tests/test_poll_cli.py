"""The service must retain apply mode across upgrades and expose partial failure."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from algosec_jira_bus import bus


def summary():
    return {'intake': {'created': [], 'skipped': [], 'refused': [], 'deferred': [], 'failed': []},
            'jira_to_fireflow': {'updated': [], 'failed': [], 'parked': []},
            'mirror': {'updated': [], 'parked': [], 'failed': []}, 'pending': [], 'missing_ids': []}


class PollCli(unittest.TestCase):
    def invoke(self, settings, result, *flags):
        with tempfile.TemporaryDirectory() as d:
            config = Path(d) / 'bus.json'
            config.write_text(json.dumps(settings)); config.chmod(0o600)
            with patch.object(bus, 'once', return_value=result) as once, contextlib.redirect_stdout(io.StringIO()):
                code = bus.main(['--config', str(config), 'poll', *flags])
            return code, once

    def test_configured_apply_is_honored_without_modifying_service(self):
        code, once = self.invoke({'apply': True}, summary())
        self.assertEqual(code, 0)
        self.assertTrue(once.call_args.args[1])

    def test_explicit_dry_run_overrides_configured_apply(self):
        code, once = self.invoke({'apply': True}, summary(), '--dry-run')
        self.assertEqual(code, 0)
        self.assertFalse(once.call_args.args[1])

    def test_false_is_dry_by_default_and_cli_can_enable_apply(self):
        self.assertFalse(self.invoke({'apply': False}, summary())[1].call_args.args[1])
        self.assertTrue(self.invoke({}, summary(), '--apply')[1].call_args.args[1])

    def test_string_apply_is_rejected_instead_of_interpreted_as_truthy(self):
        with self.assertRaises(ValueError):
            self.invoke({'apply': 'false'}, summary())

    def test_partial_failures_and_unresolved_work_are_nonzero(self):
        for section, name in [('intake', 'failed'), ('mirror', 'failed'), ('intake', 'refused'),
                              ('mirror', 'parked'), ('jira_to_fireflow', 'failed'),
                              ('jira_to_fireflow', 'parked'),
                              (None, 'pending'), (None, 'missing_ids')]:
            with self.subTest(section=section, name=name):
                result = summary()
                (result[section] if section else result)[name] = ['NET-1']
                self.assertEqual(self.invoke({}, result)[0], 1)

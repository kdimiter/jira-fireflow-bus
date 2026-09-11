import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('container_setup', Path(__file__).parents[1] / 'scripts/setup_wizard.py')
wizard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wizard)

class ContainerSetupTests(unittest.TestCase):
    def test_bus_conf_exposes_a_jira_to_fireflow_menu(self):
        helper = (Path(__file__).parents[1] / 'packaging/docker/bus_conf').read_text()
        self.assertIn('--jira-sync', helper)
        self.assertIn('MODE=--jira-sync', helper)

    def run_case(self, code, response):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'bus.json'
            secrets = config.parent / 'secrets.json'
            secrets.write_text(json.dumps({'JIRA_API_TOKEN': 'a$"b', 'ASMS_API_PASSWORD': 'c\\d'}))
            secrets.chmod(0o600)
            settings = {'apply': False}
            with patch.object(wizard.subprocess, 'run', return_value=SimpleNamespace(returncode=code)) as run, \
                 patch.object(wizard, 'ask', return_value=response) as ask, \
                 patch.object(wizard, 'write_private') as save:
                result = wizard.finish_container(
                    config, settings, SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid()))
                self.assertNotIn('systemctl', run.call_args.args[0])
                self.assertEqual(run.call_args.kwargs['env']['JIRA_API_TOKEN'], 'a$"b')
                if code:
                    ask.assert_not_called();save.assert_not_called()
                else:
                    self.assertEqual(settings['apply'], response == 'START');save.assert_called_once()
                return result
    def test_failed_doctor_never_activates(self):
        self.assertEqual(self.run_case(1, 'START'), 1)
    def test_explicit_start_saved(self):
        self.assertEqual(self.run_case(0, 'START'), 0)
    def test_blank_keeps_dry_run(self):
        self.assertEqual(self.run_case(0, ''), 0)

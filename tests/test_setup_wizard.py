import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('setup_wizard', ROOT / 'scripts/setup_wizard.py')
wizard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wizard)


class SetupWizardTests(unittest.TestCase):
    def test_private_reader_rejects_symlink_hardlink_permissions_and_size(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / 'source'
            source.write_text('secret')
            source.chmod(0o600)
            self.assertEqual(wizard.read_private_text(source, os.getuid()), 'secret')
            link = root / 'link'
            link.symlink_to(source)
            with self.assertRaises(ValueError):
                wizard.read_private_text(link, os.getuid())
            hardlink = root / 'hardlink'
            os.link(source, hardlink)
            with self.assertRaises(ValueError):
                wizard.read_private_text(hardlink, os.getuid())
            hardlink.unlink()
            source.chmod(0o644)
            with self.assertRaises(ValueError):
                wizard.read_private_text(source, os.getuid())
            source.write_text('x' * 1025)
            source.chmod(0o600)
            with self.assertRaisesRegex(ValueError, 'too large'):
                wizard.read_private_text(source, os.getuid(), max_bytes=1024)

    def test_config_replaces_all_lab_bindings_without_mutating_template(self):
        template = json.loads((ROOT / 'examples/jira-sync-basic-structured.json').read_text())
        original = json.dumps(template)
        fields = dict(structured='customfield_900', id='customfield_901', status='customfield_902', owner='customfield_903')
        c = wizard.build_config(template, 'https://example.atlassian.net/', 'api@example.com', 'DEMO', '123', 'https://asms.example.com', 'api', ['fw_demo'], '', fields)
        self.assertEqual(json.dumps(template), original)
        self.assertNotIn('10_10_31_205', json.dumps(c))
        self.assertNotIn('10110', json.dumps(c))
        self.assertNotIn('tls_certificate_sha256', c['fireflow'])
        self.assertFalse(c['apply'])
        self.assertIn('issuetype = 123', c['jira']['jql'])
        self.assertEqual(c['fireflow']['allowed_devices'], ['fw_demo'])

    def test_trust_server_certificate_enables_pin_only_tls(self):
        template = json.loads((ROOT / 'examples/jira-sync-basic-structured.json').read_text())
        fields = dict(structured='customfield_900', id='customfield_901',
                      status='customfield_902', owner='customfield_903')
        c = wizard.build_config(
            template, 'https://example.atlassian.net', 'api@example.com', 'DEMO',
            '123', 'https://192.0.2.10', 'api', ['fw_demo'], 'A' * 64, fields,
            trust_server_certificate=True, existing_ca_file='/etc/old-ca.pem')
        self.assertTrue(c['fireflow']['tls_pin_only'])
        self.assertEqual(c['fireflow']['tls_certificate_sha256'], 'a' * 64)
        self.assertNotIn('ca_file', c['fireflow'])

    def test_trust_server_certificate_requires_a_pin(self):
        template = json.loads((ROOT / 'examples/jira-sync-basic-structured.json').read_text())
        fields = dict(structured='customfield_900', id='customfield_901',
                      status='customfield_902', owner='customfield_903')
        with self.assertRaisesRegex(ValueError, 'requires'):
            wizard.build_config(
                template, 'https://example.atlassian.net', 'api@example.com',
                'DEMO', '123', 'https://192.0.2.10', 'api', ['fw_demo'], '',
                fields, trust_server_certificate=True)

    def test_certificate_refresh_preserves_apply_and_requires_explicit_trust(self):
        settings = {'apply': True, 'fireflow': {'base_url': 'https://192.0.2.10'}}
        account = SimpleNamespace(pw_uid=1, pw_gid=1)
        with patch.object(wizard, 'capture_certificate_sha256', return_value='a' * 64), \
             patch.object(wizard, 'ask', return_value='TRUST'), \
             patch.object(wizard, 'write_private') as write, \
             patch.object(wizard, 'private_json', return_value={
                 'JIRA_API_TOKEN': 'x', 'ASMS_API_PASSWORD': 'y'}), \
             patch.object(wizard.subprocess, 'run',
                          return_value=SimpleNamespace(returncode=0)):
            self.assertEqual(wizard.refresh_container_certificate(
                Path('/config/bus.json'), settings, account), 0)
        saved = json.loads(write.call_args.args[1])
        self.assertTrue(saved['apply'])
        self.assertTrue(saved['fireflow']['tls_pin_only'])
        self.assertEqual(saved['fireflow']['tls_certificate_sha256'], 'a' * 64)

    def test_origin_rejects_credentials_paths_and_http(self):
        for value in ['http://example.com', 'https://user:pass@example.com', 'https://example.com/path', 'https://example.com?q=1']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                wizard.origin(value)

    def test_secrets_escape_systemd_quotes_and_backslashes_without_shell_expansion(self):
        self.assertEqual(wizard.secret_line('TOKEN', 'a"b\\c$HOME`id`'), 'TOKEN="a\\"b\\\\c$HOME`id`"\n')
        for value in ['', 'x\ny', 'x\ry', 'x\x00y']:
            with self.assertRaises(ValueError):
                wizard.secret_line('TOKEN', value)
        with self.assertRaises(ValueError):
            wizard.secret_line('TOKEN\nOTHER', 'x')

    def test_atomic_private_write_and_symlink_rejection(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'secret'
            wizard.write_private(p, 'old', os.getuid(), os.getgid())
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            with patch.object(wizard.os, 'replace', side_effect=OSError('failure')):
                with self.assertRaises(OSError):
                    wizard.write_private(p, 'new', os.getuid(), os.getgid())
            self.assertEqual(p.read_text(), 'old')
            self.assertEqual(list(Path(d).iterdir()), [p])
            link = Path(d) / 'link'
            link.symlink_to(p)
            with self.assertRaises(ValueError):
                wizard.write_private(link, 'new', os.getuid(), os.getgid())

    def test_worktype_must_be_in_project_and_not_subtask(self):
        project = {'issueTypes': [{'id': '12'}, {'id': '13', 'subtask': True}]}
        wizard.validate_worktype(project, '12')
        for value in ['13', '99']:
            with self.assertRaises(ValueError):
                wizard.validate_worktype(project, value)

    def test_failed_doctor_never_activates_or_writes(self):
        with patch.object(wizard, 'run_doctor', return_value=SimpleNamespace(returncode=1)) as run, patch.object(wizard, 'write_private') as write, patch.object(wizard, 'ask') as ask:
            self.assertEqual(wizard.finish_setup(Path('/fake/config'), {'apply': False}, SimpleNamespace()), 1)
            self.assertEqual(run.call_count, 1)
            write.assert_not_called()
            ask.assert_not_called()

    def test_resume_runs_doctor_and_preserves_config_when_activation_declined(self):
        settings = {'apply': True}
        with patch.object(wizard, 'run_doctor', return_value=SimpleNamespace(returncode=0)) as run, patch.object(wizard, 'write_private') as write, patch.object(wizard, 'ask', return_value=''):
            self.assertEqual(wizard.finish_setup(Path('/fake/config'), settings, SimpleNamespace()), 0)
            self.assertEqual(run.call_count, 1)
            self.assertTrue(settings['apply'])
            write.assert_not_called()

    def test_main_existing_configuration_resumes_without_requesting_secrets(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            saved = {'jira': {'base_url': 'https://example.atlassian.net'}, 'mapping': {'structured': {'field': 'customfield_123'}}}
            (folder / 'bus.json').write_text(json.dumps(saved))
            (folder / 'secrets.env').write_text('TOKEN="keep"')
            (folder / 'bus.json').chmod(0o600)
            (folder / 'secrets.env').chmod(0o600)
            original_path = Path
            def paths(value):
                return folder if value == '/etc/algosec-jira-bus' else original_path(value)
            account = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
            with patch.object(wizard, 'Path', side_effect=paths), patch.object(wizard.sys, 'platform', 'linux'), patch.object(wizard.sys, 'argv', ['setup', '--source', d]), patch.object(wizard.os, 'geteuid', return_value=0), patch.object(wizard.pwd, 'getpwnam', return_value=account), patch.object(wizard, 'finish_setup', return_value=0) as finish, patch.object(wizard.getpass, 'getpass') as secret:
                self.assertEqual(wizard.main(), 0)
                finish.assert_called_once()
                secret.assert_not_called()
            self.assertEqual((folder / 'secrets.env').read_text(), 'TOKEN="keep"')

    def test_main_refuses_legacy_secret_reference_before_resume(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            saved = {'jira': {'base_url': 'https://example.atlassian.net',
                              'token_ref': 'keyring:jira/token'},
                     'mapping': {'structured': {'field': 'customfield_123'}}}
            (folder / 'bus.json').write_text(json.dumps(saved))
            (folder / 'bus.json').chmod(0o600)
            original_path = Path
            def paths(value):
                return folder if value == '/etc/algosec-jira-bus' else original_path(value)
            account = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
            with patch.object(wizard, 'Path', side_effect=paths), \
                 patch.object(wizard.sys, 'platform', 'linux'), \
                 patch.object(wizard.sys, 'argv', ['setup', '--source', d]), \
                 patch.object(wizard.os, 'geteuid', return_value=0), \
                 patch.object(wizard.pwd, 'getpwnam', return_value=account), \
                 patch.object(wizard, 'finish_setup') as finish, \
                 patch.object(wizard.getpass, 'getpass') as secret:
                with self.assertRaisesRegex(ValueError, 'env:NAME'):
                    wizard.main()
                finish.assert_not_called()
                secret.assert_not_called()

    def test_start_occurs_only_after_doctor_and_config_write(self):
        events = []
        def run(args, **kwargs):
            events.append(args[0])
            return SimpleNamespace(returncode=0)
        with patch.object(wizard, 'run_doctor', side_effect=lambda *a: (events.append('doctor') or SimpleNamespace(returncode=0))), patch.object(wizard.subprocess, 'run', side_effect=run), patch.object(wizard, 'write_private', side_effect=lambda *a: events.append('write')), patch.object(wizard, 'ask', return_value='START'), patch.object(wizard, 'Path') as paths:
            settings = {'apply': False}
            self.assertEqual(wizard.finish_setup(Path('/fake/config'), settings, SimpleNamespace(pw_uid=1, pw_gid=1)), 0)
            self.assertEqual(events, ['doctor', 'write', 'systemctl', 'systemctl'])
            interval = paths.return_value.__truediv__.return_value.write_text.call_args.args[0]
            self.assertIn('OnUnitActiveSec=30s', interval)
            self.assertIn('AccuracySec=1s', interval)
            self.assertTrue(settings['apply'])

    def test_runtime_doctor_uses_legacy_compatible_oneshot_and_cleans_up(self):
        with patch.object(wizard, 'Path') as paths, patch.object(wizard, 'write_private') as write, patch.object(wizard.subprocess, 'run', return_value=SimpleNamespace(returncode=1)) as run:
            result = wizard.run_doctor('/etc/algosec-jira-bus/bus.json')
            self.assertEqual(result.returncode, 1)
            content = write.call_args.args[1]
            self.assertIn('Type=oneshot', content)
            self.assertIn('User=algosec-jira-bus', content)
            self.assertIn('EnvironmentFile=/etc/algosec-jira-bus/secrets.env', content)
            commands = [c.args[0] for c in run.call_args_list]
            self.assertEqual(commands[1][0:2], ['systemctl', 'start'])
            self.assertEqual(commands[2][0], 'journalctl')
            self.assertNotIn('--wait', str(commands))
            paths.return_value.__truediv__.return_value.unlink.assert_called_once()

    def test_activation_failure_restores_previous_apply(self):
        settings = {'apply': False}
        with patch.object(wizard, 'run_doctor', return_value=SimpleNamespace(returncode=0)), patch.object(wizard.subprocess, 'run', side_effect=wizard.subprocess.CalledProcessError(1, ['systemctl'])), patch.object(wizard, 'write_private') as write, patch.object(wizard, 'ask', return_value='START'), patch.object(wizard, 'Path'):
            with self.assertRaises(wizard.subprocess.CalledProcessError):
                wizard.finish_setup('/fake/config', settings, SimpleNamespace(pw_uid=1, pw_gid=1))
            self.assertFalse(settings['apply'])
            self.assertFalse(json.loads(write.call_args.args[1])['apply'])

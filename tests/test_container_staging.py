import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'container_config_stager', ROOT / 'packaging/docker/stage-config.py')
stager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stager)
upgrade_spec = importlib.util.spec_from_file_location(
    'container_config_upgrader', ROOT / 'packaging/docker/upgrade-config.py')
upgrader = importlib.util.module_from_spec(upgrade_spec)
upgrade_spec.loader.exec_module(upgrader)


class ContainerConfigStagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'bus.json'
        self.secrets = self.root / 'secrets.json'
        self.config.write_text(json.dumps({
            'apply': False,
            'jira': {'token_ref': 'env:JIRA_API_TOKEN'},
            'fireflow': {'password_ref': 'env:ASMS_API_PASSWORD'},
        }))
        self.secrets.write_text(json.dumps({
            'JIRA_API_TOKEN': 'jira-value',
            'ASMS_API_PASSWORD': 'fireflow-value',
        }))
        self.config.chmod(0o600)
        self.secrets.chmod(0o600)

    def validate(self):
        return stager.validate(self.config, self.secrets, expected_uid=os.getuid())

    def test_valid_pair_is_installed_privately_without_changing_secret_values(self):
        files = self.validate()
        target = self.root / 'target'
        target.mkdir(mode=0o700)
        stager.install(target, files, target_uid=os.getuid(), target_gid=os.getgid())
        self.assertEqual(json.loads((target / 'secrets.json').read_text()),
                         json.loads(self.secrets.read_text()))
        self.assertEqual((target / 'bus.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual((target / 'secrets.json').stat().st_mode & 0o777, 0o600)

    def test_source_files_must_be_exactly_0600_regular_single_link_files(self):
        self.config.chmod(0o640)
        with self.assertRaisesRegex(ValueError, 'owner-only regular file'):
            self.validate()
        self.config.chmod(0o600)
        hardlink = self.root / 'config-link.json'
        os.link(self.config, hardlink)
        with self.assertRaisesRegex(ValueError, 'owner-only regular file'):
            self.validate()

    def test_exact_secret_references_and_shape_are_required(self):
        for config in (
            {'jira': {'token_ref': 'env:OTHER'},
             'fireflow': {'password_ref': 'env:ASMS_API_PASSWORD'}},
            {'jira': {'token_ref': 'env:JIRA_API_TOKEN'},
             'fireflow': {'password_ref': 'env:ASMS_API_PASSWORD',
                          'session_ref': 'env:SESSION'}},
        ):
            self.config.write_text(json.dumps(config)); self.config.chmod(0o600)
            with self.assertRaises(ValueError):
                self.validate()
        self.config.write_text(json.dumps({
            'jira': {'token_ref': 'env:JIRA_API_TOKEN'},
            'fireflow': {'password_ref': 'env:ASMS_API_PASSWORD'},
        })); self.config.chmod(0o600)
        self.secrets.write_text(json.dumps({'JIRA_API_TOKEN': 'only-one'})); self.secrets.chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'must contain'):
            self.validate()

    def test_duplicate_json_keys_and_symlinks_are_rejected(self):
        self.config.write_text('{"jira":{},"jira":{},"fireflow":{}}')
        self.config.chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'strict UTF-8 JSON'):
            self.validate()
        self.config.unlink()
        self.config.symlink_to(self.secrets)
        with self.assertRaisesRegex(ValueError, 'readable private regular file'):
            self.validate()

    def test_custom_ca_must_be_supplied_when_config_references_one(self):
        value = json.loads(self.config.read_text())
        value['fireflow']['ca_file'] = '/etc/algosec-jira-bus/ca.pem'
        self.config.write_text(json.dumps(value)); self.config.chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'provide its PEM bundle'):
            self.validate()


class NonInteractiveInstallerContractTests(unittest.TestCase):
    def test_inputs_are_validated_and_doctor_runs_before_container_stop(self):
        helper = (ROOT / 'packaging/docker/install-docker.sh').read_text()
        self.assertIn('--config-file', helper)
        self.assertIn('--secrets-file', helper)
        validate = helper.index('python3 "$STAGER" --config')
        docker_info = helper.index('docker info >/dev/null')
        doctor = helper.index('"$IMAGE" doctor')
        stop = helper.index('docker stop algosec-jira-bus', doctor)
        self.assertLess(validate, docker_info)
        self.assertLess(doctor, stop)
        self.assertIn('docker rm -f algosec-jira-bus', helper)

    def test_upgrade_preflights_before_stop_and_has_automatic_rollback(self):
        helper = (ROOT / 'packaging/docker/install-docker.sh').read_text()
        start = helper.index('if [ "$UPGRADE_ONLY" -eq 1 ]')
        doctor = helper.index('"$IMAGE" doctor', start)
        stop = helper.index('docker stop algosec-jira-bus', doctor)
        restore = helper.index('"$CONFIG_UPGRADER" restore', stop)
        self.assertLess(doctor, stop)
        self.assertIn('docker rename algosec-jira-bus algosec-jira-bus-previous', helper)
        self.assertGreater(restore, stop)

    def test_bus_update_stages_verified_installer_and_selects_upgrade_mode(self):
        helper = (ROOT / 'packaging/docker/bus_update').read_text()
        self.assertIn("STAGE=$(mktemp -d /tmp/algosec-jira-update.XXXXXX)", helper)
        self.assertIn('Installer SHA256 mismatch; current container was not touched', helper)
        self.assertIn('sh "$STAGED_INSTALLER" --upgrade', helper)


class UpgradeConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'bus.json'
        self.staged = self.root / 'staged.json'
        self.backup = self.root / 'backup.json'
        self.source.write_text(json.dumps({
            'apply': True,
            'jira': {'token_ref': 'env:JIRA_API_TOKEN'},
            'fireflow': {
                'password_ref': 'env:ASMS_API_PASSWORD',
                'allowed_fields': ['subject', 'devices'],
            },
        }))
        self.source.chmod(0o600)

    def test_adds_requestor_without_changing_secrets_or_apply(self):
        upgrader.stage(self.source, self.staged, os.getuid())
        result = json.loads(self.staged.read_text())
        self.assertTrue(result['apply'])
        self.assertEqual(result['jira']['token_ref'], 'env:JIRA_API_TOKEN')
        self.assertEqual(result['fireflow']['password_ref'], 'env:ASMS_API_PASSWORD')
        self.assertEqual(result['fireflow']['allowed_fields'],
                         ['subject', 'devices', 'Requestor'])
        self.assertFalse(result['fireflow']['legacy_rt_enabled'])
        self.assertEqual(result['jira_to_fireflow'], {
            'enabled': False, 'comments': True, 'status_map': {}})

    def test_basic_upgrade_adds_read_only_already_works_evidence(self):
        value = json.loads(self.source.read_text())
        value['fireflow']['template'] = 'Basic Change Traffic Request'
        value['mirror'] = {
            'transitions': {'already works': 'To Do'},
            'outcome_rules': [{
                'status': 'resolved', 'field': 'Completion verified',
                'equals': 'yes', 'transition': 'Done',
            }],
        }
        self.source.write_text(json.dumps(value)); self.source.chmod(0o600)
        upgrader.stage(self.source, self.staged, os.getuid())
        result = json.loads(self.staged.read_text())
        self.assertTrue(result['fireflow']['legacy_rt_read_enabled'])
        self.assertFalse(result['fireflow']['legacy_rt_enabled'])
        self.assertEqual(result['mirror']['transitions']['already works'], 'Done')
        self.assertIn({
            'status': 'resolved', 'field': 'Completion outcome',
            'equals': 'already works', 'transition': 'Done',
        }, result['mirror']['outcome_rules'])

    def test_preserves_an_explicit_jira_to_fireflow_configuration(self):
        value = json.loads(self.source.read_text())
        value['fireflow']['legacy_rt_enabled'] = True
        value['jira_to_fireflow'] = {
            'enabled': True, 'comments': True,
            'status_map': {'Cancelled': 'rejected'},
        }
        self.source.write_text(json.dumps(value)); self.source.chmod(0o600)
        upgrader.stage(self.source, self.staged, os.getuid())
        result = json.loads(self.staged.read_text())
        self.assertTrue(result['fireflow']['legacy_rt_enabled'])
        self.assertEqual(result['jira_to_fireflow'], value['jira_to_fireflow'])

    def test_apply_and_restore_are_atomic_from_the_callers_view(self):
        original = self.source.read_bytes()
        upgrader.stage(self.source, self.staged, os.getuid())
        upgrader.apply(self.source, self.staged, self.backup, os.getuid())
        self.assertIn('Requestor', json.loads(self.source.read_text())['fireflow']['allowed_fields'])
        self.assertTrue(self.backup.exists())
        upgrader.restore(self.source, self.backup, os.getuid())
        self.assertEqual(self.source.read_bytes(), original)
        self.assertFalse(self.backup.exists())


if __name__ == '__main__':
    unittest.main()

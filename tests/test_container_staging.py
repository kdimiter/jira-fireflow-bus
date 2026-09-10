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


if __name__ == '__main__':
    unittest.main()

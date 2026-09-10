import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'secure_container_scheduler', ROOT / 'packaging/docker/scheduler.py')
SCHEDULER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCHEDULER)


class ContainerSecretFileTests(unittest.TestCase):
    def test_secret_file_must_not_be_hardlinked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.json'
            source.write_text(json.dumps({'JIRA_API_TOKEN': 'x',
                                          'ASMS_API_PASSWORD': 'y'}))
            source.chmod(0o600)
            hardlink = root / 'secrets.json'
            os.link(source, hardlink)
            with self.assertRaises(ValueError):
                SCHEDULER.load_secrets(hardlink)

    def test_secret_file_has_a_strict_size_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'secrets.json'
            path.write_text(json.dumps({'JIRA_API_TOKEN': 'x' * (64 * 1024),
                                        'ASMS_API_PASSWORD': 'y'}))
            path.chmod(0o600)
            with self.assertRaises(ValueError):
                SCHEDULER.load_secrets(path)


if __name__ == '__main__':
    unittest.main()

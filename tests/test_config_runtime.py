from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from algosec_jira_bus.config import private_json, resolve_secret
from algosec_jira_bus.runtime import Audit, redact, secure_dir
from scripts.setup_wizard import validate_secret_references


class PrivateJsonTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, content='{"enabled": true}'):
        path = self.root / 'config.json'
        path.write_text(content, encoding='utf-8')
        path.chmod(0o600)
        return path

    def test_reads_an_owner_only_regular_json_object(self):
        self.assertEqual(private_json(self.write(), os.getuid()), {'enabled': True})

    def test_preserves_file_not_found_for_optional_state_callers(self):
        with self.assertRaises(FileNotFoundError):
            private_json(self.root / 'missing.json', os.getuid())

    def test_rejects_a_symbolic_link(self):
        target = self.write()
        link = self.root / 'config-link.json'
        link.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'regular owner-only file'):
            private_json(link, os.getuid())

    def test_rejects_a_hard_link(self):
        target = self.write()
        link = self.root / 'config-hardlink.json'
        os.link(target, link)
        with self.assertRaisesRegex(ValueError, 'regular owner-only file'):
            private_json(link, os.getuid())

    def test_rejects_group_or_world_permissions(self):
        path = self.write()
        path.chmod(0o640)
        with self.assertRaisesRegex(ValueError, 'regular owner-only file'):
            private_json(path, os.getuid())

    def test_rejects_a_file_owned_by_another_expected_uid(self):
        with self.assertRaisesRegex(ValueError, 'regular owner-only file'):
            private_json(self.write(), os.getuid() + 1)

    def test_rejects_oversized_json_before_parsing(self):
        path = self.write('{"value":"' + ('x' * (1024 * 1024)) + '"}')
        with self.assertRaisesRegex(ValueError, 'too large'):
            private_json(path, os.getuid())

    def test_rejects_duplicate_keys_and_non_object_roots(self):
        with self.assertRaisesRegex(ValueError, 'duplicate key'):
            private_json(self.write('{"apply":false,"apply":true}'), os.getuid())
        with self.assertRaisesRegex(ValueError, 'JSON object'):
            private_json(self.write('[]'), os.getuid())

    def test_rejects_non_standard_json_numbers(self):
        with self.assertRaisesRegex(ValueError, 'non-JSON number'):
            private_json(self.write('{"timeout":NaN}'), os.getuid())


class EnvironmentSecretTests(unittest.TestCase):
    def test_preserves_the_secret_without_shell_interpretation(self):
        value = '$x`id`"\\ and spaces'
        with patch.dict(os.environ, {'BUS_TEST_TOKEN': value}, clear=True):
            self.assertEqual(resolve_secret('env:BUS_TEST_TOKEN'), value)

    def test_rejects_non_environment_references_and_invalid_names(self):
        for reference in ('plain:value', 'keyring:name', 'env:', 'env:A-B', None):
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                resolve_secret(reference)

    def test_upgrade_check_rejects_legacy_keyring_references(self):
        cases = (
            {'jira': {'token_ref': 'keyring:jira/token'}, 'fireflow': {}},
            {'jira': {}, 'fireflow': {'password_ref': 'keyring:fireflow/password'}},
            {'jira': {}, 'fireflow': {'session_ref': 'keyring:fireflow/session'}},
        )
        for settings in cases:
            with self.subTest(settings=settings), self.assertRaisesRegex(
                    ValueError, 'move those secrets'):
                validate_secret_references(settings)
        validate_secret_references({
            'jira': {'token_ref': 'env:JIRA_API_TOKEN'},
            'fireflow': {'password_ref': 'env:ASMS_API_PASSWORD',
                         'session_ref': 'env:ASMS_API_SESSION'},
        })

    def test_rejects_missing_empty_and_unreasonably_large_secrets(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, 'unavailable'):
            resolve_secret('env:BUS_TEST_TOKEN')
        with patch.dict(os.environ, {'BUS_TEST_TOKEN': ''}, clear=True), self.assertRaisesRegex(ValueError, 'unavailable'):
            resolve_secret('env:BUS_TEST_TOKEN')
        with patch.dict(os.environ, {'BUS_TEST_TOKEN': 'x' * 65537}, clear=True), self.assertRaisesRegex(ValueError, 'too large'):
            resolve_secret('env:BUS_TEST_TOKEN')


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_secure_dir_creates_a_private_directory(self):
        path = secure_dir(self.root / 'state')
        self.assertTrue(path.is_dir())
        self.assertEqual(path.stat().st_mode & 0o777, 0o700)

    def test_secure_dir_rejects_permissive_and_symbolic_link_directories(self):
        loose = self.root / 'loose'
        loose.mkdir(mode=0o755)
        with self.assertRaisesRegex(ValueError, 'Private directory required'):
            secure_dir(loose)
        link = self.root / 'link'
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'Private directory required'):
            secure_dir(link)

    def test_redact_recurses_and_removes_keyed_and_inline_credentials(self):
        source = {
            'password': 'hunter2',
            'nested': [{'api_token': 'abcd'}, 'Authorization: Bearer secret-value'],
            'safe': 'token=secret-value next',
            'email': 'person@example.test',
        }
        cleaned = redact(source)
        encoded = json.dumps(cleaned)
        self.assertNotIn('hunter2', encoded)
        self.assertNotIn('abcd', encoded)
        self.assertNotIn('secret-value', encoded)
        self.assertNotIn('person@example.test', encoded)
        self.assertEqual(cleaned['password'], '[REDACTED]')

    def test_audit_rotates_at_the_configured_bound_and_keeps_private_files(self):
        audit = Audit(self.root / 'state', max_bytes=256, max_backups=2)
        for number in range(12):
            audit.record('op-%d' % number, 'received', {'message': 'x' * 48})
        files = sorted(audit.directory.glob('audit.jsonl*'))
        self.assertEqual({path.name for path in files},
                         {'audit.jsonl', 'audit.jsonl.1', 'audit.jsonl.2'})
        self.assertTrue(all(path.stat().st_size <= 256 for path in files))
        self.assertTrue(all(path.stat().st_mode & 0o077 == 0 for path in files))

    def test_audit_refuses_a_symbolic_link_target(self):
        state = secure_dir(self.root / 'state')
        target = self.root / 'outside'
        target.write_text('unchanged', encoding='utf-8')
        (state / 'audit.jsonl').symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'private regular file'):
            Audit(state).record('op-1', 'received')
        self.assertEqual(target.read_text(encoding='utf-8'), 'unchanged')

    def test_audit_refuses_a_hard_link_target(self):
        state = secure_dir(self.root / 'state')
        target = self.root / 'outside'
        target.write_text('unchanged', encoding='utf-8')
        target.chmod(0o600)
        os.link(target, state / 'audit.jsonl')
        with self.assertRaisesRegex(ValueError, 'private regular file'):
            Audit(state).record('op-1', 'received')
        self.assertEqual(target.read_text(encoding='utf-8'), 'unchanged')

    def test_audit_redacts_details_before_they_reach_disk(self):
        audit = Audit(self.root / 'state')
        record = audit.record('op-1', 'failed', {'token': 'abcd', 'error': 'password=hunter2'})
        on_disk = audit.path.read_text(encoding='utf-8')
        self.assertEqual(record['detail']['token'], '[REDACTED]')
        self.assertNotIn('abcd', on_disk)
        self.assertNotIn('hunter2', on_disk)

    def test_concurrent_writers_leave_complete_json_lines(self):
        state = self.root / 'state'

        def write(number):
            Audit(state).record('op-%d' % number, 'received', {'number': number})

        with ThreadPoolExecutor(max_workers=8) as workers:
            list(workers.map(write, range(40)))
        lines = (state / 'audit.jsonl').read_text(encoding='utf-8').splitlines()
        records = [json.loads(line) for line in lines]
        self.assertEqual(len(records), 40)
        self.assertEqual({record['detail']['number'] for record in records}, set(range(40)))

    def test_audit_rejects_a_record_that_cannot_fit_within_its_bound(self):
        audit = Audit(self.root / 'state', max_bytes=128, max_backups=1)
        with self.assertRaisesRegex(ValueError, 'too large'):
            audit.record('op-1', 'received', {'message': 'x' * 512})


if __name__ == '__main__':
    unittest.main()

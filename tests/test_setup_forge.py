"""Contract tests for the non-root Forge deployment helper."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDER = 'ari:cloud:ecosystem::app/00000000-0000-0000-0000-000000000000'
APP_ID = 'ari:cloud:ecosystem::app/12345678-1234-1234-1234-123456789abc'


class ForgeSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'source'
        self.home = Path(self.temp.name) / 'home'
        self.bin = Path(self.temp.name) / 'bin'
        (self.root / 'scripts').mkdir(parents=True)
        (self.root / 'forge/src').mkdir(parents=True)
        self.home.mkdir()
        self.bin.mkdir()
        shutil.copy(ROOT / 'scripts/setup-forge.sh', self.root / 'scripts/setup-forge.sh')
        (self.root / 'forge/manifest.yml').write_text(
            'app:\n  id: ' + PLACEHOLDER + '\nmodules: {}\n')
        (self.root / 'forge/package.json').write_text('{}\n')
        (self.root / 'forge/package-lock.json').write_text('{}\n')
        (self.root / 'forge/src/edit.tsx').write_text('export {};\n')
        self.log = Path(self.temp.name) / 'calls'
        self._script('node',
                     'if [ "${1:-}" = -p ]; then echo 22; else echo v22.0.0; fi')
        self._script('npm', 'printf "npm %s\\n" "$*" >> "$CALLS"')
        self._script(
            'forge',
            'printf "forge %s\\n" "$*" >> "$CALLS"\n'
            'if [ "${1:-}" = register ]; then\n'
            '  /usr/bin/sed "s#' + PLACEHOLDER + '#' + APP_ID + '#" manifest.yml > manifest.yml.new\n'
            '  /bin/mv manifest.yml.new manifest.yml\n'
            'fi')
        self.env = {
            **os.environ,
            'HOME': str(self.home),
            'PATH': str(self.bin) + os.pathsep + os.environ['PATH'],
            'CALLS': str(self.log),
            'FORGE_EMAIL': 'admin@example.invalid',
            'FORGE_API_TOKEN': 'synthetic-test-token',
        }

    def _script(self, name, body):
        path = self.bin / name
        path.write_text('#!/bin/sh\nset -eu\n' + body + '\n')
        path.chmod(0o700)

    def run_setup(self, *args):
        return subprocess.run(
            ['/bin/sh', str(self.root / 'scripts/setup-forge.sh'), *args],
            env=self.env, capture_output=True, text=True)

    def test_existing_app_id_is_installed_without_registering_a_duplicate(self):
        result = self.run_setup(
            '--site', 'tenant.atlassian.net', '--environment', 'production',
            '--install-mode', 'new', '--app-mode', 'existing', '--app-id', APP_ID)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text()
        self.assertNotIn('forge login', calls)
        self.assertNotIn('forge register', calls)
        self.assertIn('forge deploy --environment production', calls)
        self.assertIn(
            'forge install --site tenant.atlassian.net --product jira --environment production',
            calls)
        self.assertIn(APP_ID, (self.home / 'algosec-jira-forge/manifest.yml').read_text())

    def test_registered_app_is_preserved_and_register_runs_only_once(self):
        arguments = (
            '--site', 'tenant.atlassian.net', '--environment', 'production',
            '--install-mode', 'upgrade', '--app-mode', 'register')
        first = self.run_setup(*arguments)
        second = self.run_setup(*arguments)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        calls = self.log.read_text()
        self.assertEqual(calls.count('forge register\n'), 1)
        self.assertEqual(calls.count('forge install --upgrade'), 2)
        self.assertIn(APP_ID, (self.home / 'algosec-jira-forge/manifest.yml').read_text())

    def test_url_instead_of_hostname_is_rejected_before_commands_run(self):
        result = self.run_setup('--site', 'https://tenant.atlassian.net',
                                '--install-mode', 'new')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Invalid Jira hostname', result.stderr)
        self.assertFalse(self.log.exists())

    def test_work_directory_outside_user_home_is_rejected(self):
        self.env['ALGOSEC_FORGE_WORKDIR'] = str(Path(self.temp.name) / 'outside')
        result = self.run_setup('--site', 'tenant.atlassian.net',
                                '--install-mode', 'new')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('must be inside the user home', result.stderr)
        self.assertFalse(self.log.exists())


if __name__ == '__main__':
    unittest.main()

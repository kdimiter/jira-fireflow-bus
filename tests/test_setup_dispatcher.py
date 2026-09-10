"""Dispatcher tests exercise routing only, without installations."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SetupDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.bin = self.base / 'bin'
        self.bin.mkdir()
        for name in ('dirname', 'sh', 'cat'):
            (self.bin / name).symlink_to(shutil.which(name))
        uname = self.bin / 'uname'
        uname.write_text('#!/bin/sh\necho Linux\n')
        uname.chmod(0o700)
        self.setup = self.base / 'setup.sh'
        shutil.copy(ROOT / 'scripts/setup.sh', self.setup)
        self.log = self.base / 'calls'
        self.env = {**os.environ, 'PATH': str(self.bin), 'CALLS': str(self.log)}
        for name, mode in [('algosec-jira-bus-linux.run', 'native'), ('install-docker.sh', 'docker')]:
            (self.base / name).write_text('#!/bin/sh\nprintf "' + mode + '\\n" >> "$CALLS"\nprintf "%s\\n" "$@" >> "$CALLS"\n')

    def run_setup(self, *args, input=None):
        return subprocess.run(['/bin/sh', str(self.setup), *args], env=self.env,
                              input=input, capture_output=True, text=True)

    def test_explicit_native_dispatches_to_bundle(self):
        result = self.run_setup('--mode', 'native')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.log.read_text().startswith('native\n'))

    def test_docker_receives_explicit_bundle_and_helper_options(self):
        result = self.run_setup('--mode', 'docker', '--bundle', '/some path/release.run', '--', '--help')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.log.read_text(), 'docker\n--bundle\n/some path/release.run\n--help\n')

    def test_no_implicit_deployment_when_selection_missing(self):
        result = self.run_setup(input='')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log.exists())

    def test_prompt_selects_docker(self):
        result = self.run_setup(input='2\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.log.read_text().startswith('docker\n'))

    def test_unknown_mode_never_executes_installer(self):
        result = self.run_setup('--mode', 'other')
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.log.exists())


if __name__ == '__main__':
    unittest.main()

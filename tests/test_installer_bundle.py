import importlib.util
import json
import os
import shutil
import sys
import hashlib
import io
import tarfile
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('installer_builder', ROOT / 'scripts/build-installer.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class InstallerBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / 'source'
        (self.source / 'algosec_jira_bus').mkdir(parents=True)
        (self.source / 'algosec_jira_bus/__init__.py').write_text('')
        (self.source / '.env').write_text('SECRET=do-not-bundle')
        (self.source / 'forge/node_modules').mkdir(parents=True)
        (self.source / 'forge/node_modules/secret.json').write_text('private')
        (self.source / 'forge/manifest.yml').write_text('app:\n  id: ari:cloud:ecosystem::app/lab-id\n')
        (self.source / 'packaging/docker').mkdir(parents=True)
        (self.source / 'packaging/docker/Dockerfile').write_text('FROM python:3.12-slim\n')
        (self.source / 'packaging/docker/.dockerignore').write_text('.env\n')
        (self.source / 'CHANGELOG.md').write_text('# Changelog\n')
        self.bundle = self.base / 'installer.run'
        builder.build(self.source, self.bundle)

    def run_bundle(self, *args):
        return subprocess.run(['sh', str(self.bundle), *map(str, args)], capture_output=True, text=True, cwd=self.base)

    def test_extract_outside_checkout_checks_manifest_and_excludes_private_files(self):
        target = self.base / 'unpacked'
        result = self.run_bundle('--extract', target)
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads((target / 'MANIFEST.json').read_text())
        self.assertIn('forge/manifest.yml', manifest['files'])
        self.assertIn('packaging/docker/Dockerfile', manifest['files'])
        self.assertIn('packaging/docker/.dockerignore', manifest['files'])
        self.assertIn('CHANGELOG.md', manifest['files'])
        self.assertNotIn('lab-id', (target / 'forge/manifest.yml').read_text())
        self.assertIn('00000000-0000-0000-0000-000000000000', (target / 'forge/manifest.yml').read_text())
        self.assertFalse((target / '.env').exists())
        self.assertFalse((target / 'forge/node_modules').exists())
        self.assertFalse((target / 'vendor').exists())

    def test_corruption_fails_before_creating_destination(self):
        data = bytearray(self.bundle.read_bytes())
        data[-12] ^= 1
        self.bundle.write_bytes(data)
        target = self.base / 'unpacked'
        result = self.run_bundle('--extract', target)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('checksum mismatch', result.stderr)
        self.assertFalse(target.exists())

    def test_existing_destination_is_never_overwritten(self):
        target = self.base / 'unpacked'
        target.mkdir()
        (target / 'keep').write_text('kept')
        self.assertNotEqual(self.run_bundle('--extract', target).returncode, 0)
        self.assertEqual((target / 'keep').read_text(), 'kept')

    def test_traversal_member_is_rejected_even_with_valid_payload_checksum(self):
        original = self.bundle.read_bytes()
        header, old_payload = original.split(builder.MARKER, 1)
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w:gz') as archive:
            item = tarfile.TarInfo('../escaped')
            item.size = 4
            archive.addfile(item, io.BytesIO(b'evil'))
        payload = stream.getvalue()
        header = header.replace(hashlib.sha256(old_payload).hexdigest().encode(),
                                hashlib.sha256(payload).hexdigest().encode())
        self.bundle.write_bytes(header + builder.MARKER + payload)
        result = self.run_bundle('--extract', self.base / 'unpacked')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Unsafe archive member', result.stderr)
        self.assertFalse((self.base / 'escaped').exists())

    def test_install_requires_an_external_connector_wheel(self):
        result = self.run_bundle()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--connector-wheel', result.stderr)

    def test_connector_digest_is_verified_before_setup(self):
        wheel = self.base / 'algosec_host_mcp-0.4.0-py3-none-any.whl'
        wheel.write_bytes(b'operator-supplied')
        result = self.run_bundle('--connector-wheel', wheel, '--connector-sha256', '0' * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('SHA256 mismatch', result.stderr)


class DockerInstallerTests(unittest.TestCase):
    def test_wrong_image_digest_fails_before_docker_load(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / 'image.tar.gz'
            archive.write_bytes(b'untrusted image')
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            log = root / 'docker.log'
            for name, body in (
                    ('id', 'echo 0'),
                    ('uname', 'echo Linux'),
                    ('docker', 'echo "$*" >> "' + str(log) + '"')):
                path = bin_dir / name
                path.write_text('#!/bin/sh\n' + body + '\n')
                path.chmod(0o700)
            result = subprocess.run(
                ['/bin/sh', str(ROOT / 'packaging/docker/install-docker.sh'),
                 '--image-archive', str(archive), '--image-sha256', '0' * 64],
                env={**os.environ, 'PATH': str(bin_dir) + os.pathsep + os.environ['PATH']},
                capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Image SHA256 mismatch', result.stderr)
            self.assertFalse(log.exists(), 'docker must not see an unverified archive')

class InstallerPreflightTests(unittest.TestCase):
    """Shell contract tests with a fake manager; these do not prove systemd deployment."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.bin = self.base / 'bin'
        self.bin.mkdir()
        for name in ('awk', 'basename', 'dirname', 'mktemp', 'rm', 'sh'):
            (self.bin / name).symlink_to(shutil.which(name))
        self.script('id', 'echo 0')
        self.script('uname', 'echo Linux')
        (self.bin / 'python3').symlink_to(sys.executable)
        self.source = self.base / 'source'
        (self.source / 'scripts').mkdir(parents=True)
        for name in ('install.sh', 'setup-linux.sh'):
            shutil.copy(ROOT / 'scripts' / name, self.source / 'scripts' / name)
        (self.source / 'pyproject.toml').write_text('')
        (self.source / 'scripts/setup_wizard.py').write_text('')
        self.wheel = self.base / 'algosec_host_mcp-0.4.0-py3-none-any.whl'
        self.wheel.write_bytes(b'')
        self.log = self.base / 'manager.log'

    def script(self, name, body):
        path = self.bin / name
        path.write_text('#!/bin/sh\n' + body + '\n')
        path.chmod(0o700)

    def manager(self, version='252', available=True):
        self.script('systemctl', 'echo "$*" >> "' + str(self.log) + '"\n'
                    + 'case "$1" in --version) echo "systemd ' + version + '";; '
                    + 'show) exit ' + ('0' if available else '1') + ';; *) exit 1;; esac')

    def run_shell(self, script, *args):
        return subprocess.run(['/bin/sh', str(self.source / 'scripts' / script), *args],
                              env={**os.environ, 'PATH': str(self.bin),
                                   'ALGOSEC_CONNECTOR_SOURCE': str(self.wheel)},
                              capture_output=True, text=True)

    def test_generic_python3_is_accepted_before_any_mutation(self):
        self.manager()
        result = self.run_shell('install.sh', '--preflight')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('preflight passed', result.stdout)
        self.assertNotIn('enable', self.log.read_text())

    def test_preflight_does_not_require_connector_material(self):
        self.manager()
        env = {**os.environ, 'PATH': str(self.bin)}
        result = subprocess.run(['/bin/sh', str(self.source / 'scripts/install.sh'), '--preflight'],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_direct_install_requires_connector_digest_before_mutation(self):
        self.manager()
        result = self.run_shell('install.sh')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Connector SHA256 must be exactly 64 hexadecimal characters', result.stderr)
        self.assertNotIn('enable', self.log.read_text())

    def test_unreachable_systemd_rejects_bootstrap_before_stopping_units(self):
        self.manager(available=False)
        result = self.run_shell('setup-linux.sh')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('running and reachable', result.stderr)
        self.assertNotIn('stop', self.log.read_text())
        self.assertNotIn('disable', self.log.read_text())

    def test_invalid_systemd_version_rejects_bootstrap_before_stopping_units(self):
        self.manager(version='invalid')
        result = self.run_shell('setup-linux.sh')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('could not read the systemd version', result.stderr)
        self.assertNotIn('stop', self.log.read_text())


if __name__ == '__main__':
    unittest.main()

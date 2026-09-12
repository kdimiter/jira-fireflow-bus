import importlib.util
import json
import os
import shutil
import stat
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
docker_spec = importlib.util.spec_from_file_location(
    'docker_installer_builder', ROOT / 'scripts/build-docker-installer.py')
docker_builder = importlib.util.module_from_spec(docker_spec)
docker_spec.loader.exec_module(docker_builder)


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
        (self.source / 'forge/scripts').mkdir()
        (self.source / 'forge/scripts/check-manifest.mjs').write_text('export {};\n')
        (self.source / 'packaging/docker').mkdir(parents=True)
        (self.source / 'packaging/docker/Dockerfile').write_text('FROM python:3.12-slim\n')
        (self.source / 'packaging/docker/.dockerignore').write_text('.env\n')
        (self.source / 'CHANGELOG.md').write_text('# Changelog\n')
        self.bundle = self.base / 'installer.run'
        builder.build(self.source, self.bundle)

    def run_bundle(self, *args, env=None):
        return subprocess.run(['sh', str(self.bundle), *map(str, args)], capture_output=True,
                              text=True, cwd=self.base, env=env)

    def test_extract_outside_checkout_checks_manifest_and_excludes_private_files(self):
        target = self.base / 'unpacked'
        result = self.run_bundle('--extract', target)
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads((target / 'MANIFEST.json').read_text())
        self.assertIn('forge/manifest.yml', manifest['files'])
        self.assertIn('forge/scripts/check-manifest.mjs', manifest['files'])
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

    def test_help_describes_a_self_contained_installer(self):
        result = self.run_bundle('--help')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('sh installer.run [wizard options]', result.stdout)
        self.assertNotIn('connector', result.stdout.lower())

    def test_install_dispatches_directly_to_setup_without_extra_artifacts(self):
        log = self.base / 'setup.log'
        setup = self.source / 'scripts/setup-linux.sh'
        setup.parent.mkdir(exist_ok=True)
        setup.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$INSTALLER_TEST_LOG"\n')
        builder.build(self.source, self.bundle)
        result = self.run_bundle('--configure-only',
                                 env={**os.environ, 'INSTALLER_TEST_LOG': str(log)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(log.read_text(), '--configure-only\n')


class DockerInstallerTests(unittest.TestCase):
    def run_helper_until_docker(self, root, *arguments):
        root.mkdir(parents=True)
        archive = root / 'image.tar.gz'
        archive.write_bytes(b'verified image archive')
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        bin_dir = root / 'bin'
        bin_dir.mkdir()
        docker_log = root / 'docker.log'
        for name, body in (
                ('id', 'echo 0'),
                ('uname', 'echo Linux'),
                ('docker', 'echo "$*" >> "' + str(docker_log) + '"')):
            path = bin_dir / name
            path.write_text('#!/bin/sh\n' + body + '\n')
            path.chmod(0o700)
        result = subprocess.run(
            ['/bin/sh', str(ROOT / 'packaging/docker/install-docker.sh'),
             '--image-archive', str(archive), '--image-sha256', digest, *arguments],
            env={**os.environ, 'PATH': str(bin_dir) + os.pathsep + os.environ['PATH']},
            capture_output=True, text=True)
        return result, docker_log

    def test_system_root_data_directories_are_rejected_before_docker_or_mutation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for target in ('/', '/etc', '/opt'):
                with self.subTest(target=target):
                    before = os.stat(target).st_mode
                    result, docker_log = self.run_helper_until_docker(
                        root / target.strip('/').replace('/', '-') if target != '/' else root / 'root',
                        '--data-dir', target)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('Refusing unsafe data directory', result.stderr)
                    self.assertFalse(docker_log.exists())
                    self.assertEqual(os.stat(target).st_mode, before)

    def test_unmarked_existing_custom_directory_is_not_repermissioned(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / 'existing-custom-data'
            data.mkdir(mode=0o755)
            before = data.stat()
            harness = root / 'harness'
            result, docker_log = self.run_helper_until_docker(
                harness, '--data-dir', str(data))
            self.assertNotEqual(result.returncode, 0)
            self.assertRegex(result.stderr, r'(symbolic links|Unsafe existing data directory)')
            self.assertFalse(docker_log.exists())
            after = data.stat()
            self.assertEqual(stat.S_IMODE(after.st_mode), stat.S_IMODE(before.st_mode))
            self.assertEqual((after.st_uid, after.st_gid), (before.st_uid, before.st_gid))

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

    def test_adjacent_release_checksum_is_used_automatically(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / 'algosec-jira-bus-docker-amd64.tar.gz'
            archive.write_bytes(b'verified image archive')
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            archive.with_suffix(archive.suffix + '.sha256').write_text(
                digest + '  ' + archive.name + '\n')
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            log = root / 'docker.log'
            for name, body in (
                    ('id', 'echo 0'),
                    ('uname', 'echo Linux'),
                    ('docker', 'echo "$*" >> "' + str(log) + '"\n'
                     'if [ "$1" = info ] && [ "${2:-}" = --format ]; then echo amd64; fi\n'
                     'if [ "$1" = load ]; then exit 23; fi')):
                path = bin_dir / name
                path.write_text('#!/bin/sh\n' + body + '\n')
                path.chmod(0o700)
            result = subprocess.run(
                ['/bin/sh', str(ROOT / 'packaging/docker/install-docker.sh'),
                 '--image-archive', str(archive), '--data-dir', '/etc'],
                env={**os.environ, 'PATH': str(bin_dir) + os.pathsep + os.environ['PATH']},
                capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Refusing unsafe data directory', result.stderr)
            self.assertNotIn('Image checksum missing', result.stderr)
            self.assertFalse(log.exists())


class DockerBuildScriptTests(unittest.TestCase):
    def test_builds_linux_amd64_archive_from_the_verified_bus_bundle(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source'
            (source / 'algosec_jira_bus').mkdir(parents=True)
            (source / 'algosec_jira_bus/__init__.py').write_text('')
            (source / 'scripts').mkdir()
            (source / 'scripts/setup-linux.sh').write_text('#!/bin/sh\nexit 0\n')
            (source / 'packaging/docker').mkdir(parents=True)
            (source / 'packaging/docker/Dockerfile').write_text('FROM python:3.11-slim-bookworm\n')
            (source / 'packaging/docker/.dockerignore').write_text('**\n!pyproject.toml\n')
            (source / 'pyproject.toml').write_text('')
            bundle = root / 'bus.run'
            builder.build(source, bundle)
            output = root / 'bus-image.tar.gz'
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            log = root / 'docker.log'
            docker = bin_dir / 'docker'
            docker.write_text(
                '#!/bin/sh\n'
                'printf "%s\\n" "$*" >> "' + str(log) + '"\n'
                'if [ "$1" = save ]; then printf image > "$3"; fi\n')
            docker.chmod(0o700)
            result = subprocess.run(
                ['/bin/sh', str(ROOT / 'packaging/docker/build-image.sh'), bundle, output],
                env={**os.environ, 'PATH': str(bin_dir) + os.pathsep + os.environ['PATH']},
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('--platform linux/amd64', log.read_text())
            self.assertTrue(output.is_file())
            self.assertTrue(output.with_suffix(output.suffix + '.sha256').is_file())


class SelfContainedDockerInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.image = self.base / 'algosec-jira-bus-docker-amd64.tar.gz'
        self.image.write_bytes(b'linux-amd64-image')
        self.helper = self.base / 'install-docker.sh'
        self.helper.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$@" > "$DOCKER_INSTALLER_TEST_LOG"\n')
        self.bundle = self.base / 'algosec-jira-bus-0.3.10-docker-amd64.run'
        docker_builder.build(self.image, self.helper, self.bundle)

    def run_bundle(self, *args, env=None):
        return subprocess.run(['/bin/sh', str(self.bundle), *map(str, args)],
                              cwd=self.base, env=env, capture_output=True, text=True)

    def test_extract_verifies_all_embedded_files(self):
        target = self.base / 'unpacked'
        result = self.run_bundle('--extract', target)
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads((target / 'MANIFEST.json').read_text())
        self.assertEqual(set(manifest['files']), {
            'algosec-jira-bus-docker-amd64.tar.gz',
            'algosec-jira-bus-docker-amd64.tar.gz.sha256',
            'install-docker.sh',
            'stage-config.py',
            'upgrade-config.py',
            'bus_conf',
            'bus_update',
            'prepare-fireflow.sh',
            'prepare-jira.sh',
            'create-jira-space.sh',
            'guided-linux-setup.sh',
            'setup-forge.sh',
            'forge-app.tar.gz',
        })
        self.assertEqual((target / 'bus_conf').read_bytes(),
                         (ROOT / 'packaging/docker/bus_conf').read_bytes())
        self.assertEqual((target / 'bus_update').read_bytes(),
                         (ROOT / 'packaging/docker/bus_update').read_bytes())
        self.assertEqual((target / 'guided-linux-setup.sh').read_bytes(),
                         (ROOT / 'scripts/guided-linux-setup.sh').read_bytes())
        with tarfile.open(target / 'forge-app.tar.gz', 'r:gz') as archive:
            names = set(archive.getnames())
        self.assertIn('forge/manifest.yml', names)
        self.assertIn('forge/src/edit.tsx', names)
        self.assertFalse(any('node_modules' in name for name in names))

    def test_help_advertises_one_guided_linux_workflow(self):
        result = self.run_bundle('--help')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--guided', result.stdout)
        self.assertIn('Forge, Jira and the Docker bus', result.stdout)

    def test_one_file_installer_bootstraps_supported_linux_dependencies(self):
        header = self.bundle.read_bytes().split(docker_builder.MARKER, 1)[0].decode()
        for command in ('apt-get install -y ca-certificates curl python3',
                        'dnf -y install ca-certificates curl python3 dnf-plugins-core',
                        'docker-ce docker-ce-cli containerd.io',
                        'systemctl enable --now docker'):
            self.assertIn(command, header)
        self.assertLess(header.index('apt-get install'), header.index('exec python3'))

    def test_payload_tamper_is_rejected_before_extraction(self):
        data = bytearray(self.bundle.read_bytes())
        data[-8] ^= 1
        self.bundle.write_bytes(data)
        target = self.base / 'unpacked'
        result = self.run_bundle('--extract', target)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('checksum mismatch', result.stderr.lower())
        self.assertFalse(target.exists())

    def test_run_dispatches_to_embedded_installer_with_verified_image(self):
        log = self.base / 'install.log'
        bin_dir = self.base / 'bootstrap-bin'
        bin_dir.mkdir()
        (bin_dir / 'python3').symlink_to(sys.executable)
        for name, body in (('uname', 'echo Linux'), ('id', 'echo 0'),
                           ('docker', 'exit 0'), ('systemctl', 'exit 0')):
            path = bin_dir / name
            path.write_text('#!/bin/sh\n' + body + '\n')
            path.chmod(0o700)
        result = self.run_bundle('--data-dir', '/srv/jira-bus',
                                 env={**os.environ,
                                      'PATH': str(bin_dir) + os.pathsep + os.environ['PATH'],
                                      'DOCKER_INSTALLER_TEST_LOG': str(log)})
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = log.read_text().splitlines()
        self.assertEqual(arguments[0], '--image-archive')
        self.assertTrue(arguments[1].endswith('/algosec-jira-bus-docker-amd64.tar.gz'))
        self.assertEqual(arguments[2], '--image-sha256')
        self.assertEqual(arguments[3], hashlib.sha256(self.image.read_bytes()).hexdigest())
        self.assertEqual(arguments[4:], ['--data-dir', '/srv/jira-bus'])


class PreparationHelperImageTests(unittest.TestCase):
    def test_new_release_image_wins_over_an_existing_old_container(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            binary = root / 'docker'
            log = root / 'docker.log'
            binary.write_text(
                '#!/bin/sh\n'
                'printf "%s\\n" "$*" >> "' + str(log) + '"\n'
                'case "$1 $2" in\n'
                '  "image inspect") exit 0;;\n'
                '  "container inspect") exit 0;;\n'
                '  "inspect --format") echo algosec-jira-bus:old;;\n'
                'esac\n'
                'exit 0\n')
            binary.chmod(0o700)
            for helper in ('prepare-fireflow.sh', 'prepare-jira.sh',
                           'create-jira-space.sh'):
                with self.subTest(helper=helper):
                    log.unlink(missing_ok=True)
                    script = root / helper
                    content = (ROOT / 'scripts' / helper).read_text()
                    content = content.replace(
                        'PATH=/usr/sbin:/usr/bin:/sbin:/bin',
                        'PATH=' + str(root) + ':/usr/sbin:/usr/bin:/sbin:/bin')
                    script.write_text(content)
                    result = subprocess.run(
                        ['/bin/sh', str(script), '--help'], env=os.environ,
                        capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    calls = log.read_text()
                    self.assertIn('image inspect algosec-jira-bus:', calls)
                    self.assertNotIn('inspect --format', calls)
                    self.assertIn('run --rm', calls)
                    self.assertNotIn('algosec-jira-bus:old', calls)
                    if helper == 'create-jira-space.sh':
                        self.assertIn('jira_provision --space-only --help', calls)

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
                              env={**os.environ, 'PATH': str(self.bin)},
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

    def test_preserved_config_guard_runs_before_bootstrap_stops_services(self):
        installer = (ROOT / 'scripts/install.sh').read_text()
        bootstrap = (ROOT / 'scripts/setup-linux.sh').read_text()
        preflight = installer.index('check_preserved_config',
                                    installer.index('if [ "${1:-}" = "--preflight" ]'))
        success = installer.index('preflight passed; no system changes made')
        self.assertLess(preflight, success)
        validate = bootstrap.index('install.sh" --preflight')
        stop = bootstrap.index('systemctl disable --now')
        self.assertLess(validate, stop)

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

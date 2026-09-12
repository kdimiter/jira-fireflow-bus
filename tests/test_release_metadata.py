import hashlib
import gzip
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'release_metadata', ROOT / 'scripts/build-release-metadata.py')
release_metadata = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_metadata)
INSTALLER_SPEC = importlib.util.spec_from_file_location(
    'release_installer_builder', ROOT / 'scripts/build-installer.py')
installer_builder = importlib.util.module_from_spec(INSTALLER_SPEC)
INSTALLER_SPEC.loader.exec_module(installer_builder)
DOCKER_INSTALLER_SPEC = importlib.util.spec_from_file_location(
    'release_docker_installer_builder', ROOT / 'scripts/build-docker-installer.py')
docker_installer_builder = importlib.util.module_from_spec(DOCKER_INSTALLER_SPEC)
DOCKER_INSTALLER_SPEC.loader.exec_module(docker_installer_builder)


class ReleaseMetadataTests(unittest.TestCase):
    VERSION = '0.3.11'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'source'
        self.dist = Path(self.temp.name) / 'dist'
        (self.root / 'packaging/docker').mkdir(parents=True)
        (self.root / 'packaging/docker/Dockerfile').write_text(
            'FROM python:3.11-slim@sha256:' + 'c' * 64 + '\n')
        (self.root / 'packaging/docker/install-docker.sh').write_text(
            '#!/bin/sh\nexit 0\n')
        (self.root / 'packaging/docker/stage-config.py').write_bytes(
            (ROOT / 'packaging/docker/stage-config.py').read_bytes())
        (self.root / 'packaging/docker/upgrade-config.py').write_bytes(
            (ROOT / 'packaging/docker/upgrade-config.py').read_bytes())
        (self.root / 'packaging/docker/bus_conf').write_bytes(
            (ROOT / 'packaging/docker/bus_conf').read_bytes())
        (self.root / 'packaging/docker/bus_update').write_bytes(
            (ROOT / 'packaging/docker/bus_update').read_bytes())
        (self.root / 'scripts').mkdir()
        for name in ('prepare-fireflow.sh', 'prepare-jira.sh',
                     'create-jira-space.sh', 'guided-linux-setup.sh',
                     'setup-forge.sh'):
            (self.root / 'scripts' / name).write_bytes(
                (ROOT / 'scripts' / name).read_bytes())
        shutil.copytree(ROOT / 'forge', self.root / 'forge',
                        ignore=shutil.ignore_patterns('node_modules', '.algosec-*'))
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        subprocess.run(
            ['git', '-C', str(self.root), 'config', 'user.email', 'release@example.invalid'],
            check=True)
        subprocess.run(
            ['git', '-C', str(self.root), 'config', 'user.name', 'Release Test'], check=True)
        subprocess.run(['git', '-C', str(self.root), 'add', '.'], check=True)
        subprocess.run(
            ['git', '-C', str(self.root), 'commit', '-q', '-m', 'fixture'], check=True)
        self.REVISION = subprocess.run(
            ['git', '-C', str(self.root), 'rev-parse', 'HEAD'], check=True,
            capture_output=True, text=True).stdout.strip()
        self.dist.mkdir()
        self.layer = b'synthetic uncompressed layer'
        self.layer_digest = hashlib.sha256(self.layer).hexdigest()
        self.config = {
            'architecture': 'amd64',
            'os': 'linux',
            'config': {'Labels': {
                'org.algosec.jira-bus.image': self.VERSION,
                'org.opencontainers.image.version': self.VERSION,
                'org.opencontainers.image.revision': self.REVISION,
            }},
            'rootfs': {'type': 'layers',
                       'diff_ids': ['sha256:' + self.layer_digest]},
        }
        self.config_bytes = json.dumps(
            self.config, sort_keys=True, separators=(',', ':')).encode()
        self.config_digest = hashlib.sha256(self.config_bytes).hexdigest()
        self.IMAGE_ID = 'sha256:' + self.config_digest
        self.archive = self.dist / f'algosec-jira-bus-{self.VERSION}-docker-amd64.tar.gz'
        self._write_archive()
        installer_builder.build(
            self.root, self.dist / f'algosec-jira-bus-{self.VERSION}-linux.run')
        docker_installer_builder.build(
            self.archive, self.root / 'packaging/docker/install-docker.sh',
            self.dist / f'algosec-jira-bus-{self.VERSION}-docker-amd64.run',
            revision=self.REVISION)

    def _manifest(self):
        layer_path = 'blobs/sha256/' + self.layer_digest
        return [{
            'Config': 'blobs/sha256/' + self.config_digest,
            'RepoTags': [f'algosec-jira-bus:{self.VERSION}'],
            'Layers': [layer_path],
            'LayerSources': {'sha256:' + self.layer_digest: {
                'mediaType': 'application/vnd.oci.image.layer.v1.tar',
                'size': len(self.layer),
                'digest': 'sha256:' + self.layer_digest,
            }},
        }]

    def _archive_members(self, manifest=None):
        return [
            ('blobs/sha256/' + self.config_digest, self.config_bytes),
            ('blobs/sha256/' + self.layer_digest, self.layer),
            ('manifest.json', json.dumps(
                self._manifest() if manifest is None else manifest,
                sort_keys=True, separators=(',', ':')).encode()),
        ]

    def _write_archive(self, members=None):
        with tarfile.open(self.archive, 'w:gz') as archive:
            for name, body in members or self._archive_members():
                item = tarfile.TarInfo(name)
                item.size = len(body)
                archive.addfile(item, io.BytesIO(body))
        body = self.archive.read_bytes()
        self.archive.with_suffix(self.archive.suffix + '.sha256').write_text(
            hashlib.sha256(body).hexdigest() + '  ' + self.archive.name + '\n')

    def runner(self, command, **kwargs):
        if command[:3] == ['docker', 'image', 'inspect']:
            document = [{
                'Id': self.IMAGE_ID,
                'Os': 'linux',
                'Architecture': 'amd64',
                'RootFS': {'Type': 'layers',
                           'Layers': ['sha256:' + self.layer_digest]},
                'Config': {'Labels': {
                    'org.algosec.jira-bus.image': self.VERSION,
                    'org.opencontainers.image.version': self.VERSION,
                    'org.opencontainers.image.revision': self.REVISION,
                }},
            }]
            return subprocess.CompletedProcess(command, 0, json.dumps(document), '')
        if command[:3] == ['docker', 'scout', 'sbom']:
            output = Path(command[command.index('--output') + 1])
            output.write_text(json.dumps({
                'spdxVersion': 'SPDX-2.3',
                'name': 'algosec-jira-bus:0.3.11',
                'packages': [{
                    'SPDXID': 'SPDXRef-DocumentRoot',
                    'externalRefs': [{
                        'referenceType': 'purl',
                        'referenceLocator': (
                            'pkg:oci/algosec-jira-bus@' + self.IMAGE_ID +
                            '?repository_url=docker.io&tag=0.3.11'),
                    }],
                }],
                'relationships': [{
                    'spdxElementId': 'SPDXRef-DOCUMENT',
                    'relatedSpdxElement': 'SPDXRef-DocumentRoot',
                    'relationshipType': 'DESCRIBES',
                }],
            }, sort_keys=True) + '\n')
            self.assertEqual(command[-1], 'local://' + self.IMAGE_ID)
            return subprocess.CompletedProcess(command, 0, '', '')
        raise AssertionError(command)

    def test_builds_deterministic_inventory_sbom_and_checksums(self):
        first = release_metadata.build(
            self.root, self.dist, self.VERSION, self.REVISION,
            f'algosec-jira-bus:{self.VERSION}', runner=self.runner)
        first_manifest = (self.dist / 'RELEASE-MANIFEST.json').read_bytes()
        first_sums = (self.dist / 'SHA256SUMS').read_bytes()
        second = release_metadata.build(
            self.root, self.dist, self.VERSION, self.REVISION,
            f'algosec-jira-bus:{self.VERSION}', runner=self.runner)

        self.assertEqual(first, second)
        self.assertEqual(first_manifest, (self.dist / 'RELEASE-MANIFEST.json').read_bytes())
        self.assertEqual(first_sums, (self.dist / 'SHA256SUMS').read_bytes())
        self.assertEqual(first['source']['revision'], self.REVISION)
        self.assertEqual(first['image']['id'], self.IMAGE_ID)
        self.assertEqual(first['image']['platform'], 'linux/amd64')
        self.assertEqual(first['image']['base'], 'python:3.11-slim@sha256:' + 'c' * 64)
        self.assertIn('algosec-jira-bus-0.3.11-sbom.spdx.json', first['artifacts'])
        sum_names = [line.split('  ', 1)[1] for line in first_sums.decode().splitlines()]
        self.assertEqual(sum_names, sorted(sum_names))
        self.assertIn('RELEASE-MANIFEST.json', sum_names)
        self.assertNotIn('SHA256SUMS', sum_names)

    def test_rejects_an_invalid_adjacent_checksum(self):
        checksum = self.dist / f'algosec-jira-bus-{self.VERSION}-linux.run.sha256'
        checksum.write_text('0' * 64 + '  algosec-jira-bus-0.3.11-linux.run\n')
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            release_metadata.build(
                self.root, self.dist, self.VERSION, self.REVISION,
                f'algosec-jira-bus:{self.VERSION}', runner=self.runner)

    def test_includes_optional_html_guide_when_present(self):
        name = 'Jira-FireFlow-Deployment-Guide-uk.html'
        guide = self.dist / name
        guide.write_text('<!doctype html><html lang="uk"></html>\n')
        guide.with_suffix(guide.suffix + '.sha256').write_text(
            hashlib.sha256(guide.read_bytes()).hexdigest() + '  ' + name + '\n')
        manifest = release_metadata.build(
            self.root, self.dist, self.VERSION, self.REVISION,
            f'algosec-jira-bus:{self.VERSION}', runner=self.runner)
        self.assertIn(name, manifest['artifacts'])
        self.assertIn(name + '.sha256', manifest['artifacts'])
        self.assertIn('  ' + name + '\n', (self.dist / 'SHA256SUMS').read_text())

    def test_rejects_image_from_another_revision(self):
        original = self.runner

        def mismatched(command, **kwargs):
            result = original(command, **kwargs)
            if command[:3] == ['docker', 'image', 'inspect']:
                document = json.loads(result.stdout)
                document[0]['Config']['Labels']['org.opencontainers.image.revision'] = 'd' * 40
                result.stdout = json.dumps(document)
            return result

        with self.assertRaisesRegex(ValueError, 'revision label'):
            release_metadata.build(
                self.root, self.dist, self.VERSION, self.REVISION,
                f'algosec-jira-bus:{self.VERSION}', runner=mismatched)

    def test_rejects_archive_a_when_local_image_b_was_inspected(self):
        original = self.runner

        def different_local_image(command, **kwargs):
            result = original(command, **kwargs)
            if command[:3] == ['docker', 'image', 'inspect']:
                document = json.loads(result.stdout)
                document[0]['Id'] = 'sha256:' + 'd' * 64
                result.stdout = json.dumps(document)
            return result

        with self.assertRaisesRegex(ValueError, 'archive image ID'):
            release_metadata.build(
                self.root, self.dist, self.VERSION, self.REVISION,
                f'algosec-jira-bus:{self.VERSION}', runner=different_local_image)

    def test_rejects_stale_native_and_docker_installers(self):
        native = self.dist / f'algosec-jira-bus-{self.VERSION}-linux.run'
        docker = self.dist / f'algosec-jira-bus-{self.VERSION}-docker-amd64.run'
        (self.root / 'README.md').write_text('new release source\n')
        subprocess.run(['git', '-C', str(self.root), 'add', 'README.md'], check=True)
        subprocess.run(
            ['git', '-C', str(self.root), 'commit', '-q', '-m', 'new head'], check=True)
        new_revision = subprocess.run(
            ['git', '-C', str(self.root), 'rev-parse', 'HEAD'], check=True,
            capture_output=True, text=True).stdout.strip()

        with self.assertRaisesRegex(ValueError, 'native installer manifest'):
            release_metadata._verify_native_installer(
                self.root, native, new_revision)
        with self.assertRaisesRegex(ValueError, 'another image archive'):
            release_metadata._verify_docker_installer(
                self.root, docker, self.REVISION, 'f' * 64)

    def test_rejects_docker_installer_stager_from_another_git_tree(self):
        docker = self.dist / f'algosec-jira-bus-{self.VERSION}-docker-amd64.run'
        stager = self.root / 'packaging/docker/stage-config.py'
        stager.write_text('# different release stager\n')
        subprocess.run(['git', '-C', str(self.root), 'add', str(stager)], check=True)
        subprocess.run(
            ['git', '-C', str(self.root), 'commit', '-q', '-m', 'replace stager'], check=True)
        revision = subprocess.run(
            ['git', '-C', str(self.root), 'rev-parse', 'HEAD'], check=True,
            capture_output=True, text=True).stdout.strip()
        docker_installer_builder.build(
            self.archive, self.root / 'packaging/docker/install-docker.sh', docker,
            revision=revision)

        with self.assertRaisesRegex(ValueError, 'stager does not match'):
            release_metadata._verify_docker_installer(
                self.root, docker, revision, hashlib.sha256(self.archive.read_bytes()).hexdigest())

    def _inject_installer_header(self, path, marker):
        header, payload = path.read_bytes().split(marker, 1)
        path.write_bytes(
            header.replace(b'#!/bin/sh\n', b'#!/bin/sh\ntrue # injected command\n', 1) +
            marker + payload)
        body = path.read_bytes()
        path.with_suffix(path.suffix + '.sha256').write_text(
            hashlib.sha256(body).hexdigest() + '  ' + path.name + '\n')

    def test_rejects_native_installer_header_tamper_with_updated_checksum(self):
        path = self.dist / f'algosec-jira-bus-{self.VERSION}-linux.run'
        self._inject_installer_header(path, installer_builder.MARKER)

        with self.assertRaisesRegex(ValueError, 'header does not match'):
            release_metadata.build(
                self.root, self.dist, self.VERSION, self.REVISION,
                f'algosec-jira-bus:{self.VERSION}', runner=self.runner)

    def test_rejects_docker_installer_header_tamper_with_updated_checksum(self):
        path = self.dist / f'algosec-jira-bus-{self.VERSION}-docker-amd64.run'
        self._inject_installer_header(path, docker_installer_builder.MARKER)

        with self.assertRaisesRegex(ValueError, 'header does not match'):
            release_metadata.build(
                self.root, self.dist, self.VERSION, self.REVISION,
                f'algosec-jira-bus:{self.VERSION}', runner=self.runner)

    def test_rejects_archive_swap_after_inventory_hash(self):
        original = release_metadata._verify_primary_artifacts

        def verify_then_swap(directory, version):
            inventory = original(directory, version)
            self._write_archive(list(reversed(self._archive_members())))
            return inventory

        with mock.patch.object(
                release_metadata, '_verify_primary_artifacts', verify_then_swap):
            with self.assertRaisesRegex(ValueError, 'changed after'):
                release_metadata.build(
                    self.root, self.dist, self.VERSION, self.REVISION,
                    f'algosec-jira-bus:{self.VERSION}', runner=self.runner)

    def test_rejects_unsafe_and_duplicate_archive_members(self):
        for unsafe in ('symlink', 'hardlink', 'traversal', 'duplicate'):
            with self.subTest(unsafe=unsafe):
                with tarfile.open(self.archive, 'w:gz') as archive:
                    for name, body in self._archive_members():
                        item = tarfile.TarInfo(name)
                        item.size = len(body)
                        archive.addfile(item, io.BytesIO(body))
                    item = tarfile.TarInfo(
                        'manifest.json' if unsafe == 'duplicate' else
                        '../escape' if unsafe == 'traversal' else 'redirect')
                    if unsafe == 'symlink':
                        item.type = tarfile.SYMTYPE
                        item.linkname = 'manifest.json'
                    elif unsafe == 'hardlink':
                        item.type = tarfile.LNKTYPE
                        item.linkname = 'manifest.json'
                    else:
                        item.size = 2
                    archive.addfile(item, io.BytesIO(b'[]'))
                with self.assertRaisesRegex(ValueError, 'unsafe|duplicate'):
                    release_metadata._docker_archive_metadata(
                        self.archive, f'algosec-jira-bus:{self.VERSION}',
                        self.VERSION, self.REVISION)

    def test_accepts_legacy_docker_save_paths(self):
        layer_id = 'e' * 64
        manifest = [{
            'Config': self.config_digest + '.json',
            'RepoTags': [f'algosec-jira-bus:{self.VERSION}'],
            'Layers': [layer_id + '/layer.tar'],
        }]
        self._write_archive([
            (self.config_digest + '.json', self.config_bytes),
            (layer_id + '/layer.tar', self.layer),
            ('manifest.json', json.dumps(manifest).encode()),
            ('repositories', json.dumps({
                'algosec-jira-bus': {self.VERSION: layer_id},
            }).encode()),
        ])

        metadata = release_metadata._docker_archive_metadata(
            self.archive, f'algosec-jira-bus:{self.VERSION}',
            self.VERSION, self.REVISION)

        self.assertEqual(metadata['id'], self.IMAGE_ID)
        self.assertEqual(metadata['layers'], ['sha256:' + self.layer_digest])

    def test_rejects_a_second_image_in_oci_index(self):
        descriptor = {
            'mediaType': 'application/vnd.oci.image.manifest.v1+json',
            'digest': 'sha256:' + 'f' * 64,
            'size': 2,
        }
        index = {
            'schemaVersion': 2,
            'mediaType': 'application/vnd.oci.image.index.v1+json',
            'manifests': [descriptor, descriptor],
        }
        self._write_archive(self._archive_members() + [
            ('index.json', json.dumps(index).encode()),
            ('oci-layout', b'{"imageLayoutVersion":"1.0.0"}'),
        ])

        with self.assertRaisesRegex(ValueError, 'OCI index must contain one image'):
            release_metadata._docker_archive_metadata(
                self.archive, f'algosec-jira-bus:{self.VERSION}',
                self.VERSION, self.REVISION)

    def test_rejects_pax_metadata_beyond_expansion_limit(self):
        with tarfile.open(
                self.archive, 'w:gz', format=tarfile.PAX_FORMAT,
                pax_headers={'comment': 'x' * 100_000}) as archive:
            item = tarfile.TarInfo('manifest.json')
            item.size = 2
            archive.addfile(item, io.BytesIO(b'[]'))

        with mock.patch.object(release_metadata, 'MAX_DOCKER_ARCHIVE_BYTES', 20_000):
            with self.assertRaisesRegex(ValueError, 'expands beyond'):
                release_metadata._docker_archive_metadata(
                    self.archive, f'algosec-jira-bus:{self.VERSION}',
                    self.VERSION, self.REVISION)

    def test_rejects_one_oversized_pax_metadata_record(self):
        with tarfile.open(
                self.archive, 'w:gz', format=tarfile.PAX_FORMAT,
                pax_headers={'comment': 'x' * (5 * 1024 * 1024)}) as archive:
            item = tarfile.TarInfo('manifest.json')
            item.size = 2
            archive.addfile(item, io.BytesIO(b'[]'))

        with self.assertRaisesRegex(ValueError, 'metadata record exceeds'):
            release_metadata._docker_archive_metadata(
                self.archive, f'algosec-jira-bus:{self.VERSION}',
                self.VERSION, self.REVISION)

    def test_rejects_concatenated_gzip_data_after_tar_end(self):
        self.archive.write_bytes(
            self.archive.read_bytes() + gzip.compress(b'SECRET-TRAILER'))

        with self.assertRaisesRegex(ValueError, 'data after the tar end marker'):
            release_metadata._docker_archive_metadata(
                self.archive, f'algosec-jira-bus:{self.VERSION}',
                self.VERSION, self.REVISION)

    def test_rejects_sbom_for_another_image(self):
        path = self.dist / 'wrong.spdx.json'
        path.write_text(json.dumps({
            'spdxVersion': 'SPDX-2.3',
            'packages': [{
                'SPDXID': 'SPDXRef-DocumentRoot',
                'externalRefs': [{
                    'referenceType': 'purl',
                    'referenceLocator': 'pkg:oci/algosec-jira-bus@sha256:' + 'f' * 64,
                }],
            }],
            'relationships': [{
                'spdxElementId': 'SPDXRef-DOCUMENT',
                'relatedSpdxElement': 'SPDXRef-DocumentRoot',
                'relationshipType': 'DESCRIBES',
            }],
        }))

        with self.assertRaisesRegex(ValueError, 'image identity'):
            release_metadata._normalize_sbom(
                path, self.VERSION, self.IMAGE_ID, 0)

    def test_rejects_multiple_images_and_blob_digest_mismatch(self):
        for malformed in ('multiple', 'digest'):
            with self.subTest(malformed=malformed):
                manifest = self._manifest()
                members = self._archive_members(
                    manifest + manifest if malformed == 'multiple' else manifest)
                if malformed == 'digest':
                    members[1] = (members[1][0], b'tampered layer')
                self._write_archive(members)
                with self.assertRaisesRegex(ValueError, 'exactly one image|blob digest'):
                    release_metadata._docker_archive_metadata(
                        self.archive, f'algosec-jira-bus:{self.VERSION}',
                        self.VERSION, self.REVISION)

    def test_documentation_does_not_claim_unsigned_assets_are_signed(self):
        guide = (ROOT / 'docs/DEPLOYMENT-GUIDE-uk.md').read_text().lower()
        self.assertNotIn('підписан', guide)
        self.assertIn('не окремий\nцифровий підпис', guide)


if __name__ == '__main__':
    unittest.main()

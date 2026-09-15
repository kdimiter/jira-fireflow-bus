#!/usr/bin/env python3
"""Verify release artifacts and write an SBOM, traceability manifest and SHA256SUMS."""
import argparse
import datetime
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SOURCE = 'https://github.com/kdimiter/jira-fireflow-bus/'
MAX_ARTIFACT_BYTES = 10 * 1024 * 1024 * 1024
MAX_DOCKER_ARCHIVE_BYTES = 4 * 1024 * 1024 * 1024
MAX_DOCKER_ARCHIVE_MEMBERS = 4096
MAX_DOCKER_METADATA_BYTES = 4 * 1024 * 1024
MAX_INSTALLER_HEADER_BYTES = 1024 * 1024
MAX_INSTALLER_MEMBERS = 4096
NATIVE_INSTALLER_MARKER = b'\n__ALGOSEC_PAYLOAD__\n'
DOCKER_INSTALLER_MARKER = b'\n__ALGOSEC_DOCKER_PAYLOAD__\n'


class _BoundedTarReader:
    """Prevent tarfile from allocating an unbounded PAX/GNU metadata record."""

    def __init__(self, source):
        self.source = source

    def read(self, size=-1):
        if size < 0 or size > MAX_DOCKER_METADATA_BYTES:
            raise ValueError('tar metadata record exceeds the safety limit')
        return self.source.read(size)

    def seek(self, offset, whence=os.SEEK_SET):
        return self.source.seek(offset, whence)

    def tell(self):
        return self.source.tell()


def _sha256(path):
    flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    try:
        information = os.fstat(descriptor)
        if (not stat.S_ISREG(information.st_mode) or information.st_nlink != 1 or
                information.st_size > MAX_ARTIFACT_BYTES):
            raise ValueError(f'unsafe release artifact: {Path(path).name}')
        for block in iter(lambda: os.read(descriptor, 1024 * 1024), b''):
            digest.update(block)
        return digest.hexdigest(), information.st_size
    finally:
        os.close(descriptor)


def _write_atomic(path, content):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name + '.')
    try:
        with os.fdopen(descriptor, 'wb') as output:
            os.fchmod(output.fileno(), 0o644)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _run(command, runner):
    result = runner(command, capture_output=True, text=True)
    if result.returncode:
        detail = (result.stderr or result.stdout or 'command failed').strip()
        raise RuntimeError(f'{" ".join(command[:3])}: {detail}')
    return result.stdout


def _base_image(root):
    first = (Path(root) / 'packaging/docker/Dockerfile').read_text().splitlines()[0]
    match = re.fullmatch(r'FROM ([^\s@]+@sha256:[0-9a-f]{64})', first)
    if not match:
        raise ValueError('Docker base image must be pinned by SHA-256 digest')
    return match.group(1)


def _verify_primary_artifacts(directory, version):
    primary = [
        f'algosec-jira-bus-{version}-linux.run',
        f'algosec-jira-bus-{version}-docker-amd64.tar.gz',
        f'algosec-jira-bus-{version}-docker-amd64.run',
    ]
    optional = ['Jira-FireFlow-Deployment-Guide-uk.html']
    primary.extend(name for name in optional if (directory / name).exists())
    inventory = {}
    for name in primary:
        path = directory / name
        digest, size = _sha256(path)
        checksum_path = path.with_suffix(path.suffix + '.sha256')
        checksum_text = checksum_path.read_text()
        expected = f'{digest}  {name}\n'
        if checksum_text != expected:
            raise ValueError(f'checksum mismatch: {name}')
        inventory[name] = {'sha256': digest, 'size': size}
        checksum_digest, checksum_size = _sha256(checksum_path)
        inventory[checksum_path.name] = {
            'sha256': checksum_digest, 'size': checksum_size,
        }
    return inventory


def _installer_header(builder_name, payload_digest):
    builder_path = ROOT / 'scripts' / builder_name
    spec = importlib.util.spec_from_file_location(
        '_algosec_release_installer_builder', builder_path)
    if spec is None or spec.loader is None:
        raise ValueError(f'cannot load trusted installer format: {builder_name}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.installer_header(payload_digest)


def _require_zero_tar_tail(expanded, offset, description):
    expanded.seek(offset)
    tail_size = 0
    while True:
        block = expanded.read(1024 * 1024)
        if not block:
            break
        tail_size += len(block)
        if any(block):
            raise ValueError(f'{description} contains data after the tar end marker')
    if tail_size < 1024:
        raise ValueError(f'{description} has an incomplete tar end marker')


def _self_extractor_files(path, marker, builder_name, capture_names):
    flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(path, flags)
    try:
        information = os.fstat(descriptor)
        if (not stat.S_ISREG(information.st_mode) or information.st_nlink != 1 or
                information.st_size > MAX_ARTIFACT_BYTES):
            raise ValueError(f'unsafe release artifact: {Path(path).name}')
        prefix = os.read(descriptor, MAX_INSTALLER_HEADER_BYTES + len(marker))
        marker_at = prefix.find(marker)
        if marker_at < 0 or marker_at > MAX_INSTALLER_HEADER_BYTES:
            raise ValueError(f'invalid self-extractor header: {Path(path).name}')
        header = prefix[:marker_at]
        embedded_digests = re.findall(
            rb"hashlib\.sha256\(payload\)\.hexdigest\(\) != '([0-9a-f]{64})'", header)
        if len(embedded_digests) != 1:
            raise ValueError(f'invalid self-extractor payload digest: {Path(path).name}')
        payload_digest_text = embedded_digests[0].decode('ascii')
        if header != _installer_header(builder_name, payload_digest_text):
            raise ValueError(f'self-extractor header does not match release: {Path(path).name}')
        payload_offset = marker_at + len(marker)
        os.lseek(descriptor, payload_offset, os.SEEK_SET)
        payload_digest = hashlib.sha256()
        for block in iter(lambda: os.read(descriptor, 1024 * 1024), b''):
            payload_digest.update(block)
        if payload_digest.hexdigest() != payload_digest_text:
            raise ValueError(f'self-extractor payload digest mismatch: {Path(path).name}')

        os.lseek(descriptor, payload_offset, os.SEEK_SET)
        files = {}
        captured = {}
        try:
            with os.fdopen(descriptor, 'rb', closefd=False) as source, \
                    gzip.GzipFile(fileobj=source, mode='rb') as compressed, \
                    tempfile.TemporaryFile() as expanded:
                expanded_size = 0
                while True:
                    block = compressed.read(1024 * 1024)
                    if not block:
                        break
                    expanded_size += len(block)
                    if expanded_size > MAX_ARTIFACT_BYTES:
                        raise ValueError('self-extractor expands beyond the safety limit')
                    expanded.write(block)
                expanded.seek(0)
                with tarfile.open(fileobj=_BoundedTarReader(expanded), mode='r:') as archive:
                    while True:
                        member = archive.next()
                        if member is None:
                            break
                        if len(files) >= MAX_INSTALLER_MEMBERS:
                            raise ValueError('self-extractor contains too many members')
                        name = _safe_archive_name(member)
                        if name in files:
                            raise ValueError(f'duplicate self-extractor member: {name}')
                        if member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE):
                            raise ValueError(f'unsafe self-extractor member type: {name}')
                        capture = name in capture_names
                        if capture and member.size > MAX_DOCKER_METADATA_BYTES:
                            raise ValueError(f'self-extractor metadata is too large: {name}')
                        digest, content = _read_archive_file(
                            archive, member, capture=capture)
                        files[name] = {'sha256': digest, 'size': member.size}
                        if capture:
                            captured[name] = content
                    _require_zero_tar_tail(
                        expanded, archive.offset, 'self-extractor payload')
        except (tarfile.TarError, EOFError, OSError):
            raise ValueError(f'invalid self-extractor payload: {Path(path).name}') from None
        return files, captured
    finally:
        os.close(descriptor)


def _source_path_allowed(name, mode):
    path = PurePosixPath(name)
    excluded = {'node_modules', '__pycache__', '.git', '.venv', 'dist', 'build'}
    if mode not in {'100644', '100755'} or any(part in excluded for part in path.parts):
        return False
    if len(path.parts) == 1:
        return name in {'pyproject.toml', 'README.md', 'CHANGELOG.md', 'LICENSE', 'install.sh'}
    if path.parts[0] not in {'algosec_jira_bus', 'scripts', 'packaging', 'forge',
                              'examples', 'docs'}:
        return False
    if path.name.startswith('.') and name != 'packaging/docker/.dockerignore':
        return False
    if path.name in {'secrets.json', 'secrets.env'}:
        return False
    if name in {'packaging/docker/Dockerfile', 'packaging/docker/.dockerignore'}:
        return True
    return path.suffix in {
        '.py', '.sh', '.json', '.md', '.mjs', '.yml', '.yaml', '.tsx', '.ts',
        '.service', '.timer', '.png',
    }


def _git_source_inventory(root, revision):
    result = subprocess.run(
        ['git', '-C', str(root), 'ls-tree', '-r', '-z', revision],
        capture_output=True)
    if result.returncode:
        raise ValueError('cannot read the release revision Git tree')
    inventory = {}
    for record in result.stdout.split(b'\0'):
        if not record:
            continue
        try:
            metadata, raw_name = record.split(b'\t', 1)
            mode, object_type, _ = metadata.decode('ascii').split(' ', 2)
            name = raw_name.decode('utf-8')
        except (ValueError, UnicodeDecodeError):
            raise ValueError('invalid path in the release revision Git tree') from None
        if object_type != 'blob' or not _source_path_allowed(name, mode):
            continue
        blob = subprocess.run(
            ['git', '-C', str(root), 'show', f'{revision}:{name}'], capture_output=True)
        if blob.returncode or len(blob.stdout) > MAX_DOCKER_ARCHIVE_BYTES:
            raise ValueError(f'cannot read release source from Git: {name}')
        inventory[name] = hashlib.sha256(blob.stdout).hexdigest()
    return inventory


def _verify_native_installer(root, path, revision):
    files, captured = _self_extractor_files(
        path, NATIVE_INSTALLER_MARKER, 'build-installer.py', {'MANIFEST.json'})
    if 'MANIFEST.json' not in captured:
        raise ValueError('native installer is missing MANIFEST.json')
    manifest = _json_document(captured['MANIFEST.json'], 'native installer MANIFEST.json')
    if (not isinstance(manifest, dict) or set(manifest) != {
            'format', 'network_required', 'package_index_required', 'revision', 'files'} or
            manifest.get('format') != 1 or manifest.get('network_required') is not True or
            manifest.get('package_index_required') is not False or
            manifest.get('revision') != revision or not isinstance(manifest.get('files'), dict)):
        raise ValueError('native installer manifest does not match release revision')
    expected = _git_source_inventory(root, revision)
    if set(files) != set(expected) | {'MANIFEST.json'} or set(manifest['files']) != set(expected):
        raise ValueError('native installer file inventory does not match release Git tree')
    for name, digest in expected.items():
        if (files[name]['sha256'] != digest or manifest['files'].get(name) != digest):
            raise ValueError(f'native installer source does not match release Git tree: {name}')


def _git_blob_digest(root, revision, name):
    result = subprocess.run(
        ['git', '-C', str(root), 'show', f'{revision}:{name}'], capture_output=True)
    if result.returncode:
        raise ValueError(f'cannot read release source from Git: {name}')
    return hashlib.sha256(result.stdout).hexdigest()


def _verify_docker_installer(root, path, revision, image_digest):
    image_name = 'algosec-jira-bus-docker-amd64.tar.gz'
    checksum_name = image_name + '.sha256'
    stager_name = 'stage-config.py'
    upgrader_name = 'upgrade-config.py'
    bus_conf_name = 'bus_conf'
    bus_update_name = 'bus_update'
    bus_diag_name = 'bus_diag'
    guided_setup_name = 'guided-linux-setup.sh'
    forge_setup_name = 'setup-forge.sh'
    forge_archive_name = 'forge-app.tar.gz'
    prepare_names = {'prepare-fireflow.sh', 'prepare-jira.sh',
                     'create-jira-space.sh'}
    files, captured = _self_extractor_files(
        path, DOCKER_INSTALLER_MARKER, 'build-docker-installer.py',
        {'MANIFEST.json', checksum_name, 'install-docker.sh', stager_name,
         upgrader_name, bus_conf_name, bus_update_name, bus_diag_name, guided_setup_name,
         forge_setup_name, forge_archive_name, *prepare_names})
    expected_names = {
        image_name, checksum_name, 'install-docker.sh', stager_name, upgrader_name,
        bus_conf_name, bus_update_name, bus_diag_name, guided_setup_name, forge_setup_name,
        forge_archive_name,
        *prepare_names,
        'MANIFEST.json'}
    if set(files) != expected_names or 'MANIFEST.json' not in captured:
        raise ValueError('Docker installer file inventory does not match release')
    manifest = _json_document(captured['MANIFEST.json'], 'Docker installer MANIFEST.json')
    if (not isinstance(manifest, dict) or set(manifest) != {
            'format', 'platform', 'revision', 'files'} or manifest.get('format') != 1 or
            manifest.get('platform') != 'linux/amd64' or manifest.get('revision') != revision or
            not isinstance(manifest.get('files'), dict) or
            set(manifest['files']) != expected_names - {'MANIFEST.json'}):
        raise ValueError('Docker installer manifest does not match release revision')
    for name in expected_names - {'MANIFEST.json'}:
        if manifest['files'].get(name) != files[name]['sha256']:
            raise ValueError(f'Docker installer manifest checksum mismatch: {name}')
    if files[image_name]['sha256'] != image_digest:
        raise ValueError('Docker installer embeds another image archive')
    expected_checksum = f'{image_digest}  {image_name}\n'.encode()
    if captured.get(checksum_name) != expected_checksum:
        raise ValueError('Docker installer image checksum file does not match release')
    helper_digest = _git_blob_digest(
        root, revision, 'packaging/docker/install-docker.sh')
    if files['install-docker.sh']['sha256'] != helper_digest:
        raise ValueError('Docker installer helper does not match release Git tree')
    stager_digest = _git_blob_digest(
        root, revision, 'packaging/docker/stage-config.py')
    if files[stager_name]['sha256'] != stager_digest:
        raise ValueError('Docker installer stager does not match release Git tree')
    upgrader_digest = _git_blob_digest(
        root, revision, 'packaging/docker/upgrade-config.py')
    if files[upgrader_name]['sha256'] != upgrader_digest:
        raise ValueError('Docker installer upgrader does not match release Git tree')
    bus_conf_digest = _git_blob_digest(
        root, revision, 'packaging/docker/bus_conf')
    if files[bus_conf_name]['sha256'] != bus_conf_digest:
        raise ValueError('Docker installer bus_conf helper does not match release Git tree')
    bus_update_digest = _git_blob_digest(
        root, revision, 'packaging/docker/bus_update')
    if files[bus_update_name]['sha256'] != bus_update_digest:
        raise ValueError('Docker installer bus_update helper does not match release Git tree')
    bus_diag_digest = _git_blob_digest(
        root, revision, 'packaging/docker/bus_diag')
    if files[bus_diag_name]['sha256'] != bus_diag_digest:
        raise ValueError('Docker installer bus_diag helper does not match release Git tree')
    for name in prepare_names:
        digest = _git_blob_digest(root, revision, 'scripts/' + name)
        if files[name]['sha256'] != digest:
            raise ValueError('Docker installer preparation helper does not match release Git tree')
    for name in (guided_setup_name, forge_setup_name):
        digest = _git_blob_digest(root, revision, 'scripts/' + name)
        if files[name]['sha256'] != digest:
            raise ValueError('Docker installer guided helper does not match release Git tree')

    result = subprocess.run(
        ['git', '-C', str(root), 'ls-tree', '-r', '--name-only', revision, 'forge'],
        capture_output=True, text=True)
    if result.returncode:
        raise ValueError('cannot read Forge source inventory from release Git tree')
    expected_forge = {name for name in result.stdout.splitlines() if name}
    found_forge = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(captured[forge_archive_name]), mode='r:gz') as archive:
            for member in archive:
                name = _safe_archive_name(member)
                if (name in found_forge or name not in expected_forge or
                        member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE)):
                    raise ValueError('unsafe Forge source bundle inventory')
                content = archive.extractfile(member).read()
                found_forge[name] = hashlib.sha256(content).hexdigest()
    except (tarfile.TarError, EOFError, OSError, KeyError):
        raise ValueError('invalid Forge source bundle') from None
    if set(found_forge) != expected_forge:
        raise ValueError('Forge source bundle does not match release Git tree')
    for name, digest in found_forge.items():
        if digest != _git_blob_digest(root, revision, name):
            raise ValueError('Forge source bundle differs from release Git tree')


def _json_document(content, description):
    def reject_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'duplicate JSON key in {description}: {key}')
            result[key] = value
        return result

    try:
        return json.loads(content, object_pairs_hook=reject_duplicate_keys)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError(f'invalid JSON in Docker archive {description}') from None


def _safe_archive_name(member):
    name = member.name
    normalized = name.rstrip('/') if member.isdir() else name
    path = PurePosixPath(normalized)
    if (not normalized or name.startswith('/') or '\\' in name or
            name.startswith('./') or normalized != str(path) or
            any(part in ('', '.', '..') for part in path.parts)):
        raise ValueError(f'unsafe Docker archive member: {name!r}')
    if member.isdir():
        if name not in (normalized, normalized + '/'):
            raise ValueError(f'unsafe Docker archive member: {name!r}')
    elif name != normalized:
        raise ValueError(f'unsafe Docker archive member: {name!r}')
    return normalized


def _read_archive_file(archive, member, *, capture=False):
    source = archive.extractfile(member)
    if source is None:
        raise ValueError(f'cannot read Docker archive member: {member.name}')
    digest = hashlib.sha256()
    content = bytearray() if capture else None
    consumed = 0
    with source:
        while True:
            block = source.read(min(1024 * 1024, member.size - consumed + 1))
            if not block:
                break
            consumed += len(block)
            if consumed > member.size:
                raise ValueError(f'Docker archive member exceeds declared size: {member.name}')
            digest.update(block)
            if capture:
                content.extend(block)
    if consumed != member.size:
        raise ValueError(f'truncated Docker archive member: {member.name}')
    return digest.hexdigest(), bytes(content) if capture else None


def _docker_archive_metadata(path, image_ref, version, revision):
    """Validate a bounded docker-save archive and return its immutable image identity."""
    flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(path, flags)
    try:
        information = os.fstat(descriptor)
        if (not stat.S_ISREG(information.st_mode) or information.st_nlink != 1 or
                information.st_size > MAX_ARTIFACT_BYTES):
            raise ValueError(f'unsafe release artifact: {Path(path).name}')
        compressed_digest = hashlib.sha256()
        for block in iter(lambda: os.read(descriptor, 1024 * 1024), b''):
            compressed_digest.update(block)
        os.lseek(descriptor, 0, os.SEEK_SET)
        members = {}
        total_size = 0
        metadata = {}
        oci_manifest_content = None
        try:
            with os.fdopen(descriptor, 'rb', closefd=False) as source, \
                    gzip.GzipFile(fileobj=source, mode='rb') as compressed, \
                    tempfile.TemporaryFile() as expanded:
                expanded_size = 0
                while True:
                    block = compressed.read(1024 * 1024)
                    if not block:
                        break
                    expanded_size += len(block)
                    if expanded_size > MAX_DOCKER_ARCHIVE_BYTES:
                        raise ValueError('Docker archive expands beyond the safety limit')
                    expanded.write(block)
                expanded.seek(0)
                with tarfile.open(fileobj=_BoundedTarReader(expanded), mode='r:') as archive:
                    while True:
                        member = archive.next()
                        if member is None:
                            break
                        if len(members) >= MAX_DOCKER_ARCHIVE_MEMBERS:
                            raise ValueError('Docker archive contains too many members')
                        name = _safe_archive_name(member)
                        if name in members:
                            raise ValueError(f'duplicate Docker archive member: {name}')
                        if member.isdir():
                            members[name] = {'kind': 'directory'}
                            continue
                        if member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE):
                            raise ValueError(f'unsafe Docker archive member type: {name}')
                        if member.size < 0:
                            raise ValueError(f'unsafe Docker archive member size: {name}')
                        total_size += member.size
                        if total_size > MAX_DOCKER_ARCHIVE_BYTES:
                            raise ValueError('Docker archive expands beyond the safety limit')
                        capture = name in {
                            'manifest.json', 'index.json', 'oci-layout', 'repositories',
                        }
                        if capture and member.size > MAX_DOCKER_METADATA_BYTES:
                            raise ValueError(f'Docker archive metadata is too large: {name}')
                        digest, content = _read_archive_file(
                            archive, member, capture=capture)
                        blob = re.fullmatch(r'blobs/sha256/([0-9a-f]{64})', name)
                        if blob and digest != blob.group(1):
                            raise ValueError(f'Docker archive blob digest mismatch: {name}')
                        members[name] = {
                            'kind': 'file', 'size': member.size, 'sha256': digest,
                            'member': member,
                        }
                        if capture:
                            metadata[name] = content

                    manifest_content = metadata.get('manifest.json')
                    if manifest_content is None:
                        raise ValueError('Docker archive is missing manifest.json')
                    manifest = _json_document(manifest_content, 'manifest.json')
                    if not isinstance(manifest, list) or len(manifest) != 1:
                        raise ValueError('Docker archive must contain exactly one image')
                    entry = manifest[0]
                    if not isinstance(entry, dict):
                        raise ValueError('unexpected Docker archive manifest structure')
                    required = {'Config', 'RepoTags', 'Layers'}
                    allowed = required | {'LayerSources'}
                    if not required.issubset(entry) or not set(entry).issubset(allowed):
                        raise ValueError('unexpected Docker archive manifest structure')
                    if ('LayerSources' in entry and
                            not isinstance(entry['LayerSources'], dict)):
                        raise ValueError('unexpected Docker archive manifest structure')
                    if entry['RepoTags'] != [image_ref]:
                        raise ValueError('Docker archive must contain exactly one expected image tag')
                    config_path = entry['Config']
                    layer_paths = entry['Layers']
                    if (not isinstance(config_path, str) or
                            not isinstance(layer_paths, list) or not layer_paths or
                            not all(isinstance(item, str) for item in layer_paths)):
                        raise ValueError('unexpected Docker archive manifest structure')
                    referenced = [config_path, *layer_paths]
                    if any(name not in members or members[name]['kind'] != 'file'
                           for name in referenced):
                        raise ValueError('Docker archive manifest references a missing file')
                    config_member = members[config_path]
                    if config_member['size'] > MAX_DOCKER_METADATA_BYTES:
                        raise ValueError('Docker image configuration is too large')
                    _, config_content = _read_archive_file(
                        archive, config_member['member'], capture=True)

                    has_index = 'index.json' in metadata
                    has_layout = 'oci-layout' in metadata
                    if has_index != has_layout:
                        raise ValueError('incomplete OCI metadata in Docker archive')
                    if has_index:
                        layout = _json_document(metadata['oci-layout'], 'oci-layout')
                        if layout != {'imageLayoutVersion': '1.0.0'}:
                            raise ValueError('unexpected Docker archive OCI layout')
                        index = _json_document(metadata['index.json'], 'index.json')
                        if (not isinstance(index, dict) or index.get('schemaVersion') != 2 or
                                index.get('mediaType') !=
                                'application/vnd.oci.image.index.v1+json' or
                                not isinstance(index.get('manifests'), list) or
                                len(index['manifests']) != 1):
                            raise ValueError('Docker archive OCI index must contain one image')
                        descriptor_entry = index['manifests'][0]
                        if not isinstance(descriptor_entry, dict):
                            raise ValueError('unexpected Docker archive OCI index structure')
                        descriptor_digest = descriptor_entry.get('digest')
                        if (descriptor_entry.get('mediaType') !=
                                'application/vnd.oci.image.manifest.v1+json' or
                                not isinstance(descriptor_digest, str) or
                                not re.fullmatch(r'sha256:[0-9a-f]{64}', descriptor_digest)):
                            raise ValueError('unexpected Docker archive OCI index structure')
                        oci_path = 'blobs/sha256/' + descriptor_digest.removeprefix('sha256:')
                        oci_member = members.get(oci_path)
                        if (not oci_member or oci_member['kind'] != 'file' or
                                descriptor_entry.get('size') != oci_member['size'] or
                                oci_member['size'] > MAX_DOCKER_METADATA_BYTES):
                            raise ValueError('Docker archive OCI index references an invalid image')
                        _, oci_manifest_content = _read_archive_file(
                            archive, oci_member['member'], capture=True)

                    if 'repositories' in metadata:
                        repositories = _json_document(
                            metadata['repositories'], 'repositories')
                        try:
                            repository, tag = image_ref.rsplit(':', 1)
                        except ValueError:
                            raise ValueError('release image reference must include a tag') from None
                        top_layer_path = PurePosixPath(layer_paths[-1])
                        top_layer = (top_layer_path.name if
                                     str(top_layer_path).startswith('blobs/sha256/') else
                                     top_layer_path.parts[0])
                        if repositories != {repository: {tag: top_layer}}:
                            raise ValueError('Docker archive repositories metadata has another tag')
                    _require_zero_tar_tail(
                        expanded, archive.offset, 'Docker archive')
        except (tarfile.TarError, EOFError, OSError):
            raise ValueError('invalid or truncated Docker archive') from None

        config_digest = hashlib.sha256(config_content).hexdigest()
        modern_config = re.fullmatch(r'blobs/sha256/([0-9a-f]{64})', config_path)
        legacy_config = re.fullmatch(r'([0-9a-f]{64})\.json', config_path)
        expected_config_digest = (
            modern_config.group(1) if modern_config else
            legacy_config.group(1) if legacy_config else None)
        if expected_config_digest is None:
            raise ValueError('unsupported Docker image configuration path')
        if config_digest != expected_config_digest:
            raise ValueError('Docker archive configuration digest mismatch')

        if oci_manifest_content is not None:
            oci_manifest = _json_document(oci_manifest_content, 'OCI image manifest')
            try:
                oci_config = oci_manifest['config']
                oci_layers = oci_manifest['layers']
            except (KeyError, TypeError):
                raise ValueError('unexpected Docker archive OCI manifest structure') from None
            expected_layers = [
                {
                    'digest': 'sha256:' + members[name]['sha256'],
                    'size': members[name]['size'],
                }
                for name in layer_paths
            ]
            if (oci_manifest.get('schemaVersion') != 2 or
                    oci_manifest.get('mediaType') !=
                    'application/vnd.oci.image.manifest.v1+json' or
                    not isinstance(oci_config, dict) or
                    oci_config.get('digest') != 'sha256:' + config_digest or
                    oci_config.get('size') != len(config_content) or
                    not isinstance(oci_layers, list) or len(oci_layers) != len(expected_layers)):
                raise ValueError('Docker archive OCI manifest does not match image')
            for actual, expected in zip(oci_layers, expected_layers):
                if (not isinstance(actual, dict) or
                        actual.get('digest') != expected['digest'] or
                        actual.get('size') != expected['size']):
                    raise ValueError('Docker archive OCI manifest does not match image')

        config = _json_document(config_content, config_path)
        try:
            labels = config['config']['Labels']
            rootfs = config['rootfs']
            diff_ids = rootfs['diff_ids']
        except (KeyError, TypeError):
            raise ValueError('unexpected Docker image configuration structure') from None
        if config.get('os') != 'linux' or config.get('architecture') != 'amd64':
            raise ValueError('release archive image must be linux/amd64')
        if (not isinstance(labels, dict) or not isinstance(rootfs, dict) or
                labels.get('org.algosec.jira-bus.image') != version or
                labels.get('org.opencontainers.image.version') != version):
            raise ValueError('Docker archive version label does not match release')
        if labels.get('org.opencontainers.image.revision') != revision:
            raise ValueError('Docker archive revision label does not match release')
        if (rootfs.get('type') != 'layers' or not isinstance(diff_ids, list) or
                len(diff_ids) != len(layer_paths) or
                not all(isinstance(value, str) and
                        re.fullmatch(r'sha256:[0-9a-f]{64}', value)
                        for value in diff_ids)):
            raise ValueError('unexpected Docker image rootfs structure')
        layer_digests = ['sha256:' + members[name]['sha256'] for name in layer_paths]
        if layer_digests != diff_ids:
            raise ValueError('Docker archive layer digest does not match image configuration')
        return {
            'id': 'sha256:' + config_digest,
            'platform': 'linux/amd64',
            'layers': diff_ids,
            'archive_sha256': compressed_digest.hexdigest(),
        }
    finally:
        os.close(descriptor)


def _image_metadata(image_ref, version, revision, runner):
    raw = _run(['docker', 'image', 'inspect', image_ref], runner)
    try:
        images = json.loads(raw)
        image = images[0] if len(images) == 1 else None
        labels = image['Config']['Labels']
        rootfs = image['RootFS']
    except (KeyError, TypeError, IndexError, json.JSONDecodeError):
        raise ValueError('docker image inspect returned invalid metadata') from None
    if image.get('Os') != 'linux' or image.get('Architecture') != 'amd64':
        raise ValueError('release image must be linux/amd64')
    if (not isinstance(labels, dict) or
            labels.get('org.algosec.jira-bus.image') != version or
            labels.get('org.opencontainers.image.version') != version):
        raise ValueError('Docker image version label does not match release')
    if labels.get('org.opencontainers.image.revision') != revision:
        raise ValueError('Docker image revision label does not match release')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', image.get('Id', '')):
        raise ValueError('Docker image ID is not a SHA-256 digest')
    layers = rootfs.get('Layers') if isinstance(rootfs, dict) else None
    if (not isinstance(rootfs, dict) or rootfs.get('Type') != 'layers' or
            not isinstance(layers, list) or not layers or
            not all(isinstance(value, str) and
                    re.fullmatch(r'sha256:[0-9a-f]{64}', value) for value in layers)):
        raise ValueError('docker image inspect returned invalid rootfs metadata')
    return {
        'reference': image_ref,
        'id': image['Id'],
        'platform': 'linux/amd64',
        'layers': layers,
    }


def _normalize_sbom(path, version, image_id, source_timestamp):
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        raise ValueError('Docker Scout did not produce a valid JSON SBOM') from None
    if document.get('spdxVersion') != 'SPDX-2.3':
        raise ValueError('Docker Scout did not produce SPDX 2.3')
    packages = document.get('packages')
    roots = ([package for package in packages
              if isinstance(package, dict) and
              package.get('SPDXID') == 'SPDXRef-DocumentRoot']
             if isinstance(packages, list) else [])
    if len(roots) != 1:
        raise ValueError('Docker Scout SBOM has no unique document root package')
    references = roots[0].get('externalRefs')
    purls = ([reference.get('referenceLocator') for reference in references
              if isinstance(reference, dict) and reference.get('referenceType') == 'purl' and
              isinstance(reference.get('referenceLocator'), str) and
              reference['referenceLocator'].startswith('pkg:oci/')]
             if isinstance(references, list) else [])
    expected_digest = image_id.removeprefix('sha256:')
    if (len(purls) != 1 or not re.fullmatch(
            rf'pkg:oci/[^@?]+@sha256:{expected_digest}(?:\?[^#]*)?', purls[0])):
        raise ValueError('Docker Scout SBOM image identity does not match release image')
    relationships = document.get('relationships')
    describes_root = ([relationship for relationship in relationships
                       if isinstance(relationship, dict) and
                       relationship.get('spdxElementId') == 'SPDXRef-DOCUMENT' and
                       relationship.get('relatedSpdxElement') == 'SPDXRef-DocumentRoot' and
                       relationship.get('relationshipType') == 'DESCRIBES']
                      if isinstance(relationships, list) else [])
    if len(describes_root) != 1:
        raise ValueError('Docker Scout SBOM does not describe its document root')
    created = datetime.datetime.fromtimestamp(
        source_timestamp, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    document.setdefault('creationInfo', {})['created'] = created
    document['name'] = f'algosec-jira-bus-{version}-linux-amd64'
    document['documentNamespace'] = (
        f'{SOURCE}releases/download/v{version}/'
        f'algosec-jira-bus-{version}-sbom.spdx.json#{image_id.removeprefix("sha256:")}')
    for key, fields in {
            'packages': ('SPDXID', 'name', 'versionInfo'),
            'files': ('SPDXID', 'fileName'),
            'relationships': ('spdxElementId', 'relationshipType', 'relatedSpdxElement'),
    }.items():
        values = document.get(key)
        if isinstance(values, list):
            document[key] = sorted(
                values, key=lambda value: tuple(str(value.get(field, '')) for field in fields))
    _write_atomic(path, (json.dumps(document, indent=2, sort_keys=True) + '\n').encode())


def build(root, directory, version, revision, image_ref, *, source_timestamp=0,
          runner=subprocess.run):
    root, directory = Path(root), Path(directory)
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version):
        raise ValueError('version must use MAJOR.MINOR.PATCH')
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('revision must be a full Git commit SHA')
    if type(source_timestamp) is not int or source_timestamp < 0:
        raise ValueError('source timestamp must be a non-negative integer')

    inventory = _verify_primary_artifacts(directory, version)
    native_installer_name = f'algosec-jira-bus-{version}-linux.run'
    docker_installer_name = f'algosec-jira-bus-{version}-docker-amd64.run'
    archive_name = f'algosec-jira-bus-{version}-docker-amd64.tar.gz'
    _verify_native_installer(root, directory / native_installer_name, revision)
    archive_image = _docker_archive_metadata(
        directory / archive_name, image_ref, version, revision)
    if archive_image['archive_sha256'] != inventory[archive_name]['sha256']:
        raise ValueError('Docker archive changed after release checksum verification')
    _verify_docker_installer(
        root, directory / docker_installer_name, revision,
        inventory[archive_name]['sha256'])
    image = _image_metadata(image_ref, version, revision, runner)
    if archive_image['id'] != image['id']:
        raise ValueError('Docker archive image ID does not match inspected local image')
    if archive_image['platform'] != image['platform']:
        raise ValueError('Docker archive platform does not match inspected local image')
    if archive_image['layers'] != image['layers']:
        raise ValueError('Docker archive rootfs does not match inspected local image')
    image['archive'] = archive_name
    image['archive_sha256'] = inventory[archive_name]['sha256']
    image['base'] = _base_image(root)

    sbom_name = f'algosec-jira-bus-{version}-sbom.spdx.json'
    sbom_path = directory / sbom_name
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix='.sbom.', suffix='.json')
    os.close(descriptor)
    try:
        _run(['docker', 'scout', 'sbom', '--format', 'spdx', '--output', temporary,
              f'local://{image["id"]}'], runner)
        _normalize_sbom(Path(temporary), version, image['id'], source_timestamp)
        os.replace(temporary, sbom_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    sbom_digest, sbom_size = _sha256(sbom_path)
    inventory[sbom_name] = {'sha256': sbom_digest, 'size': sbom_size}

    manifest = {
        'format': 1,
        'version': version,
        'source': {
            'repository': SOURCE,
            'revision': revision,
            'commit_timestamp': source_timestamp,
        },
        'image': image,
        'artifacts': {name: inventory[name] for name in sorted(inventory)},
        'verification': {
            'algorithm': 'SHA-256',
            'aggregate': 'SHA256SUMS',
            'signature': None,
        },
    }
    manifest_path = directory / 'RELEASE-MANIFEST.json'
    _write_atomic(manifest_path, (json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode())
    manifest_digest, _ = _sha256(manifest_path)
    checksums = {name: values['sha256'] for name, values in inventory.items()}
    checksums[manifest_path.name] = manifest_digest
    _write_atomic(directory / 'SHA256SUMS', ''.join(
        f'{checksums[name]}  {name}\n' for name in sorted(checksums)).encode())
    return manifest


def _git_value(root, *arguments):
    result = subprocess.run(['git', '-C', str(root), *arguments], check=True,
                            capture_output=True, text=True)
    return result.stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--image', required=True)
    args = parser.parse_args()
    if _git_value(ROOT, 'status', '--porcelain', '--untracked-files=no'):
        raise SystemExit('Refusing release metadata for a dirty tracked working tree')
    revision = _git_value(ROOT, 'rev-parse', 'HEAD')
    source_timestamp = int(_git_value(ROOT, 'show', '-s', '--format=%ct', 'HEAD'))
    manifest = build(ROOT, args.directory, args.version, revision, args.image,
                     source_timestamp=source_timestamp)
    print(f'{args.directory}: verified {len(manifest["artifacts"])} artifacts; '
          'wrote SPDX SBOM, RELEASE-MANIFEST.json and SHA256SUMS')


if __name__ == '__main__':
    main()

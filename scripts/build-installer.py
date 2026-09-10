#!/usr/bin/env python3
"""Build a portable, checksum-verified source installer without proprietary binaries."""
import argparse
import hashlib
import io
import json
import re
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MARKER = b'\n__ALGOSEC_PAYLOAD__\n'
EXTRACTOR = r'''
import hashlib, io, json, os, pathlib, shutil, stat, subprocess, sys, tarfile, tempfile
bundle = pathlib.Path(sys.argv[1]).read_bytes()
payload = bundle.split(b'\n__ALGOSEC_PAYLOAD__\n', 1)[1]
if hashlib.sha256(payload).hexdigest() != '__DIGEST__':
    raise SystemExit('Installer checksum mismatch; nothing extracted.')
args = sys.argv[2:]
if args == ['--help']:
    print('Usage: sh installer.run --connector-wheel /secure/path/connector.whl --connector-sha256 HEX [wizard options]\n       sh installer.run --extract NEW_DIRECTORY\nRequires Linux, root, systemd, Python 3.11+ and an independently verified connector digest.\n--extract only verifies and unpacks; no system changes.')
    raise SystemExit(0)
extract_only = bool(args and args[0] == '--extract')
if extract_only and len(args) != 2:
    raise SystemExit('Usage: --extract NEW_DIRECTORY')
setup_args = []
connector = None
if extract_only:
    destination = pathlib.Path(args[1]).absolute()
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
else:
    if len(args) < 4 or args[0] != '--connector-wheel' or args[2] != '--connector-sha256':
        raise SystemExit('Installation requires --connector-wheel PATH --connector-sha256 HEX')
    connector = pathlib.Path(args[1]).expanduser().absolute()
    connector_digest = args[3].lower()
    if (not connector.name.startswith('algosec_host_mcp-')
            or not connector.name.endswith('-py3-none-any.whl')):
        raise SystemExit('Connector must be a universal algosec_host_mcp wheel')
    if len(connector_digest) != 64 or any(c not in '0123456789abcdef' for c in connector_digest):
        raise SystemExit('Connector SHA256 must be exactly 64 hexadecimal characters')
    setup_args = args[4:]
    destination = pathlib.Path(tempfile.mkdtemp(prefix='algosec-setup-'))
try:
    with tarfile.open(fileobj=io.BytesIO(payload), mode='r:gz') as archive:
        seen = set()
        for member in archive:
            name = pathlib.PurePosixPath(member.name)
            if name.is_absolute() or '..' in name.parts or not member.isfile() or member.name in seen:
                raise ValueError('Unsafe archive member: ' + member.name)
            seen.add(member.name)
            target = destination.joinpath(*name.parts)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with archive.extractfile(member) as source, target.open('xb') as output:
                shutil.copyfileobj(source, output)
            target.chmod(0o600)
    manifest = json.loads((destination / 'MANIFEST.json').read_text())
    actual = {str(p.relative_to(destination)) for p in destination.rglob('*') if p.is_file()}
    if actual != set(manifest['files']) | {'MANIFEST.json'}:
        raise ValueError('Manifest file inventory mismatch')
    for name, expected in manifest['files'].items():
        if hashlib.sha256((destination / name).read_bytes()).hexdigest() != expected:
            raise ValueError('Manifest checksum mismatch: ' + name)
    if extract_only:
        print('Verified installer extracted to ' + str(destination))
    else:
        vendor = destination / 'vendor'
        vendor.mkdir(mode=0o700)
        staged = vendor / connector.name
        digest = hashlib.sha256()
        source_fd = os.open(connector, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(source_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 100 * 1024 * 1024:
                raise SystemExit('Connector must be a regular file smaller than 100 MiB')
            with os.fdopen(source_fd, 'rb') as source, staged.open('xb') as output:
                source_fd = None
                for block in iter(lambda: source.read(1024 * 1024), b''):
                    digest.update(block)
                    output.write(block)
            staged.chmod(0o600)
        finally:
            if source_fd is not None:
                os.close(source_fd)
        if digest.hexdigest() != connector_digest:
            staged.unlink(missing_ok=True)
            raise SystemExit('Connector SHA256 mismatch; installation stopped')
        environment = dict(os.environ, ALGOSEC_CONNECTOR_SOURCE=str(staged),
                           ALGOSEC_CONNECTOR_SHA256=connector_digest)
        result = subprocess.run(['sh', str(destination / 'scripts/setup-linux.sh'), *setup_args],
                                env=environment)
        raise SystemExit(result.returncode)
finally:
    if not extract_only:
        shutil.rmtree(destination)
'''


def source_files(root):
    """An allowlist avoids accidentally distributing state, secrets or environment files."""
    selected = []
    for top in ('algosec_jira_bus', 'scripts', 'packaging', 'forge', 'examples', 'docs'):
        for path in (root / top).rglob('*'):
            relative = path.relative_to(root)
            if path.is_symlink() or not path.is_file():
                continue
            if any(part in {'node_modules', '__pycache__', '.git', '.venv', 'dist', 'build'} for part in relative.parts):
                continue
            if path.suffix not in {'.py', '.sh', '.json', '.md', '.yml', '.yaml', '.tsx', '.ts', '.service', '.timer', '.png'} and str(relative) not in {'packaging/docker/Dockerfile', 'packaging/docker/.dockerignore'}:
                continue
            if (path.name.startswith('.') and str(relative) != 'packaging/docker/.dockerignore') or path.name in {'secrets.json', 'secrets.env'}:
                continue
            selected.append(path)
    selected.extend(
        root / name
        for name in ('pyproject.toml', 'README.md', 'CHANGELOG.md', 'install.sh')
        if (root / name).exists()
    )
    return sorted(selected)


def build(root, output):
    files = {str(path.relative_to(root)): path.read_bytes() for path in source_files(root)}
    if 'forge/manifest.yml' in files:
        manifest_text = files['forge/manifest.yml'].decode()
        manifest_text = re.sub(r'(?m)^  id: ari:cloud:ecosystem::app/[^\n]+$', '  id: ari:cloud:ecosystem::app/00000000-0000-0000-0000-000000000000', manifest_text)
        files['forge/manifest.yml'] = manifest_text.encode()
    manifest = {'format': 1, 'network_required': True, 'files': {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}}
    files['MANIFEST.json'] = (json.dumps(manifest, indent=2) + '\n').encode()
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as archive:
        for name, data in sorted(files.items()):
            item = tarfile.TarInfo(name)
            item.size = len(data)
            item.mode = 0o600
            archive.addfile(item, io.BytesIO(data))
    payload = stream.getvalue()
    extractor = EXTRACTOR.replace('__DIGEST__', hashlib.sha256(payload).hexdigest())
    header = '#!/bin/sh\nset -eu\ncommand -v python3 >/dev/null 2>&1 || { echo "Python 3 required to unpack installer" >&2; exit 1; }\nexec python3 - "$0" "$@" <<\'ALGOSEC_PYTHON\'\n' + extractor + '\nALGOSEC_PYTHON\n'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(header.encode() + MARKER + payload)
    output.chmod(0o700)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + '.sha256').write_text(digest + '  ' + output.name + '\n')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT.parent / 'outputs/algosec-jira-bus-linux.run')
    args = parser.parse_args()
    manifest = build(ROOT, args.output)
    print(f'{args.output}: {len(manifest["files"])} verified source files; connector wheel is supplied only at installation time')

if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Build one checksum-verified installer containing the ready linux/amd64 image."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile


ROOT = Path(__file__).resolve().parents[1]
MARKER = b'\n__ALGOSEC_DOCKER_PAYLOAD__\n'
IMAGE_NAME = 'algosec-jira-bus-docker-amd64.tar.gz'
STAGER_NAME = 'stage-config.py'
UPGRADER_NAME = 'upgrade-config.py'
BUS_CONF_NAME = 'bus_conf'
BUS_UPDATE_NAME = 'bus_update'
PREPARE_NAMES = ('prepare-fireflow.sh', 'prepare-jira.sh', 'create-jira-space.sh')
GUIDED_SETUP_NAME = 'guided-linux-setup.sh'
FORGE_SETUP_NAME = 'setup-forge.sh'
FORGE_ARCHIVE_NAME = 'forge-app.tar.gz'
HOST_SETUP = r'''if [ "${1:-}" != "--help" ] && [ "${1:-}" != "--extract" ]; then
    [ "$(uname -s)" = Linux ] || { echo 'Linux host required.' >&2; exit 1; }
    [ "$(id -u)" -eq 0 ] || { echo 'Run the installer with sudo.' >&2; exit 1; }
    if ! command -v python3 >/dev/null 2>&1 || ! command -v docker >/dev/null 2>&1; then
        [ -r /etc/os-release ] || { echo 'Unsupported Linux: /etc/os-release is missing.' >&2; exit 1; }
        . /etc/os-release
        case "$ID" in
            ubuntu|debian)
                apt-get update
                apt-get install -y ca-certificates curl python3
                if ! command -v docker >/dev/null 2>&1; then
                    install -m 0755 -d /etc/apt/keyrings
                    curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
                    chmod a+r /etc/apt/keyrings/docker.asc
                    SUITE=${UBUNTU_CODENAME:-$VERSION_CODENAME}
                    cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/$ID
Suites: $SUITE
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
                    apt-get update
                    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
                fi
                ;;
            rhel|rocky|almalinux)
                dnf -y install ca-certificates curl python3 dnf-plugins-core
                if ! command -v docker >/dev/null 2>&1; then
                    case "$ID" in rhel) DOCKER_RPM_FAMILY=rhel;; *) DOCKER_RPM_FAMILY=centos;; esac
                    dnf config-manager --add-repo "https://download.docker.com/linux/$DOCKER_RPM_FAMILY/docker-ce.repo"
                    dnf -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
                fi
                ;;
            *)
                echo "Unsupported Linux distribution: $ID. Install Docker Engine and Python 3, then rerun." >&2
                exit 1
                ;;
        esac
    fi
    command -v python3 >/dev/null 2>&1 || { echo 'Python 3 installation failed.' >&2; exit 1; }
    command -v docker >/dev/null 2>&1 || { echo 'Docker Engine installation failed.' >&2; exit 1; }
    command -v systemctl >/dev/null 2>&1 || { echo 'systemd is required to manage Docker.' >&2; exit 1; }
    systemctl enable --now docker
    docker info >/dev/null
fi
'''
EXTRACTOR = r'''
import hashlib, io, json, os, pathlib, shutil, subprocess, sys, tarfile, tempfile
bundle = pathlib.Path(sys.argv[1]).read_bytes()
payload = bundle.split(b'\n__ALGOSEC_DOCKER_PAYLOAD__\n', 1)[1]
if hashlib.sha256(payload).hexdigest() != '__PAYLOAD_DIGEST__':
    raise SystemExit('Docker installer checksum mismatch; nothing extracted.')
args = sys.argv[2:]
if args == ['--help']:
    print('Usage: sudo sh algosec-jira-bus-0.3.14-docker-amd64.run [--guided | --upgrade | --prepare-only] [--data-dir /absolute/path] [--config-file /root/bus.json --secrets-file /root/secrets.json [--ca-file /root/ca.pem]]\n       sh algosec-jira-bus-0.3.14-docker-amd64.run --extract NEW_DIRECTORY\n--guided opens one Linux dialog workflow for Forge, Jira and the Docker bus.\nContains the ready linux/amd64 image; the target host does not build software.')
    raise SystemExit(0)
extract_only = bool(args and args[0] == '--extract')
if extract_only and len(args) != 2:
    raise SystemExit('Usage: --extract NEW_DIRECTORY')
if extract_only:
    destination = pathlib.Path(args[1]).absolute()
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
else:
    destination = pathlib.Path(tempfile.mkdtemp(prefix='algosec-docker-install-'))
try:
    with tarfile.open(fileobj=io.BytesIO(payload), mode='r:gz') as archive:
        seen = set()
        for member in archive:
            name = pathlib.PurePosixPath(member.name)
            if name.is_absolute() or '..' in name.parts or not member.isfile() or member.name in seen:
                raise ValueError('Unsafe archive member: ' + member.name)
            seen.add(member.name)
            target = destination.joinpath(*name.parts)
            with archive.extractfile(member) as source, target.open('xb') as output:
                shutil.copyfileobj(source, output)
            target.chmod(0o600)
    manifest = json.loads((destination / 'MANIFEST.json').read_text())
    actual = {p.name for p in destination.iterdir() if p.is_file()}
    if actual != set(manifest['files']) | {'MANIFEST.json'}:
        raise ValueError('Manifest file inventory mismatch')
    for name, expected in manifest['files'].items():
        if hashlib.sha256((destination / name).read_bytes()).hexdigest() != expected:
            raise ValueError('Manifest checksum mismatch: ' + name)
    if extract_only:
        print('Verified Docker installer extracted to ' + str(destination))
    else:
        image = destination / '__IMAGE_NAME__'
        image_digest = manifest['files']['__IMAGE_NAME__']
        guided = bool(args and args[0] == '--guided')
        interactive = guided or all(option not in args for option in
                          ('--config-file', '--prepare-only', '--upgrade'))
        terminal = os.fdopen(os.dup(3), 'rb', buffering=0) if interactive else None
        try:
            if guided:
                result = subprocess.run(
                    ['sh', str(destination / '__GUIDED_SETUP_NAME__'), *args[1:]],
                    stdin=terminal)
            else:
                result = subprocess.run(['sh', str(destination / 'install-docker.sh'),
                                         '--image-archive', str(image),
                                         '--image-sha256', image_digest, *args], stdin=terminal)
        finally:
            if terminal is not None:
                terminal.close()
        raise SystemExit(result.returncode)
finally:
    if not extract_only:
        shutil.rmtree(destination)
'''


def installer_header(payload_digest):
    if not re.fullmatch(r'[0-9a-f]{64}', payload_digest):
        raise ValueError('payload digest must be lowercase SHA-256')
    extractor = EXTRACTOR.replace('__PAYLOAD_DIGEST__', payload_digest)
    extractor = extractor.replace('__IMAGE_NAME__', IMAGE_NAME)
    extractor = extractor.replace('__GUIDED_SETUP_NAME__', GUIDED_SETUP_NAME)
    return (
        '#!/bin/sh\nset -eu\n' + HOST_SETUP +
        'command -v python3 >/dev/null 2>&1 || '
        '{ echo "Python 3 required to unpack installer" >&2; exit 1; }\n'
        'exec python3 - "$0" "$@" 3<&0 <<\'ALGOSEC_PYTHON\'\n' + extractor +
        '\nALGOSEC_PYTHON\n').encode()


def _forge_archive():
    inventory = subprocess.run(
        ['git', '-C', str(ROOT), 'ls-files', '-z', '--', 'forge'], check=True,
        stdout=subprocess.PIPE).stdout.split(b'\0')
    paths = [ROOT / name.decode('utf-8') for name in inventory if name]
    if not paths:
        raise ValueError('Forge source is missing')
    stream = io.BytesIO()
    with gzip.GzipFile(fileobj=stream, mode='wb', filename='', mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode='w') as archive:
            for path in paths:
                if path.is_symlink():
                    raise ValueError('Forge source cannot contain symbolic links')
                data = path.read_bytes()
                relative = path.relative_to(ROOT).as_posix()
                member = tarfile.TarInfo(relative)
                member.size = len(data)
                member.mode = 0o600
                member.uid = member.gid = 0
                member.uname = member.gname = ''
                member.mtime = 0
                archive.addfile(member, io.BytesIO(data))
    return stream.getvalue()


def build(image: Path, helper: Path, output: Path, revision=None):
    image = image.resolve(strict=True)
    helper = helper.resolve(strict=True)
    files = {
        IMAGE_NAME: image.read_bytes(),
        'install-docker.sh': helper.read_bytes(),
        STAGER_NAME: (ROOT / 'packaging/docker' / STAGER_NAME).read_bytes(),
        UPGRADER_NAME: (ROOT / 'packaging/docker' / UPGRADER_NAME).read_bytes(),
        BUS_CONF_NAME: (ROOT / 'packaging/docker' / BUS_CONF_NAME).read_bytes(),
        BUS_UPDATE_NAME: (ROOT / 'packaging/docker' / BUS_UPDATE_NAME).read_bytes(),
        GUIDED_SETUP_NAME: (ROOT / 'scripts' / GUIDED_SETUP_NAME).read_bytes(),
        FORGE_SETUP_NAME: (ROOT / 'scripts' / FORGE_SETUP_NAME).read_bytes(),
        FORGE_ARCHIVE_NAME: _forge_archive(),
    }
    for name in PREPARE_NAMES:
        files[name] = (ROOT / 'scripts' / name).read_bytes()
    image_digest = hashlib.sha256(files[IMAGE_NAME]).hexdigest()
    files[IMAGE_NAME + '.sha256'] = (image_digest + '  ' + IMAGE_NAME + '\n').encode()
    if revision is None:
        revision = subprocess.run(
            ['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True).stdout.strip()
    if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('revision must be a full Git commit SHA')
    manifest = {
        'format': 1,
        'platform': 'linux/amd64',
        'revision': revision,
        'files': {name: hashlib.sha256(data).hexdigest()
                  for name, data in sorted(files.items())},
    }
    files['MANIFEST.json'] = (json.dumps(manifest, indent=2) + '\n').encode()
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as archive:
        for name, data in sorted(files.items()):
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mode = 0o600
            archive.addfile(member, io.BytesIO(data))
    payload = stream.getvalue()
    header = installer_header(hashlib.sha256(payload).hexdigest())
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(header + MARKER + payload)
    output.chmod(0o700)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + '.sha256').write_text(
        digest + '  ' + output.name + '\n')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', type=Path, required=True)
    parser.add_argument('--helper', type=Path,
                        default=ROOT / 'packaging/docker/install-docker.sh')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    manifest = build(args.image, args.helper, args.output)
    print(f'{args.output}: ready {manifest["platform"]} image embedded and verified')


if __name__ == '__main__':
    main()

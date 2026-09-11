#!/bin/sh
# Install a prebuilt image without building software or modifying the host Python.
set -eu
umask 077
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ARCHIVE="$HERE/algosec-jira-bus-docker-amd64.tar.gz"
IMAGE_SHA256=""
DATA=/opt/algosec-jira-docker
IMAGE=algosec-jira-bus:0.3.6
CONFIG_FILE=""
SECRETS_FILE=""
CA_FILE=""
PREPARE_ONLY=0
UPGRADE_ONLY=0
BUS_CONF_SOURCE="$HERE/bus_conf"
BUS_UPDATE_SOURCE="$HERE/bus_update"
CONFIG_UPGRADER="$HERE/upgrade-config.py"
FIREFLOW_PREPARE_SOURCE="$HERE/prepare-fireflow.sh"
JIRA_PREPARE_SOURCE="$HERE/prepare-jira.sh"
JIRA_SPACE_SOURCE="$HERE/create-jira-space.sh"
if [ ! -f "$FIREFLOW_PREPARE_SOURCE" ] && [ -f "$HERE/../../scripts/prepare-fireflow.sh" ]; then
    FIREFLOW_PREPARE_SOURCE="$HERE/../../scripts/prepare-fireflow.sh"
    JIRA_PREPARE_SOURCE="$HERE/../../scripts/prepare-jira.sh"
    JIRA_SPACE_SOURCE="$HERE/../../scripts/create-jira-space.sh"
fi
while [ "$#" -gt 0 ]; do
    case "$1" in
        --bundle) [ "$#" -ge 2 ] || exit 2; shift 2;;
        --image-archive) [ "$#" -ge 2 ] || exit 2; ARCHIVE=$2; shift 2;;
        --image-sha256) [ "$#" -ge 2 ] || exit 2; IMAGE_SHA256=$2; shift 2;;
        --data-dir) [ "$#" -ge 2 ] || exit 2; DATA=$2; shift 2;;
        --config-file) [ "$#" -ge 2 ] || exit 2; CONFIG_FILE=$2; shift 2;;
        --secrets-file) [ "$#" -ge 2 ] || exit 2; SECRETS_FILE=$2; shift 2;;
        --ca-file) [ "$#" -ge 2 ] || exit 2; CA_FILE=$2; shift 2;;
        --prepare-only) PREPARE_ONLY=1; shift;;
        --upgrade) UPGRADE_ONLY=1; shift;;
        --help) echo 'Usage: sh install-docker.sh --image-archive FILE --image-sha256 HEX [--upgrade | --prepare-only] [--data-dir /absolute/path] [--config-file /root/bus.json --secrets-file /root/secrets.json [--ca-file /root/ca.pem]]'; exit 0;;
        *) echo "Unknown option: $1" >&2; exit 2;;
    esac
done
[ "$(uname -s)" = Linux ] || { echo 'Linux host required.' >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || { echo 'Run with sudo to prepare private persistent directories.' >&2; exit 1; }
case "$DATA" in /*) ;; *) echo 'Data directory must be absolute.' >&2; exit 1;; esac
case "$DATA" in *:*|*,*) echo 'Data directory cannot contain colon or comma.' >&2; exit 1;; esac
[ -f "$ARCHIVE" ] || { echo "Image archive missing: $ARCHIVE" >&2; exit 1; }
[ -f "$BUS_CONF_SOURCE" ] && [ ! -L "$BUS_CONF_SOURCE" ] \
    || { echo 'Verified bus_conf helper is missing.' >&2; exit 1; }
for HELPER in "$BUS_UPDATE_SOURCE" "$CONFIG_UPGRADER"; do
    [ -f "$HELPER" ] && [ ! -L "$HELPER" ] \
        || { echo "Verified upgrade helper is missing: $HELPER" >&2; exit 1; }
done
for HELPER in "$FIREFLOW_PREPARE_SOURCE" "$JIRA_PREPARE_SOURCE" "$JIRA_SPACE_SOURCE"; do
    [ -f "$HELPER" ] && [ ! -L "$HELPER" ] \
        || { echo "Verified preparation helper is missing: $HELPER" >&2; exit 1; }
done
command -v python3 >/dev/null 2>&1 || { echo 'Python 3 is required to verify and stage the image archive.' >&2; exit 1; }
if { [ -n "$CONFIG_FILE" ] && [ -z "$SECRETS_FILE" ]; } || \
   { [ -z "$CONFIG_FILE" ] && [ -n "$SECRETS_FILE" ]; }; then
    echo 'Use --config-file and --secrets-file together.' >&2
    exit 1
fi
[ "$UPGRADE_ONLY" -eq 0 ] || { [ "$PREPARE_ONLY" -eq 0 ] && [ -z "$CONFIG_FILE" ]; } \
    || { echo '--upgrade cannot be combined with configuration or preparation options.' >&2; exit 2; }
[ -z "$CA_FILE" ] || [ -n "$CONFIG_FILE" ] \
    || { echo '--ca-file requires --config-file and --secrets-file.' >&2; exit 1; }
if [ -n "$CONFIG_FILE" ]; then
    case "$CONFIG_FILE:$SECRETS_FILE${CA_FILE:+:$CA_FILE}" in
        /*:/*|/*:/*:/*) ;;
        *) echo 'Non-interactive input paths must be absolute.' >&2; exit 1;;
    esac
    STAGER="$HERE/stage-config.py"
    [ -f "$STAGER" ] && [ ! -L "$STAGER" ] \
        || { echo 'Verified configuration stager is missing.' >&2; exit 1; }
    if [ -n "$CA_FILE" ]; then
        python3 "$STAGER" --config "$CONFIG_FILE" --secrets "$SECRETS_FILE" --ca "$CA_FILE"
    else
        python3 "$STAGER" --config "$CONFIG_FILE" --secrets "$SECRETS_FILE"
    fi
fi

# This helper runs as root, so a typo such as --data-dir / must never turn into
# chmod/chown of an operating-system directory.  Existing custom locations are accepted
# only after this installer has marked them; the historic default remains upgradeable.
validate_data_dir() {
    python3 - "$1" "$2" <<'PY'
import os, pathlib, stat, sys

raw, action = sys.argv[1:]
default = '/opt/algosec-jira-docker'
marker_name = '.algosec-jira-bus-managed'
marker_value = b'algosec-jira-bus-docker-data-v1\n'
blocked = {
    '/', '/bin', '/boot', '/dev', '/etc', '/home', '/lib', '/lib64', '/media',
    '/mnt', '/opt', '/proc', '/root', '/run', '/sbin', '/srv', '/sys', '/tmp',
    '/usr', '/var',
}
if action not in {'validate', 'initialize'}:
    raise SystemExit('Invalid data-directory operation')
if (not raw.startswith('/') or any(ord(character) < 32 for character in raw)
        or ':' in raw or ',' in raw):
    raise SystemExit('Data directory must be a safe absolute path')
parts = pathlib.PurePosixPath(raw).parts
if '.' in parts or '..' in parts:
    raise SystemExit('Data directory cannot contain dot path components')
path = pathlib.Path(os.path.normpath(raw))
canonical = str(path)
if canonical in blocked or len(path.parts) < 3:
    raise SystemExit('Refusing unsafe data directory: ' + canonical)
if os.path.realpath(canonical) != canonical:
    raise SystemExit('Data directory and its parents must not use symbolic links')

def directory_info(candidate, *, uid, gid=None):
    info = os.lstat(candidate)
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != uid or (gid is not None and info.st_gid != gid)
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise SystemExit('Unsafe existing data directory component: ' + str(candidate))
    return info

def valid_marker(candidate):
    flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
    try:
        descriptor = os.open(candidate, flags)
    except FileNotFoundError:
        return False
    except OSError:
        raise SystemExit('Invalid installer data-directory marker') from None
    try:
        info = os.fstat(descriptor)
        content = os.read(descriptor, len(marker_value) + 1)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600 or content != marker_value):
            raise SystemExit('Invalid installer data-directory marker')
    finally:
        os.close(descriptor)
    return True

exists = path.exists()
if exists:
    directory_info(path, uid=0)
    marked = valid_marker(path / marker_name)
    if not marked and canonical != default:
        raise SystemExit('Existing custom data directory is not managed by this installer')
    for name in ('config', 'state'):
        child = path / name
        if child.exists() or child.is_symlink():
            directory_info(child, uid=10001, gid=10001)
else:
    parent = path.parent
    try:
        info = os.lstat(parent)
    except FileNotFoundError:
        raise SystemExit('Data-directory parent must already exist: ' + str(parent)) from None
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != 0 or info.st_mode & 0o022):
        raise SystemExit('Data-directory parent must be a root-owned, non-writable directory')

if action == 'initialize':
    if not exists:
        os.mkdir(path, 0o700)
        directory_info(path, uid=0)
    for name in ('config', 'state'):
        child = path / name
        if not child.exists():
            os.mkdir(child, 0o700)
            os.chown(child, 10001, 10001)
        directory_info(child, uid=10001, gid=10001)
    marker = path / marker_name
    if not valid_marker(marker):
        flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                 getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0))
        descriptor = os.open(marker, flags, 0o600)
        try:
            os.write(descriptor, marker_value)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        valid_marker(marker)
    directory = os.open(path, os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)

print(canonical)
PY
}
install_helpers() {
    python3 - "$BUS_CONF_SOURCE" "$BUS_UPDATE_SOURCE" "$FIREFLOW_PREPARE_SOURCE" \
        "$JIRA_PREPARE_SOURCE" "$JIRA_SPACE_SOURCE" "$DATA" <<'PY'
import os, pathlib, stat, sys, tempfile

sources = list(map(pathlib.Path, sys.argv[1:6]))
data = pathlib.Path(sys.argv[6])

def atomic_write(destination, payload, mode):
    destination = pathlib.Path(destination)
    descriptor, temporary = tempfile.mkstemp(dir=destination.parent,
                                              prefix='.' + destination.name + '.')
    try:
        with os.fdopen(descriptor, 'wb') as output:
            os.fchmod(output.fileno(), mode)
            os.fchown(output.fileno(), 0, 0)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        parent = os.open(destination.parent, os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0))
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass

for source, destination in zip(
        sources, ('/usr/local/sbin/bus_conf', '/usr/local/sbin/bus_update',
                  '/usr/local/sbin/prepare-fireflow.sh',
                  '/usr/local/sbin/prepare-jira.sh',
                  '/usr/local/sbin/create-jira-space.sh')):
    source_fd = os.open(source, os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) |
                        getattr(os, 'O_NOFOLLOW', 0))
    try:
        details = os.fstat(source_fd)
        if (not stat.S_ISREG(details.st_mode) or details.st_nlink != 1
                or details.st_size > 1024 * 1024):
            raise SystemExit('Invalid verified installation helper')
        content = os.read(source_fd, details.st_size + 1)
    finally:
        os.close(source_fd)
    if len(content) != details.st_size:
        raise SystemExit('Could not read verified installation helper')
    atomic_write(destination, content, 0o755)
atomic_write('/etc/algosec-jira-bus-docker.path',
             (str(data) + '\n').encode('utf-8'), 0o600)
PY
}
if [ -z "$IMAGE_SHA256" ]; then
    CHECKSUM_FILE=$ARCHIVE.sha256
    [ -f "$CHECKSUM_FILE" ] && [ ! -L "$CHECKSUM_FILE" ] \
        || { echo "Image checksum missing: $CHECKSUM_FILE" >&2; exit 1; }
    IMAGE_SHA256=$(awk 'NR == 1 {print $1; exit}' "$CHECKSUM_FILE")
fi

# Docker will execute image content with access to mounted secrets. Copy through an open
# no-follow descriptor and verify the independently supplied digest before docker load.
STAGE_DIR=$(mktemp -d /tmp/algosec-jira-docker.XXXXXX)
PENDING_CONFIG=""
DOCTOR_STATE=""
cleanup() {
    rm -rf "$STAGE_DIR"
    [ -z "$PENDING_CONFIG" ] || rm -rf "$PENDING_CONFIG"
    [ -z "$DOCTOR_STATE" ] || rm -rf "$DOCTOR_STATE"
}
trap cleanup 0 HUP INT TERM
STAGED_ARCHIVE=$STAGE_DIR/image.tar.gz
python3 - "$ARCHIVE" "$IMAGE_SHA256" "$STAGED_ARCHIVE" <<'PY'
import hashlib, os, pathlib, stat, sys
source, expected, target = pathlib.Path(sys.argv[1]), sys.argv[2].lower(), pathlib.Path(sys.argv[3])
if len(expected) != 64 or any(c not in '0123456789abcdef' for c in expected):
    raise SystemExit('Image SHA256 must be exactly 64 hexadecimal characters')
fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
digest = hashlib.sha256()
try:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size > 10 * 1024 * 1024 * 1024:
        raise SystemExit('Image archive must be a regular file smaller than 10 GiB')
    with os.fdopen(fd, 'rb') as src, target.open('xb') as dst:
        fd = None
        for block in iter(lambda: src.read(1024 * 1024), b''):
            digest.update(block)
            dst.write(block)
finally:
    if fd is not None:
        os.close(fd)
if digest.hexdigest() != expected:
    target.unlink(missing_ok=True)
    raise SystemExit('Image SHA256 mismatch; installation stopped')
target.chmod(0o600)
PY
DATA=$(validate_data_dir "$DATA" validate)

# Refuse an incompatible preserved configuration before loading the new image or stopping
# an existing container. The self-contained image accepts only secrets from its private
# secrets.json file.
python3 - "$DATA/config/bus.json" <<'PY'
import json, os, pathlib, stat, sys
import re

path = pathlib.Path(sys.argv[1])
try:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) |
                          getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
except FileNotFoundError:
    raise SystemExit(0)
except OSError:
    raise SystemExit('Existing configuration cannot be inspected safely; installation stopped') from None
try:
    info = os.fstat(descriptor)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 10001 or info.st_nlink != 1
            or info.st_mode & 0o077 or info.st_size > 1024 * 1024):
        raise SystemExit('Existing configuration must be an owner-only regular file; installation stopped')
    chunks = []
    remaining = 1024 * 1024 + 1
    while remaining:
        chunk = os.read(descriptor, min(65536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    raw = b''.join(chunks)
finally:
    os.close(descriptor)
if len(raw) > 1024 * 1024:
    raise SystemExit('Existing configuration is too large; installation stopped')
try:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result
    def reject_constant(_value):
        raise ValueError('non-JSON number')
    settings = json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                          parse_constant=reject_constant)
except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
    raise SystemExit('Existing configuration is invalid JSON; installation stopped') from None
if not isinstance(settings, dict):
    raise SystemExit('Existing configuration root must be a JSON object; installation stopped')
refs = []
jira, fireflow = settings.get('jira'), settings.get('fireflow')
if isinstance(jira, dict) and 'token_ref' in jira:
    refs.append(jira.get('token_ref'))
if isinstance(fireflow, dict):
    refs.extend(fireflow.get(name) for name in ('password_ref', 'session_ref') if name in fireflow)
if any(not isinstance(value, str) or
       not re.fullmatch(r'env:[A-Za-z_][A-Za-z0-9_]{0,127}', value) for value in refs):
    raise SystemExit('Existing configuration uses unsupported secret references. Move the secrets '
                     'to config/secrets.json, change the references to env:NAME, and rerun; '
                     'the existing container was not stopped.')
PY

docker info >/dev/null
ARCH=$(docker info --format '{{.Architecture}}')
case "$ARCH" in x86_64|amd64) ;; *) echo "This image requires an amd64 Docker host; found $ARCH." >&2; exit 1;; esac
# Loading and validating the new image succeeds before an existing instance is stopped.
docker load -i "$STAGED_ARCHIVE"
docker image inspect "$IMAGE" >/dev/null
IMAGE_LABEL=$(docker image inspect --format '{{index .Config.Labels "org.algosec.jira-bus.image"}}' "$IMAGE")
[ "$IMAGE_LABEL" = 0.3.6 ] || { echo 'Loaded archive is not the expected Jira FireFlow bus image.' >&2; exit 1; }
IMAGE_PLATFORM=$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$IMAGE")
[ "$IMAGE_PLATFORM" = linux/amd64 ] || { echo "Loaded image has unexpected platform: $IMAGE_PLATFORM" >&2; exit 1; }
DATA=$(validate_data_dir "$DATA" initialize)
if [ "$PREPARE_ONLY" -eq 1 ]; then
    install_helpers
    echo 'Preparation commands installed: prepare-fireflow.sh, create-jira-space.sh and prepare-jira.sh'
    echo 'After preparing both systems, rerun the installer without --prepare-only.'
    exit 0
fi
MOUNT_RO=ro
MOUNT_RW=rw
if command -v getenforce >/dev/null 2>&1 && [ "$(getenforce)" = Enforcing ]; then
    MOUNT_RO=ro,Z
    MOUNT_RW=rw,Z
fi
if [ "$UPGRADE_ONLY" -eq 1 ]; then
    docker container inspect algosec-jira-bus >/dev/null 2>&1 \
        || { echo 'No installed algosec-jira-bus container to upgrade.' >&2; exit 1; }
    LABEL=$(docker inspect -f '{{index .Config.Labels "org.algosec.jira-bus.managed"}}' algosec-jira-bus)
    [ "$LABEL" = helper ] \
        || { echo 'Existing container is not managed by this installer.' >&2; exit 1; }
    [ "$(docker inspect -f '{{.State.Running}}' algosec-jira-bus)" = true ] \
        || { echo 'Existing container is not running; inspect it before upgrading.' >&2; exit 1; }
    docker container inspect algosec-jira-bus-previous >/dev/null 2>&1 \
        && { echo 'A previous rollback container already exists; inspect it before upgrading.' >&2; exit 1; }
    [ -f "$DATA/config/secrets.json" ] && [ ! -L "$DATA/config/secrets.json" ] \
        || { echo 'Existing secrets.json is missing or unsafe; current container was not touched.' >&2; exit 1; }
    MIGRATED_CONFIG=$STAGE_DIR/bus.json
    DOCTOR_STATE=$(mktemp -d "$DATA/.doctor-state.XXXXXX")
    chown 10001:10001 "$DOCTOR_STATE"
    chmod 0700 "$DOCTOR_STATE"
    python3 "$CONFIG_UPGRADER" stage "$DATA/config/bus.json" "$MIGRATED_CONFIG"
    echo 'Checking the new image with migrated config and the existing read-only secrets.'
    docker run --rm --user 10001:10001 --read-only --tmpfs /tmp:rw,nosuid,nodev,size=64m \
        --cap-drop ALL --security-opt no-new-privileges --pids-limit 128 \
        --memory 512m --memory-swap 512m \
        -v "$DATA/config:/etc/algosec-jira-bus:$MOUNT_RO" \
        -v "$MIGRATED_CONFIG:/etc/algosec-jira-bus/bus.json:$MOUNT_RO" \
        -v "$DOCTOR_STATE:/var/lib/algosec-jira-bus:$MOUNT_RW" \
        "$IMAGE" doctor
    BACKUP=$DATA/.bus-json.upgrade-backup
    if ! docker stop algosec-jira-bus >/dev/null; then
        echo 'Could not stop the current container; upgrade cancelled.' >&2
        exit 1
    fi
    if ! docker rename algosec-jira-bus algosec-jira-bus-previous; then
        docker start algosec-jira-bus >/dev/null 2>&1 || true
        echo 'Could not reserve the rollback container; previous service restarted.' >&2
        exit 1
    fi
    if ! python3 "$CONFIG_UPGRADER" apply "$DATA/config/bus.json" "$MIGRATED_CONFIG" "$BACKUP"; then
        docker rename algosec-jira-bus-previous algosec-jira-bus >/dev/null 2>&1 || true
        docker start algosec-jira-bus >/dev/null 2>&1 || true
        echo 'Could not install migrated configuration; previous service restored.' >&2
        exit 1
    fi
    READY=0
    STARTED=$(date +%s)
    if docker run -d --name algosec-jira-bus --label org.algosec.jira-bus.managed=helper \
        --restart unless-stopped --user 10001:10001 --read-only \
        --tmpfs /tmp:rw,nosuid,nodev,size=64m --cap-drop ALL --security-opt no-new-privileges \
        --pids-limit 128 --memory 512m --memory-swap 512m \
        --log-opt max-size=10m --log-opt max-file=3 \
        -v "$DATA/config:/etc/algosec-jira-bus:$MOUNT_RO" \
        -v "$DATA/state:/var/lib/algosec-jira-bus:$MOUNT_RW" "$IMAGE" >/dev/null; then
        ATTEMPT=0
        while [ "$ATTEMPT" -lt 90 ]; do
            [ "$(docker inspect -f '{{.State.Running}}' algosec-jira-bus 2>/dev/null || true)" = true ] \
                || break
            if docker logs --since "$STARTED" algosec-jira-bus 2>&1 \
                | grep -q '^READY: startup doctor passed\.$'; then
                READY=1
                break
            fi
            sleep 2
            ATTEMPT=$((ATTEMPT + 2))
        done
    fi
    if [ "$READY" -eq 1 ] && install_helpers; then
        python3 "$CONFIG_UPGRADER" discard "$BACKUP"
        docker rm algosec-jira-bus-previous >/dev/null 2>&1 || true
        echo 'Upgrade completed. Existing secrets and state were preserved.'
        echo 'Check: docker logs --tail 100 algosec-jira-bus'
        exit 0
    fi
    echo 'New container failed readiness; restoring the previous container and config.' >&2
    docker container inspect algosec-jira-bus >/dev/null 2>&1 \
        && docker rm -f algosec-jira-bus >/dev/null 2>&1 || true
    python3 "$CONFIG_UPGRADER" restore "$DATA/config/bus.json" "$BACKUP" || true
    docker rename algosec-jira-bus-previous algosec-jira-bus >/dev/null 2>&1 || true
    docker start algosec-jira-bus >/dev/null 2>&1 || true
    exit 1
fi
if [ -n "$CONFIG_FILE" ]; then
    PENDING_CONFIG=$(mktemp -d "$DATA/.config.pending.XXXXXX")
    DOCTOR_STATE=$(mktemp -d "$DATA/.doctor-state.XXXXXX")
    chown 10001:10001 "$PENDING_CONFIG" "$DOCTOR_STATE"
    chmod 0700 "$PENDING_CONFIG" "$DOCTOR_STATE"
    if [ -n "$CA_FILE" ]; then
        python3 "$STAGER" --config "$CONFIG_FILE" --secrets "$SECRETS_FILE" --ca "$CA_FILE" \
            --target "$PENDING_CONFIG"
    else
        python3 "$STAGER" --config "$CONFIG_FILE" --secrets "$SECRETS_FILE" \
            --target "$PENDING_CONFIG"
    fi
    echo 'Checking Jira and FireFlow APIs from the installed image.'
    docker run --rm --user 10001:10001 --read-only --tmpfs /tmp:rw,nosuid,nodev,size=64m \
        --cap-drop ALL --security-opt no-new-privileges --pids-limit 128 \
        --memory 512m --memory-swap 512m \
        -v "$PENDING_CONFIG:/etc/algosec-jira-bus:$MOUNT_RO" \
        -v "$DOCTOR_STATE:/var/lib/algosec-jira-bus:$MOUNT_RW" \
        "$IMAGE" doctor

    python3 - "$DATA/.config.previous" <<'PY'
import pathlib, sys
path = pathlib.Path(sys.argv[1])
if path.exists() or path.is_symlink():
    raise SystemExit('A previous configuration backup exists; inspect it before upgrading')
PY
    HAD_CONTAINER=0
    if docker container inspect algosec-jira-bus >/dev/null 2>&1; then
        LABEL=$(docker inspect -f '{{index .Config.Labels "org.algosec.jira-bus.managed"}}' algosec-jira-bus)
        [ "$LABEL" = helper ] || { echo 'Existing container is not managed by this helper; refusing to replace it.' >&2; exit 1; }
        docker container inspect algosec-jira-bus-previous >/dev/null 2>&1 \
            && { echo 'A previous rollback container already exists; inspect it before upgrading.' >&2; exit 1; }
        HAD_CONTAINER=1
        docker stop algosec-jira-bus >/dev/null
        docker rename algosec-jira-bus algosec-jira-bus-previous
    fi
    if ! python3 - "$DATA" "$PENDING_CONFIG" <<'PY'
import os, pathlib, sys
data, pending = map(pathlib.Path, sys.argv[1:])
current, backup = data / 'config', data / '.config.previous'
os.rename(current, backup)
try:
    os.rename(pending, current)
except BaseException:
    os.rename(backup, current)
    raise
descriptor = os.open(data, os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0))
try:
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
    then
        if [ "$HAD_CONTAINER" -eq 1 ]; then
            docker rename algosec-jira-bus-previous algosec-jira-bus
            docker start algosec-jira-bus >/dev/null
        fi
        exit 1
    fi
    PENDING_CONFIG=""
    READY=0
    if docker run -d --name algosec-jira-bus --label org.algosec.jira-bus.managed=helper \
        --restart unless-stopped --user 10001:10001 --read-only \
        --tmpfs /tmp:rw,nosuid,nodev,size=64m --cap-drop ALL --security-opt no-new-privileges \
        --pids-limit 128 --memory 512m --memory-swap 512m \
        --log-opt max-size=10m --log-opt max-file=3 \
        -v "$DATA/config:/etc/algosec-jira-bus:$MOUNT_RO" \
        -v "$DATA/state:/var/lib/algosec-jira-bus:$MOUNT_RW" "$IMAGE" >/dev/null; then
        ATTEMPT=0
        while [ "$ATTEMPT" -lt 90 ]; do
            if [ "$(docker inspect -f '{{.State.Running}}' algosec-jira-bus 2>/dev/null || true)" != true ]; then
                break
            fi
            if docker logs algosec-jira-bus 2>&1 | grep -q '^READY: startup doctor passed\.$'; then
                READY=1
                break
            fi
            sleep 2
            ATTEMPT=$((ATTEMPT + 2))
        done
    fi
    if [ "$READY" -eq 1 ]; then
        install_helpers
        [ "$HAD_CONTAINER" -eq 0 ] || docker rm algosec-jira-bus-previous >/dev/null
        python3 - "$DATA/.config.previous" "$DOCTOR_STATE" <<'PY'
import pathlib, shutil, sys
for raw in sys.argv[1:]:
    path = pathlib.Path(raw)
    if path.exists() and not path.is_symlink():
        shutil.rmtree(path)
PY
        echo 'Container started. Check: docker logs --tail 100 algosec-jira-bus'
        echo "Persistent configuration and state: $DATA"
        exit 0
    fi
    echo 'New container failed to start; restoring the previous deployment.' >&2
    docker container inspect algosec-jira-bus >/dev/null 2>&1 && docker rm -f algosec-jira-bus >/dev/null
    python3 - "$DATA" <<'PY'
import os, pathlib, shutil, sys
data = pathlib.Path(sys.argv[1]); current, backup = data / 'config', data / '.config.previous'
if current.exists() and not current.is_symlink():
    shutil.rmtree(current)
os.rename(backup, current)
descriptor = os.open(data, os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0))
try:
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
    if [ "$HAD_CONTAINER" -eq 1 ]; then
        docker rename algosec-jira-bus-previous algosec-jira-bus
        docker start algosec-jira-bus >/dev/null
    fi
    exit 1
else
    if docker container inspect algosec-jira-bus >/dev/null 2>&1; then
        LABEL=$(docker inspect -f '{{index .Config.Labels "org.algosec.jira-bus.managed"}}' algosec-jira-bus)
        [ "$LABEL" = helper ] || { echo 'Existing container is not managed by this helper; refusing to replace it.' >&2; exit 1; }
        docker stop algosec-jira-bus >/dev/null
    fi
    echo 'Enter Jira and ASMS settings in the wizard. Existing saved configuration is preserved.'
    docker run --rm -it --user 10001:10001 --read-only --tmpfs /tmp:rw,nosuid,nodev,size=64m \
        --cap-drop ALL --security-opt no-new-privileges --pids-limit 128 \
        --memory 512m --memory-swap 512m \
        -v "$DATA/config:/etc/algosec-jira-bus:$MOUNT_RW" \
        -v "$DATA/state:/var/lib/algosec-jira-bus:$MOUNT_RW" \
        --entrypoint python "$IMAGE" /opt/algosec-jira-bus/setup-source/scripts/setup_wizard.py \
        --source /opt/algosec-jira-bus/setup-source --container
fi
# A failed wizard exits above and leaves an existing instance stopped for inspection.
if docker container inspect algosec-jira-bus >/dev/null 2>&1; then docker rm algosec-jira-bus >/dev/null; fi
docker run -d --name algosec-jira-bus --label org.algosec.jira-bus.managed=helper \
    --restart unless-stopped --user 10001:10001 --read-only \
    --tmpfs /tmp:rw,nosuid,nodev,size=64m --cap-drop ALL --security-opt no-new-privileges \
    --pids-limit 128 --memory 512m --memory-swap 512m \
    --log-opt max-size=10m --log-opt max-file=3 \
    -v "$DATA/config:/etc/algosec-jira-bus:$MOUNT_RO" \
    -v "$DATA/state:/var/lib/algosec-jira-bus:$MOUNT_RW" "$IMAGE"
install_helpers
echo 'Container started. Check: docker logs --tail 100 algosec-jira-bus'
echo 'Reconfigure later: sudo bus_conf'
echo 'Prepare remote systems: prepare-fireflow.sh, create-jira-space.sh and prepare-jira.sh'
echo "Persistent configuration and state: $DATA"

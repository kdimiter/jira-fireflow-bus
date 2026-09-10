#!/bin/sh
# Install a prebuilt image without building software or modifying the host Python.
set -eu
umask 077
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ARCHIVE="$HERE/algosec-jira-bus-docker-amd64.tar.gz"
IMAGE_SHA256=""
DATA=/opt/algosec-jira-docker
IMAGE=algosec-jira-bus:0.2.0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --bundle) [ "$#" -ge 2 ] || exit 2; shift 2;;
        --image-archive) [ "$#" -ge 2 ] || exit 2; ARCHIVE=$2; shift 2;;
        --image-sha256) [ "$#" -ge 2 ] || exit 2; IMAGE_SHA256=$2; shift 2;;
        --data-dir) [ "$#" -ge 2 ] || exit 2; DATA=$2; shift 2;;
        --help) echo 'Usage: sh install-docker.sh --image-archive FILE --image-sha256 HEX [--data-dir /absolute/path]'; exit 0;;
        *) echo "Unknown option: $1" >&2; exit 2;;
    esac
done
[ "$(uname -s)" = Linux ] || { echo 'Linux host required.' >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || { echo 'Run with sudo to prepare private persistent directories.' >&2; exit 1; }
case "$DATA" in /*) ;; *) echo 'Data directory must be absolute.' >&2; exit 1;; esac
case "$DATA" in *:*|*,*) echo 'Data directory cannot contain colon or comma.' >&2; exit 1;; esac
[ -f "$ARCHIVE" ] || { echo "Image archive missing: $ARCHIVE" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo 'Python 3 is required to verify and stage the image archive.' >&2; exit 1; }

# Docker will execute image content with access to mounted secrets. Copy through an open
# no-follow descriptor and verify the independently supplied digest before docker load.
STAGE_DIR=$(mktemp -d /tmp/algosec-jira-docker.XXXXXX)
trap 'rm -rf "$STAGE_DIR"' 0 HUP INT TERM
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

docker info >/dev/null
ARCH=$(docker info --format '{{.Architecture}}')
case "$ARCH" in x86_64|amd64) ;; *) echo "This image requires an amd64 Docker host; found $ARCH." >&2; exit 1;; esac
# Loading and validating the new image succeeds before an existing instance is stopped.
docker load -i "$STAGED_ARCHIVE"
docker image inspect "$IMAGE" >/dev/null
IMAGE_LABEL=$(docker image inspect --format '{{index .Config.Labels "org.algosec.jira-bus.image"}}' "$IMAGE")
[ "$IMAGE_LABEL" = 0.2.0 ] || { echo 'Loaded archive is not the expected Jira FireFlow bus image.' >&2; exit 1; }
if docker container inspect algosec-jira-bus >/dev/null 2>&1; then
    LABEL=$(docker inspect -f '{{index .Config.Labels "org.algosec.jira-bus.managed"}}' algosec-jira-bus)
    [ "$LABEL" = helper ] || { echo 'Existing container is not managed by this helper; refusing to replace it.' >&2; exit 1; }
    docker stop algosec-jira-bus >/dev/null
fi
install -d -m 0700 "$DATA"
install -d -m 0700 -o 10001 -g 10001 "$DATA/config" "$DATA/state"
echo 'Enter Jira and ASMS settings in the wizard. Existing saved configuration is preserved.'
docker run --rm -it --user 10001:10001 --read-only --tmpfs /tmp:rw,nosuid,nodev,size=64m \
    --cap-drop ALL --security-opt no-new-privileges \
    -v "$DATA/config:/etc/algosec-jira-bus:rw" -v "$DATA/state:/var/lib/algosec-jira-bus:rw" \
    --entrypoint python "$IMAGE" /opt/algosec-jira-bus/setup-source/scripts/setup_wizard.py \
    --source /opt/algosec-jira-bus/setup-source --container
# A failed wizard exits above and leaves an existing instance stopped for inspection.
if docker container inspect algosec-jira-bus >/dev/null 2>&1; then docker rm algosec-jira-bus >/dev/null; fi
docker run -d --name algosec-jira-bus --label org.algosec.jira-bus.managed=helper \
    --restart unless-stopped --user 10001:10001 --read-only \
    --tmpfs /tmp:rw,nosuid,nodev,size=64m --cap-drop ALL --security-opt no-new-privileges \
    --log-opt max-size=10m --log-opt max-file=3 \
    -v "$DATA/config:/etc/algosec-jira-bus:ro" -v "$DATA/state:/var/lib/algosec-jira-bus:rw" "$IMAGE"
echo 'Container started. Check: docker logs --tail 100 algosec-jira-bus'
echo "Persistent configuration and state: $DATA"

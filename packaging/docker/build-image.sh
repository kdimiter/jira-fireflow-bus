#!/bin/sh
# Build only from the verified distributable, never the operator's secrets or Git credentials.
set -eu
[ "$#" -eq 4 ] || { echo "Usage: sh build-image.sh INSTALLER.run CONNECTOR.whl CONNECTOR_SHA256 OUTPUT_IMAGE.tar.gz" >&2; exit 2; }
INSTALLER=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
CONNECTOR=$(cd "$(dirname "$2")" && pwd)/$(basename "$2")
DIGEST=$3
OUTPUT=$(cd "$(dirname "$4")" && pwd)/$(basename "$4")
ROOT=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
TEMP=$(mktemp -d)
trap 'rm -rf "$TEMP"' EXIT HUP INT TERM
sh "$INSTALLER" --extract "$TEMP/source"
case "$(basename "$CONNECTOR")" in algosec_host_mcp-*-py3-none-any.whl) ;; *) echo 'Invalid connector wheel name' >&2; exit 1;; esac
mkdir "$TEMP/source/vendor"
python3 - "$CONNECTOR" "$DIGEST" "$TEMP/source/vendor/$(basename "$CONNECTOR")" <<'PY'
import hashlib, os, pathlib, stat, sys
source, expected, target = pathlib.Path(sys.argv[1]), sys.argv[2].lower(), pathlib.Path(sys.argv[3])
if len(expected) != 64 or any(c not in '0123456789abcdef' for c in expected):
    raise SystemExit('Connector SHA256 must be exactly 64 hexadecimal characters')
fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
digest = hashlib.sha256()
try:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size > 100 * 1024 * 1024:
        raise SystemExit('Connector must be a regular file smaller than 100 MiB')
    with os.fdopen(fd, 'rb') as src, target.open('xb') as dst:
        fd = None
        for block in iter(lambda: src.read(1024 * 1024), b''):
            digest.update(block); dst.write(block)
finally:
    if fd is not None: os.close(fd)
if digest.hexdigest() != expected:
    target.unlink(missing_ok=True)
    raise SystemExit('Connector SHA256 mismatch')
target.chmod(0o600)
PY
# Dockerfile is deliberately outside the source-installer suffix allowlist.
cp "$ROOT/packaging/docker/Dockerfile" "$TEMP/source/Dockerfile"
docker build --platform linux/amd64 --tag algosec-jira-bus:0.2.0 "$TEMP/source"
docker save --output "$TEMP/image.tar" algosec-jira-bus:0.2.0
gzip -c "$TEMP/image.tar" > "$OUTPUT"
python3 - "$OUTPUT" <<'PY'
import hashlib, pathlib, sys
p = pathlib.Path(sys.argv[1]); h = hashlib.sha256()
with p.open('rb') as f:
    for block in iter(lambda: f.read(1024*1024), b''): h.update(block)
p.with_suffix(p.suffix + '.sha256').write_text(h.hexdigest() + '  ' + p.name + '\n')
PY

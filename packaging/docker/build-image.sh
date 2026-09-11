#!/bin/sh
# Build only from the verified distributable, never the operator's secrets or Git credentials.
set -eu
[ "$#" -eq 2 ] || { echo "Usage: sh build-image.sh INSTALLER.run OUTPUT_IMAGE.tar.gz" >&2; exit 2; }
INSTALLER=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
OUTPUT=$(cd "$(dirname "$2")" && pwd)/$(basename "$2")
TEMP=$(mktemp -d)
trap 'rm -rf "$TEMP"' EXIT HUP INT TERM
sh "$INSTALLER" --extract "$TEMP/source"
[ -f "$TEMP/source/packaging/docker/Dockerfile" ] || { echo 'Verified bundle has no Dockerfile.' >&2; exit 1; }
cp "$TEMP/source/packaging/docker/Dockerfile" "$TEMP/source/Dockerfile"
cp "$TEMP/source/packaging/docker/.dockerignore" "$TEMP/source/.dockerignore"
REVISION=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["revision"])' "$TEMP/source/MANIFEST.json")
docker build --pull --platform linux/amd64 --build-arg "VCS_REF=$REVISION" \
    --tag algosec-jira-bus:0.3.5 "$TEMP/source"
docker save --output "$TEMP/image.tar" algosec-jira-bus:0.3.5
gzip -n -c "$TEMP/image.tar" > "$OUTPUT"
python3 - "$OUTPUT" <<'PY'
import hashlib, pathlib, sys
p = pathlib.Path(sys.argv[1]); h = hashlib.sha256()
with p.open('rb') as f:
    for block in iter(lambda: f.read(1024*1024), b''): h.update(block)
p.with_suffix(p.suffix + '.sha256').write_text(h.hexdigest() + '  ' + p.name + '\n')
PY

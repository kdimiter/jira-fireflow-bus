#!/bin/sh
# Public entry point for native Linux or Docker installation.
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec sh "$HERE/scripts/setup.sh" "$@"

#!/bin/sh
# Create the dedicated ASMS/FireFlow account through the installed Docker image.
set -eu
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
unset PYTHONPATH PYTHONHOME DOCKER_HOST DOCKER_CONTEXT DOCKER_CONFIG
CONTAINER=algosec-jira-bus
IMAGE=algosec-jira-bus:0.3.10
if ! docker image inspect "$IMAGE" >/dev/null 2>&1 \
        && docker container inspect "$CONTAINER" >/dev/null 2>&1; then
    IMAGE=$(docker inspect --format '{{.Config.Image}}' "$CONTAINER")
fi
TTY=-i
if [ -t 0 ] && [ -t 1 ]; then TTY=-it; fi
CA_FILE=
EXPECT_CA=0
for ARGUMENT in "$@"; do
    if [ "$EXPECT_CA" -eq 1 ]; then CA_FILE=$ARGUMENT; EXPECT_CA=0
    elif [ "$ARGUMENT" = --ca-file ]; then EXPECT_CA=1
    fi
done
[ "$EXPECT_CA" -eq 0 ] || { echo 'Missing --ca-file value.' >&2; exit 2; }
if [ -n "$CA_FILE" ]; then
    case "$CA_FILE" in /*) ;; *) echo '--ca-file must be absolute.' >&2; exit 2;; esac
    case "$CA_FILE" in *:*|*,*) echo 'Unsafe --ca-file path.' >&2; exit 2;; esac
    [ -f "$CA_FILE" ] && [ ! -L "$CA_FILE" ] \
        || { echo 'CA file must be a regular non-link file.' >&2; exit 2; }
    exec docker run --rm "$TTY" --user 0:0 --read-only \
        --tmpfs /tmp:rw,nosuid,nodev,size=32m --cap-drop ALL \
        --security-opt no-new-privileges --pids-limit 64 \
        --memory 256m --memory-swap 256m -v "$CA_FILE:$CA_FILE:ro" \
        --entrypoint python "$IMAGE" -m algosec_jira_bus.provision "$@"
fi
exec docker run --rm "$TTY" --read-only --tmpfs /tmp:rw,nosuid,nodev,size=32m \
    --cap-drop ALL --security-opt no-new-privileges --pids-limit 64 \
    --memory 256m --memory-swap 256m --entrypoint python "$IMAGE" \
    -m algosec_jira_bus.provision "$@"

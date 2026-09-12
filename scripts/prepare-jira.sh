#!/bin/sh
# Prepare Jira through the installed Docker image; no host Python is used.
set -eu
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
unset PYTHONPATH PYTHONHOME DOCKER_HOST DOCKER_CONTEXT DOCKER_CONFIG
CONTAINER=algosec-jira-bus
IMAGE=algosec-jira-bus:0.3.12
CREDENTIALS_DIR=
if [ "${1:-}" = --credentials-dir ]; then
    [ "$#" -ge 2 ] || { echo 'Missing Jira credentials directory.' >&2; exit 2; }
    CREDENTIALS_DIR=$2
    shift 2
    case "$CREDENTIALS_DIR" in /*) ;; *) echo 'Jira credentials directory must be absolute.' >&2; exit 2;; esac
    [ -d "$CREDENTIALS_DIR" ] && [ ! -L "$CREDENTIALS_DIR" ] \
        || { echo 'Jira credentials directory is unsafe.' >&2; exit 1; }
fi
if ! docker image inspect "$IMAGE" >/dev/null 2>&1 \
        && docker container inspect "$CONTAINER" >/dev/null 2>&1; then
    IMAGE=$(docker inspect --format '{{.Config.Image}}' "$CONTAINER")
fi
TTY=-i
if [ -t 0 ] && [ -t 1 ]; then TTY=-it; fi
if [ -n "$CREDENTIALS_DIR" ]; then
    exec docker run --rm "$TTY" --read-only --tmpfs /tmp:rw,nosuid,nodev,size=32m \
        --cap-drop ALL --security-opt no-new-privileges --pids-limit 64 \
        --memory 256m --memory-swap 256m --user 0:0 \
        --mount "type=bind,src=$CREDENTIALS_DIR,dst=/run/jira-credentials,readonly" \
        --entrypoint python "$IMAGE" -m algosec_jira_bus.jira_provision \
        --credentials-dir /run/jira-credentials "$@"
fi
exec docker run --rm "$TTY" --read-only --tmpfs /tmp:rw,nosuid,nodev,size=32m \
    --cap-drop ALL --security-opt no-new-privileges --pids-limit 64 \
    --memory 256m --memory-swap 256m --entrypoint python "$IMAGE" \
    -m algosec_jira_bus.jira_provision "$@"

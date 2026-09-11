#!/bin/sh
# Prepare Jira through the installed Docker image; no host Python is used.
set -eu
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
unset PYTHONPATH PYTHONHOME DOCKER_HOST DOCKER_CONTEXT DOCKER_CONFIG
CONTAINER=algosec-jira-bus
IMAGE=algosec-jira-bus:0.2.2
if docker container inspect "$CONTAINER" >/dev/null 2>&1; then
    IMAGE=$(docker inspect --format '{{.Config.Image}}' "$CONTAINER")
fi
TTY=-i
if [ -t 0 ] && [ -t 1 ]; then TTY=-it; fi
exec docker run --rm "$TTY" --read-only --tmpfs /tmp:rw,nosuid,nodev,size=32m \
    --cap-drop ALL --security-opt no-new-privileges --pids-limit 64 \
    --memory 256m --memory-swap 256m --entrypoint python "$IMAGE" \
    -m algosec_jira_bus.jira_provision "$@"

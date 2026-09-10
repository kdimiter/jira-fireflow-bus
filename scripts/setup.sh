#!/bin/sh
# Unified Linux entry point; preserves an explicit choice of deployment model.
set -eu
usage() {
    cat <<'HELP'
Usage: sh setup.sh [--mode native|docker] [--bundle PATH.run] [-- helper-options]
Without --mode, asks whether to install a native systemd service or Docker deployment.
Native mode needs algosec-jira-bus-linux.run. Docker mode uses the supplied ready image
archive and install-docker.sh; it never builds software on the target server.
Native mode requires root, Python 3.11+ and a running systemd manager.
Docker mode delegates requirements and configuration to install-docker.sh.
No passwords are accepted by this dispatcher; enter credentials in the setup wizard.
HELP
}
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
MODE=""
BUNDLE=$HERE/algosec-jira-bus-linux.run
while [ "$#" -gt 0 ]; do
    case "$1" in
        --help) usage; exit 0 ;;
        --mode|--bundle)
            [ "$#" -ge 2 ] || { usage >&2; exit 2; }
            case "$1" in --mode) MODE=$2;; --bundle) BUNDLE=$2;; esac
            shift 2 ;;
        --) shift; break ;;
        *) echo 'Unknown setup option; use --help.' >&2; exit 2 ;;
    esac
done
[ "$(uname -s)" = Linux ] || { echo 'This deployment helper requires Linux.' >&2; exit 1; }
if [ -z "$MODE" ]; then
    printf 'Deployment: 1) Native Linux / systemd  2) Docker\nChoose 1 or 2: '
    read -r CHOICE || { echo 'No selection; rerun with --mode native or --mode docker.' >&2; exit 2; }
    case "$CHOICE" in 1) MODE=native;; 2) MODE=docker;; *) echo 'Choose 1 or 2.' >&2; exit 2;; esac
fi
case "$MODE" in native|docker) ;; *) echo 'Mode must be native or docker.' >&2; exit 2;; esac
if [ "$MODE" = native ]; then
    [ -f "$BUNDLE" ] || { echo 'Release bundle missing; supply --bundle PATH.run.' >&2; exit 1; }
    exec sh "$BUNDLE" "$@"
fi
if [ -f "$HERE/../packaging/docker/install-docker.sh" ]; then
    DOCKER_HELPER=$HERE/../packaging/docker/install-docker.sh
elif [ -f "$HERE/install-docker.sh" ]; then
    DOCKER_HELPER=$HERE/install-docker.sh
else
    echo 'Docker helper missing; keep install-docker.sh alongside setup.sh or use the extracted bundle.' >&2
    exit 1
fi
exec sh "$DOCKER_HELPER" "$@"

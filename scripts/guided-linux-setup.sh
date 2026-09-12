#!/bin/sh
# One guided Linux path: host prerequisites, Forge, Jira, then the Docker bus.
set -eu
umask 077

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
IMAGE_ARCHIVE=$HERE/algosec-jira-bus-docker-amd64.tar.gz
IMAGE_CHECKSUM=$IMAGE_ARCHIVE.sha256
FORGE_ARCHIVE=$HERE/forge-app.tar.gz
DATA=/opt/algosec-jira-docker

while [ "$#" -gt 0 ]; do
    case "$1" in
        --data-dir) [ "$#" -ge 2 ] || exit 2; DATA=$2; shift 2;;
        --help)
            echo 'Usage: sudo sh installer.run --guided [--data-dir /absolute/path]'
            exit 0
            ;;
        *) echo "Unknown guided setup option: $1" >&2; exit 2;;
    esac
done

[ "$(uname -s)" = Linux ] || { echo 'Guided setup requires Linux.' >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || { echo 'Run the guided installer with sudo.' >&2; exit 1; }
for FILE in "$IMAGE_ARCHIVE" "$IMAGE_CHECKSUM" "$FORGE_ARCHIVE" \
            "$HERE/install-docker.sh" "$HERE/setup-forge.sh"; do
    [ -f "$FILE" ] && [ ! -L "$FILE" ] || { echo "Verified component missing: $FILE" >&2; exit 1; }
done

. /etc/os-release
case "$ID" in
    ubuntu|debian)
        apt-get update
        apt-get install -y ca-certificates curl git build-essential whiptail xz-utils
        ;;
    rhel|rocky|almalinux)
        dnf -y install ca-certificates curl git gcc gcc-c++ make newt xz
        ;;
    *) echo "Unsupported Linux distribution for guided dialogs: $ID" >&2; exit 1;;
esac
command -v whiptail >/dev/null 2>&1 || { echo 'whiptail installation failed.' >&2; exit 1; }
command -v runuser >/dev/null 2>&1 || { echo 'runuser is required.' >&2; exit 1; }

cancelled() { echo 'Setup cancelled; no synchronization was enabled.' >&2; exit 1; }
message() { whiptail --title "$1" --msgbox "$2" 12 78; }
input() {
    RESULT=$(whiptail --title "$1" --inputbox "$2" 12 78 "${3:-}" 3>&1 1>&2 2>&3) || cancelled
    printf '%s' "$RESULT"
}
password() {
    RESULT=$(whiptail --title "$1" --passwordbox "$2" 12 78 3>&1 1>&2 2>&3) || cancelled
    printf '%s' "$RESULT"
}
choose() {
    RESULT=$(whiptail --title "$1" --menu "$2" 16 82 5 \
        "$3" "$4" "$5" "$6" 3>&1 1>&2 2>&3) || cancelled
    printf '%s' "$RESULT"
}

DEFAULT_USER=${SUDO_USER:-}
case "$DEFAULT_USER" in ''|root) DEFAULT_USER=;; esac
while :; do
    OPERATOR=$(input 'Linux operator' \
        'Normal Linux user that will own Node.js and the private Forge working copy.' \
        "$DEFAULT_USER")
    if [ -n "$OPERATOR" ] && [ "$OPERATOR" != root ] && getent passwd "$OPERATOR" >/dev/null; then
        break
    fi
    message 'Invalid user' 'Enter an existing non-root Linux user.'
done
OP_UID=$(id -u "$OPERATOR")
OP_GID=$(id -g "$OPERATOR")
OP_HOME=$(getent passwd "$OPERATOR" | awk -F: '{print $6}')
[ -d "$OP_HOME" ] && [ ! -L "$OP_HOME" ] || { echo 'Operator home directory is unsafe.' >&2; exit 1; }

while :; do
    RAW_SITE=$(input 'Jira Cloud' 'Jira hostname or HTTPS URL.' 'your-tenant.atlassian.net')
    SITE=${RAW_SITE#https://}
    SITE=${SITE%/}
    case "$SITE" in ''|*://*|*/*|*:*|*[!a-zA-Z0-9.-]*)
        message 'Invalid Jira site' 'Use tenant.atlassian.net or https://tenant.atlassian.net.';;
        *) break;;
    esac
done
JIRA_URL=https://$SITE

while :; do
    FORGE_EMAIL=$(input 'Forge account' \
        'Atlassian developer/admin email. This is separate from the runtime bus account.')
    [ -n "$FORGE_EMAIL" ] && break
    message 'Required value' 'Forge email cannot be empty.'
done
while :; do
    FORGE_TOKEN=$(password 'Forge token' \
        'Enter an Atlassian API token created with Forge scopes. It is used only during this setup.')
    [ -n "$FORGE_TOKEN" ] && break
    message 'Required value' 'Forge token cannot be empty.'
done

SAVED_MANIFEST=$OP_HOME/algosec-jira-forge/manifest.yml
SAVED_ID=
if [ -f "$SAVED_MANIFEST" ] && [ ! -L "$SAVED_MANIFEST" ]; then
    SAVED_ID=$(sed -n 's/^[[:space:]]*id:[[:space:]]*//p' "$SAVED_MANIFEST" | head -1)
fi
PLACEHOLDER=ari:cloud:ecosystem::app/00000000-0000-0000-0000-000000000000
if [ -n "$SAVED_ID" ] && [ "$SAVED_ID" != "$PLACEHOLDER" ]; then
    APP_MODE=existing
    APP_ID=$SAVED_ID
    message 'Existing Forge App' \
        'The saved registered Forge App ID will be reused. A second application will not be created.'
else
    APP_CHOICE=$(choose 'Forge application' \
        'Register a new application, or attach this Linux host to an existing App ID?' \
        register 'Register a new Forge App' existing 'Use an existing Forge App ID')
    APP_MODE=$APP_CHOICE
    APP_ID=
    if [ "$APP_MODE" = existing ]; then
        while :; do
            APP_ID=$(input 'Existing Forge App ID' \
                'Enter ari:cloud:ecosystem::app/ followed by the UUID from Atlassian Developer Console.')
            printf '%s\n' "$APP_ID" | grep -Eq '^ari:cloud:ecosystem::app/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' && break
            message 'Invalid App ID' 'The Forge App ID format is invalid.'
        done
    fi
fi

if whiptail --title 'Jira installation' --yesno \
    'Is this same Forge App already installed in this Jira site production environment?' 10 78; then
    INSTALL_MODE=upgrade
else
    INSTALL_MODE=new
fi

while :; do
    SPACE_KEY=$(input 'Jira Space' 'Space key: uppercase letters, digits or underscore.' 'ALGO')
    printf '%s\n' "$SPACE_KEY" | grep -Eq '^[A-Z][A-Z0-9_]{1,20}$' && break
    message 'Invalid key' 'Example valid keys: ALGO, NETOPS, FIREWALL_1.'
done
while :; do
    SPACE_NAME=$(input 'Jira Space' 'Visible Space name.' 'AlgoSec')
    [ -n "$SPACE_NAME" ] && break
    message 'Required value' 'Space name cannot be empty.'
done
while :; do
    WORK_TYPE=$(input 'Jira work type' 'Work type name for access requests.' 'Network Access')
    [ -n "$WORK_TYPE" ] && break
    message 'Required value' 'Work type name cannot be empty.'
done

SUMMARY="Jira: $JIRA_URL
Space: $SPACE_KEY — $SPACE_NAME
Work type: $WORK_TYPE
Forge user: $FORGE_EMAIL
Forge action: $APP_MODE / $INSTALL_MODE
Linux owner: $OPERATOR

The next stages load the verified Docker image, deploy Forge, prepare Jira and open the bus configuration wizard."
whiptail --title 'Confirm installation' --yesno "$SUMMARY" 18 82 || cancelled

message 'Stage 1 of 4' 'Loading the verified Docker image and installing preparation helpers.'
IMAGE_SHA256=$(awk 'NR == 1 {print $1; exit}' "$IMAGE_CHECKSUM")
sh "$HERE/install-docker.sh" --image-archive "$IMAGE_ARCHIVE" \
    --image-sha256 "$IMAGE_SHA256" --data-dir "$DATA" --prepare-only

WORK=$(mktemp -d /tmp/algosec-guided-setup.XXXXXX)
CREDENTIALS=$(mktemp -d /run/algosec-forge-credentials.XXXXXX)
cleanup() {
    rm -rf "$WORK" "$CREDENTIALS"
    FORGE_TOKEN=
}
trap cleanup 0 HUP INT TERM

python3 - "$FORGE_ARCHIVE" "$WORK" <<'PY'
import pathlib, sys, tarfile
source, destination = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
with tarfile.open(source, 'r:gz') as archive:
    seen = set()
    for item in archive:
        name = pathlib.PurePosixPath(item.name)
        if (name.is_absolute() or '..' in name.parts or not item.isfile()
                or item.name in seen or not name.parts or name.parts[0] != 'forge'):
            raise SystemExit('Unsafe Forge archive member: ' + item.name)
        seen.add(item.name)
        target = destination.joinpath(*name.parts)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with archive.extractfile(item) as incoming, target.open('xb') as output:
            output.write(incoming.read())
        target.chmod(0o600)
PY
mkdir "$WORK/scripts"
cp "$HERE/setup-forge.sh" "$WORK/scripts/setup-forge.sh"
chmod 0700 "$WORK/scripts/setup-forge.sh"
chown -R "$OP_UID:$OP_GID" "$WORK"
printf '%s' "$FORGE_EMAIL" > "$CREDENTIALS/email"
printf '%s' "$FORGE_TOKEN" > "$CREDENTIALS/token"
chown -R "$OP_UID:$OP_GID" "$CREDENTIALS"
chmod 0700 "$CREDENTIALS"
chmod 0600 "$CREDENTIALS/email" "$CREDENTIALS/token"

message 'Stage 2 of 4' \
    'Installing Node.js 22 and Forge CLI for the selected Linux user, then deploying the structured Jira field. Forge may ask you to select a Developer Space when registering a new app.'
set -- --site "$SITE" --environment production --install-mode "$INSTALL_MODE" \
    --app-mode "$APP_MODE" --credentials-dir "$CREDENTIALS" --install-node
[ -z "$APP_ID" ] || set -- "$@" --app-id "$APP_ID"
runuser -u "$OPERATOR" -- env HOME="$OP_HOME" USER="$OPERATOR" LOGNAME="$OPERATOR" \
    sh "$WORK/scripts/setup-forge.sh" "$@"
FORGE_TOKEN=
rm -rf "$CREDENTIALS"

message 'Stage 3 of 4' \
    'Jira preparation will now ask for the Jira administrator email and API token. The token is masked and is not saved by the preparation helper.'
/usr/local/sbin/prepare-jira.sh --base-url "$JIRA_URL" --space-key "$SPACE_KEY" \
    --space-name "$SPACE_NAME" --work-type-name "$WORK_TYPE" --apply

message 'Stage 4 of 4' \
    'The final wizard will ask for the runtime Jira token, FireFlow URL and password, TLS trust choice and synchronization confirmation.'
sh "$HERE/install-docker.sh" --image-archive "$IMAGE_ARCHIVE" \
    --image-sha256 "$IMAGE_SHA256" --data-dir "$DATA"

message 'Installation complete' \
    'Forge, Jira preparation and the Docker bus completed. Verify with: sudo docker logs --tail 100 algosec-jira-bus'

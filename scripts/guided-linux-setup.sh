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
        apt-get install -y ca-certificates curl git build-essential python3 whiptail xz-utils
        ;;
    rhel|rocky|almalinux)
        dnf -y install ca-certificates curl git gcc gcc-c++ make newt python3 xz
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
DEFAULT_USER=${SUDO_USER:-}
case "$DEFAULT_USER" in ''|root) DEFAULT_USER=;; esac

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

DISCOVERY=$(mktemp -d /tmp/algosec-forge-discovery.XXXXXX)
ADMIN_CREDENTIALS=$(mktemp -d /run/algosec-jira-admin.XXXXXX)
cleanup_discovery() {
    rm -rf "$DISCOVERY" "$ADMIN_CREDENTIALS"
    JIRA_ADMIN_TOKEN=
}
trap cleanup_discovery 0 HUP INT TERM
cat > "$DISCOVERY/discover.py" <<'PY'
import base64
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

base_url, output, credentials = sys.argv[1:]
with open(credentials + '/email', encoding='utf-8') as stream:
    email = stream.read()
with open(credentials + '/token', encoding='utf-8') as stream:
    token = stream.read()
if (not email or not token
        or any(ord(char) < 32 or 127 <= ord(char) <= 159
               for char in email + token)):
    raise SystemExit('Jira discovery credentials are invalid')
authorization = base64.b64encode((email + ':' + token).encode()).decode()
start_at = 0
candidates = {}
try:
    while True:
        query = urllib.parse.urlencode({
            'type': 'custom', 'startAt': start_at, 'maxResults': 100})
        request = urllib.request.Request(
            base_url + '/rest/api/3/field/search?' + query,
            headers={'Accept': 'application/json',
                     'Authorization': 'Basic ' + authorization})
        with urllib.request.urlopen(request, timeout=30) as response:
            document = json.load(response)
        values = document.get('values') if isinstance(document, dict) else None
        if not isinstance(values, list):
            raise ValueError('invalid field list')
        for field in values:
            schema = field.get('schema') if isinstance(field, dict) else None
            custom = schema.get('custom') if isinstance(schema, dict) else None
            if (field.get('name') != 'Мережеві доступи AlgoSec'
                    or not isinstance(custom, str)
                    or not custom.endswith('/static/algosec-network-access')):
                continue
            uuid = r'[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}'
            match = re.search(
                r'::extension/(' + uuid + r')(?:/' + uuid
                + r')?/static/algosec-network-access$', custom)
            field_id = field.get('id')
            if not match or not isinstance(field_id, str):
                continue
            configuration = schema.get('configuration')
            environment = (configuration.get('environment', 'UNKNOWN')
                           if isinstance(configuration, dict) else 'UNKNOWN')
            environment = str(environment).upper()
            app_id = match.group(1).lower()
            candidate = (environment, field_id)
            previous = candidates.get(app_id)
            if previous is None or (candidate[0] == 'PRODUCTION'
                                    and previous[0] != 'PRODUCTION'):
                candidates[app_id] = candidate
        if document.get('isLast') is True:
            break
        page_start = document.get('startAt')
        page_size = document.get('maxResults')
        if type(page_start) is not int or type(page_size) is not int or page_size < 1:
            raise ValueError('invalid pagination')
        start_at = page_start + page_size
except (OSError, ValueError, json.JSONDecodeError, urllib.error.HTTPError) as error:
    raise SystemExit('Could not read compatible Forge apps from Jira: '
                     + type(error).__name__) from None

with open(output, 'w', encoding='utf-8') as stream:
    for app_id, (environment, field_id) in sorted(candidates.items()):
        stream.write(f'{app_id}\t{environment}\t{field_id}\n')
PY
chmod 0700 "$DISCOVERY/discover.py"

while :; do
    JIRA_ADMIN_EMAIL=$(input 'Jira administrator' \
        'Administrator email used once to discover installed compatible Forge apps.')
    [ -n "$JIRA_ADMIN_EMAIL" ] && break
    message 'Required value' 'Jira administrator email cannot be empty.'
done
while :; do
    JIRA_ADMIN_TOKEN=$(password 'Jira administrator' \
        'Administrator API token used only for Forge app discovery. It is not saved.')
    [ -n "$JIRA_ADMIN_TOKEN" ] && break
    message 'Required value' 'Jira administrator API token cannot be empty.'
done
printf '%s' "$JIRA_ADMIN_EMAIL" > "$ADMIN_CREDENTIALS/email"
printf '%s' "$JIRA_ADMIN_TOKEN" > "$ADMIN_CREDENTIALS/token"
chmod 0600 "$ADMIN_CREDENTIALS/email" "$ADMIN_CREDENTIALS/token"
if ! python3 "$DISCOVERY/discover.py" "$JIRA_URL" "$DISCOVERY/apps.tsv" \
        "$ADMIN_CREDENTIALS"; then
    message 'Forge discovery failed' \
        'Could not read Jira fields. Verify the Jira administrator credentials and network access, then rerun the installer.'
    exit 1
fi
JIRA_ADMIN_TOKEN=
JIRA_ADMIN_EMAIL=

set -- whiptail --title 'Forge application' --menu \
    'Choose a compatible Forge app discovered in Jira, or register a new one.' \
    20 96 10
while IFS="$(printf '\t')" read -r CANDIDATE_ID CANDIDATE_ENV CANDIDATE_FIELD; do
    [ -n "$CANDIDATE_ID" ] || continue
    set -- "$@" "existing:$CANDIDATE_ID" \
        "AlgoSec field; $CANDIDATE_ENV; $CANDIDATE_FIELD"
done < "$DISCOVERY/apps.tsv"
set -- "$@" new 'Register and install a new Forge app'
APP_CHOICE=$("$@" 3>&1 1>&2 2>&3) || cancelled

APP_ID=
APP_MODE=register
INSTALL_MODE=new
SKIP_FORGE=0
FORGE_SELECTION='register new / install production'
case "$APP_CHOICE" in
    existing:*)
        APP_UUID=${APP_CHOICE#existing:}
        APP_ID=ari:cloud:ecosystem::app/$APP_UUID
        CANDIDATE_ENV=$(awk -F '\t' -v id="$APP_UUID" '$1 == id {print $2; exit}' \
            "$DISCOVERY/apps.tsv")
        APP_MODE=existing
        if [ "$CANDIDATE_ENV" = PRODUCTION ]; then
            FORGE_EXISTING_ACTION=$(whiptail --title 'Forge application' --menu \
                'The production app is already installed. Reuse it unchanged, or deploy the bundled Forge update to this same App ID.' \
                15 92 2 \
                reuse 'Reuse the installed production app unchanged' \
                upgrade 'Deploy the bundled Forge app update' \
                3>&1 1>&2 2>&3) || cancelled
            case "$FORGE_EXISTING_ACTION" in
                reuse)
                    SKIP_FORGE=1
                    FORGE_SELECTION="$APP_UUID / reuse verified production installation"
                    ;;
                upgrade)
                    APP_MODE=existing
                    INSTALL_MODE=upgrade
                    SKIP_FORGE=0
                    FORGE_SELECTION="$APP_UUID / deploy bundled update to existing production app"
                    ;;
                *) echo 'Invalid Forge application action.' >&2; exit 1;;
            esac
        else
            FORGE_SELECTION="$APP_UUID / deploy and install production"
        fi
        ;;
    new) ;;
    *) echo 'Invalid Forge application selection.' >&2; exit 1;;
esac
rm -rf "$DISCOVERY"

FORGE_EMAIL=
FORGE_TOKEN=
if [ "$SKIP_FORGE" = 0 ]; then
    while :; do
        OPERATOR=$(input 'Linux operator' \
            'Normal Linux user that will own Node.js and the private Forge working copy.' \
            "$DEFAULT_USER")
        if [ -n "$OPERATOR" ] && [ "$OPERATOR" != root ] \
                && getent passwd "$OPERATOR" >/dev/null; then
            break
        fi
        message 'Invalid user' 'Enter an existing non-root Linux user.'
    done
    OP_UID=$(id -u "$OPERATOR")
    OP_GID=$(id -g "$OPERATOR")
    OP_HOME=$(getent passwd "$OPERATOR" | awk -F: '{print $6}')
    [ -d "$OP_HOME" ] && [ ! -L "$OP_HOME" ] \
        || { echo 'Operator home directory is unsafe.' >&2; exit 1; }
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
else
    OPERATOR='not required (Forge skipped)'
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
Forge action: $FORGE_SELECTION
Linux owner: $OPERATOR

The next stages load the verified Docker image, prepare the selected Forge app when needed, prepare Jira and open the bus configuration wizard."
whiptail --title 'Confirm installation' --yesno "$SUMMARY" 18 82 || cancelled

message 'Stage 1 of 4' 'Loading the verified Docker image and installing preparation helpers.'
IMAGE_SHA256=$(awk 'NR == 1 {print $1; exit}' "$IMAGE_CHECKSUM")
sh "$HERE/install-docker.sh" --image-archive "$IMAGE_ARCHIVE" \
    --image-sha256 "$IMAGE_SHA256" --data-dir "$DATA" --prepare-only

WORK=
CREDENTIALS=
JIRA_PREPARATION_RESULT=
cleanup() {
    [ -z "$WORK" ] || rm -rf "$WORK"
    [ -z "$CREDENTIALS" ] || rm -rf "$CREDENTIALS"
    rm -rf "$ADMIN_CREDENTIALS" "$DISCOVERY"
    [ -z "$JIRA_PREPARATION_RESULT" ] || rm -f "$JIRA_PREPARATION_RESULT"
    FORGE_TOKEN=
    JIRA_ADMIN_TOKEN=
}
trap cleanup 0 HUP INT TERM

if [ "$SKIP_FORGE" = 1 ]; then
    message 'Stage 2 of 4' \
        'The selected Forge app already exposes the production field in Jira. Deployment and installation are skipped; Jira preparation will verify this exact App ID before making changes.'
else
    WORK=$(mktemp -d /tmp/algosec-guided-setup.XXXXXX)
    CREDENTIALS=$(mktemp -d /run/algosec-forge-credentials.XXXXXX)
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

    if [ "$APP_MODE" = register ]; then
        FORGE_WORKDIR=$(mktemp -d "$OP_HOME/algosec-jira-forge-new.XXXXXX")
        chown "$OP_UID:$OP_GID" "$FORGE_WORKDIR"
    else
        FORGE_WORKDIR=$OP_HOME/algosec-jira-forge-$APP_UUID
    fi
    message 'Stage 2 of 4' \
        'Installing Node.js 22 and Forge CLI for the selected Linux user, then deploying the structured Jira field. Forge may ask you to select a Developer Space when registering a new app.'
    set -- --site "$SITE" --environment production --install-mode "$INSTALL_MODE" \
        --app-mode "$APP_MODE" --credentials-dir "$CREDENTIALS" --install-node
    [ -z "$APP_ID" ] || set -- "$@" --app-id "$APP_ID"
    runuser -u "$OPERATOR" -- env HOME="$OP_HOME" USER="$OPERATOR" LOGNAME="$OPERATOR" \
        ALGOSEC_FORGE_WORKDIR="$FORGE_WORKDIR" \
        sh "$WORK/scripts/setup-forge.sh" "$@"
    FORGE_TOKEN=
    rm -rf "$CREDENTIALS"
    CREDENTIALS=
    if [ "$APP_MODE" = register ]; then
        APP_ID=$(sed -n 's/^[[:space:]]*id:[[:space:]]*//p' \
            "$FORGE_WORKDIR/manifest.yml" | head -1)
        printf '%s\n' "$APP_ID" | grep -Eq '^ari:cloud:ecosystem::app/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' \
            || { echo 'Forge setup did not return a valid App ID.' >&2; exit 1; }
    fi
fi

message 'Stage 3 of 4' \
    'Jira preparation will now reuse the temporary administrator credentials, verify the selected Forge App ID and prepare the Space, work type, workflow and fields.'
JIRA_PREPARATION_RESULT=$(mktemp /tmp/algosec-jira-preparation.XXXXXX)
/usr/local/sbin/prepare-jira.sh --credentials-dir "$ADMIN_CREDENTIALS" \
    --base-url "$JIRA_URL" --space-key "$SPACE_KEY" \
    --space-name "$SPACE_NAME" --work-type-name "$WORK_TYPE" \
    --forge-app-id "$APP_ID" --apply > "$JIRA_PREPARATION_RESULT"
cat "$JIRA_PREPARATION_RESULT"
LAYOUT_URL=$(python3 - "$JIRA_PREPARATION_RESULT" <<'PY'
import json, sys
with open(sys.argv[1], encoding='utf-8') as stream:
    result = json.load(stream)
print(result['issue_layout']['url'])
PY
)
rm -rf "$ADMIN_CREDENTIALS"

printf '%s\n' "Required Jira layout step: $LAYOUT_URL" >&2
printf '%s\n' \
    'Move Мережеві доступи AlgoSec to Description fields; keep FireFlow result fields in Context fields; then Save changes.' >&2
whiptail --title 'Jira work item layout' --yesno \
    "Jira Cloud does not provide a public API for this visual placement.\n\nOpen:\n$LAYOUT_URL\n\nMove Мережеві доступи AlgoSec to Description fields. Keep FireFlow Request ID, FireFlow Status and FireFlow Owner in Context fields. Select Save changes, then return here and choose Yes." \
    20 96 || {
        echo 'Complete the Jira work item layout before enabling synchronization.' >&2
        exit 1
    }

message 'Stage 4 of 4' \
    'The final wizard will ask for the runtime Jira token, FireFlow URL and password, TLS trust choice and synchronization confirmation.'
sh "$HERE/install-docker.sh" --image-archive "$IMAGE_ARCHIVE" \
    --image-sha256 "$IMAGE_SHA256" --data-dir "$DATA" \
    --forge-app-id "$APP_ID"

message 'Installation complete' \
    'Forge, Jira preparation and the Docker bus completed. Verify with: sudo docker logs --tail 100 algosec-jira-bus'

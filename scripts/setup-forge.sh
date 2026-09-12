#!/bin/sh
# Deploy the bundled Forge field as the Linux operator, never as root.
set -eu
umask 077

SOURCE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
DEST=${ALGOSEC_FORGE_WORKDIR:-"$HOME/algosec-jira-forge"}
SITE=${ALGOSEC_FORGE_SITE:-}
ENVIRONMENT=${ALGOSEC_FORGE_ENVIRONMENT:-production}
INSTALL_MODE=${ALGOSEC_FORGE_INSTALL_MODE:-}
APP_MODE=${ALGOSEC_FORGE_APP_MODE:-auto}
APP_ID=${ALGOSEC_FORGE_APP_ID:-}
CREDENTIALS_DIR=${ALGOSEC_FORGE_CREDENTIALS_DIR:-}
INSTALL_NODE=${ALGOSEC_FORGE_INSTALL_NODE:-0}
PLACEHOLDER=ari:cloud:ecosystem::app/00000000-0000-0000-0000-000000000000

usage() {
    cat <<'HELP'
Usage: sh scripts/setup-forge.sh [options]
  --site HOSTNAME                 Jira hostname without https://
  --environment NAME             production, staging or development
  --install-mode new|upgrade     Install once or upgrade an existing installation
  --app-mode auto|register|existing
  --app-id ARI                   Existing ari:cloud:ecosystem::app/UUID
  --credentials-dir DIRECTORY    Owner-only files named email and token
  --install-node                 Install Node 22 with nvm and install Forge CLI

Without options the script keeps the original interactive workflow. A registered
manifest in ~/algosec-jira-forge is preserved on every rerun.
HELP
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --site|--environment|--install-mode|--app-mode|--app-id|--credentials-dir)
            [ "$#" -ge 2 ] || { usage >&2; exit 2; }
            case "$1" in
                --site) SITE=$2;;
                --environment) ENVIRONMENT=$2;;
                --install-mode) INSTALL_MODE=$2;;
                --app-mode) APP_MODE=$2;;
                --app-id) APP_ID=$2;;
                --credentials-dir) CREDENTIALS_DIR=$2;;
            esac
            shift 2
            ;;
        --install-node) INSTALL_NODE=1; shift;;
        --help) usage; exit 0;;
        *) echo "Unknown Forge setup option: $1" >&2; usage >&2; exit 2;;
    esac
done

case "$(uname -s)" in Linux|Darwin) ;; *) echo 'Forge setup requires Linux or macOS.' >&2; exit 1;; esac
[ "$(id -u)" -ne 0 ] || {
    echo 'Run Forge setup as a normal Linux user with Atlassian developer access.' >&2
    exit 1
}
[ -d "$SOURCE/forge" ] || { echo 'Bundled Forge source is missing.' >&2; exit 1; }
case "$DEST" in "$HOME"/*) ;; *) echo 'Forge work directory must be inside the user home.' >&2; exit 1;; esac
case "$SITE" in *://*|*/*|*:*|*[!a-zA-Z0-9.-]*) echo 'Invalid Jira hostname.' >&2; exit 1;; esac
case "$ENVIRONMENT" in production|staging|development) ;; *) echo 'Invalid Forge environment.' >&2; exit 1;; esac
case "$INSTALL_MODE" in ''|new|upgrade) ;; *) echo 'Install mode must be new or upgrade.' >&2; exit 1;; esac
case "$APP_MODE" in auto|register|existing) ;; *) echo 'Invalid Forge app mode.' >&2; exit 1;; esac
if [ -n "$APP_ID" ]; then
    printf '%s\n' "$APP_ID" | grep -Eq '^ari:cloud:ecosystem::app/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' \
        || { echo 'Invalid existing Forge App ID.' >&2; exit 1; }
fi

node_supported() {
    command -v node >/dev/null 2>&1 || return 1
    MAJOR=$(node -p 'Number(process.versions.node.split(".")[0])' 2>/dev/null || true)
    [ "$MAJOR" = 22 ] || [ "$MAJOR" = 24 ]
}

if ! node_supported || ! command -v npm >/dev/null 2>&1; then
    [ "$INSTALL_NODE" = 1 ] || {
        echo 'Install Node.js 22 LTS first or rerun with --install-node.' >&2
        exit 1
    }
    command -v git >/dev/null 2>&1 || { echo 'git is required to install nvm.' >&2; exit 1; }
    NVM_DIR=${NVM_DIR:-"$HOME/.nvm"}
    export NVM_DIR
    if [ ! -d "$NVM_DIR" ]; then
        git clone --branch v0.40.7 --depth 1 https://github.com/nvm-sh/nvm.git "$NVM_DIR"
    fi
    [ -f "$NVM_DIR/nvm.sh" ] && [ ! -L "$NVM_DIR/nvm.sh" ] \
        || { echo 'Existing nvm directory is incomplete or unsafe.' >&2; exit 1; }
    # shellcheck disable=SC1090
    . "$NVM_DIR/nvm.sh"
    nvm install 22
    nvm alias default 22
    nvm use 22
    [ -f "$HOME/.profile" ] || : > "$HOME/.profile"
    if ! grep -Fq 'NVM_DIR="$HOME/.nvm"' "$HOME/.profile"; then
        cat >> "$HOME/.profile" <<'PROFILE'

export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
PROFILE
    fi
fi

node_supported || { echo 'Forge requires Node.js 22 or 24.' >&2; exit 1; }
command -v npm >/dev/null 2>&1 || { echo 'npm installation failed.' >&2; exit 1; }
unset NODE_ENV
if ! command -v forge >/dev/null 2>&1; then
    [ "$INSTALL_NODE" = 1 ] || { echo 'Install Forge CLI: npm install -g @forge/cli' >&2; exit 1; }
    npm install -g @forge/cli
fi
forge --version

if [ -n "$CREDENTIALS_DIR" ]; then
    [ -d "$CREDENTIALS_DIR" ] && [ ! -L "$CREDENTIALS_DIR" ] \
        || { echo 'Forge credentials directory is unsafe.' >&2; exit 1; }
    for SECRET_FILE in "$CREDENTIALS_DIR/email" "$CREDENTIALS_DIR/token"; do
        [ -f "$SECRET_FILE" ] && [ ! -L "$SECRET_FILE" ] \
            || { echo 'Forge credential file is missing or unsafe.' >&2; exit 1; }
        case "$(uname -s)" in
            Linux) OWNER=$(stat -c %u "$SECRET_FILE"); MODE=$(stat -c %a "$SECRET_FILE");;
            Darwin) OWNER=$(stat -f %u "$SECRET_FILE"); MODE=$(stat -f %Lp "$SECRET_FILE");;
        esac
        [ "$OWNER" = "$(id -u)" ] && [ "$MODE" = 600 ] \
            || { echo 'Forge credential files must be owner-only (0600).' >&2; exit 1; }
    done
    FORGE_EMAIL=$(cat "$CREDENTIALS_DIR/email")
    FORGE_API_TOKEN=$(cat "$CREDENTIALS_DIR/token")
    case "$FORGE_EMAIL$FORGE_API_TOKEN" in *'
'*|'') echo 'Forge credentials must be nonempty single-line values.' >&2; exit 1;; esac
    export FORGE_EMAIL FORGE_API_TOKEN
fi

# Refresh application code but keep the customer-specific registered App ID and marker.
[ ! -L "$DEST" ] || { echo 'Refusing a symbolic-link Forge work directory.' >&2; exit 1; }
if [ ! -d "$DEST" ]; then
    mkdir -m 700 "$DEST"
    cp -R "$SOURCE/forge/." "$DEST/"
else
    SAVED=$(mktemp -d "$HOME/.algosec-forge-save.XXXXXX")
    NEXT=$DEST.new.$$
    OLD=
    cleanup_sync() {
        if [ -n "$OLD" ] && [ -d "$OLD" ]; then
            if [ -d "$DEST" ]; then rm -rf "$OLD"; else mv "$OLD" "$DEST"; fi
        fi
        rm -rf "$SAVED" "$NEXT"
    }
    trap cleanup_sync 0 HUP INT TERM
    [ ! -L "$DEST/manifest.yml" ] || { echo 'Refusing a symbolic-link Forge manifest.' >&2; exit 1; }
    [ ! -f "$DEST/manifest.yml" ] || cp "$DEST/manifest.yml" "$SAVED/manifest.yml"
    [ ! -f "$DEST/.algosec-registered" ] || cp "$DEST/.algosec-registered" "$SAVED/registered"
    mkdir -m 700 "$NEXT"
    cp -R "$SOURCE/forge/." "$NEXT/"
    [ ! -f "$SAVED/manifest.yml" ] || cp "$SAVED/manifest.yml" "$NEXT/manifest.yml"
    [ ! -f "$SAVED/registered" ] || : > "$NEXT/.algosec-registered"
    OLD=$DEST.old.$$
    mv "$DEST" "$OLD"
    if mv "$NEXT" "$DEST"; then
        rm -rf "$OLD" "$SAVED"
        OLD=
        trap - 0 HUP INT TERM
    else
        mv "$OLD" "$DEST"
        exit 1
    fi
fi

cd "$DEST"
CURRENT_ID=$(sed -n 's/^[[:space:]]*id:[[:space:]]*//p' manifest.yml | head -1)
if [ -n "$APP_ID" ]; then
    if [ "$CURRENT_ID" != "$PLACEHOLDER" ] && [ "$CURRENT_ID" != "$APP_ID" ]; then
        echo 'The saved Forge work directory belongs to another App ID; refusing to replace it.' >&2
        exit 1
    fi
    if [ "$CURRENT_ID" = "$PLACEHOLDER" ]; then
        sed "s#$PLACEHOLDER#$APP_ID#" manifest.yml > manifest.yml.new
        mv manifest.yml.new manifest.yml
        CURRENT_ID=$APP_ID
        : > .algosec-registered
    fi
fi

npm ci
printf '%s\n' 'Use the Atlassian developer/admin account; the bus runtime account is separate.'
if [ -z "${FORGE_EMAIL:-}" ] || [ -z "${FORGE_API_TOKEN:-}" ]; then
    forge login
fi

case "$APP_MODE" in
    existing)
        [ "$CURRENT_ID" != "$PLACEHOLDER" ] \
            || { echo 'Existing Forge App ID is required.' >&2; exit 1; }
        ;;
    register)
        if [ "$CURRENT_ID" = "$PLACEHOLDER" ]; then
            forge register
            : > .algosec-registered
            CURRENT_ID=$(sed -n 's/^[[:space:]]*id:[[:space:]]*//p' manifest.yml | head -1)
            [ "$CURRENT_ID" != "$PLACEHOLDER" ] \
                || { echo 'Forge registration did not update the App ID.' >&2; exit 1; }
        else
            printf '%s\n' 'A registered App ID already exists; reusing it to avoid a duplicate app.'
        fi
        ;;
    auto)
        if [ "$CURRENT_ID" = "$PLACEHOLDER" ]; then
            forge register
            : > .algosec-registered
            CURRENT_ID=$(sed -n 's/^[[:space:]]*id:[[:space:]]*//p' manifest.yml | head -1)
            [ "$CURRENT_ID" != "$PLACEHOLDER" ] \
                || { echo 'Forge registration did not update the App ID.' >&2; exit 1; }
        fi
        ;;
esac

if [ -z "$SITE" ]; then
    printf 'Jira site hostname (example: your-tenant.atlassian.net): '
    read -r SITE
    case "$SITE" in *://*|*/*|*:*|*[!a-zA-Z0-9.-]*|'') echo 'Invalid hostname.' >&2; exit 1;; esac
fi
if [ -z "$INSTALL_MODE" ]; then
    printf 'Existing installation on this site/environment? [y/N]: '
    read -r UPGRADE
    case "$UPGRADE" in y|Y) INSTALL_MODE=upgrade;; *) INSTALL_MODE=new;; esac
fi

forge deploy --environment "$ENVIRONMENT"
if [ "$INSTALL_MODE" = upgrade ]; then
    forge install --upgrade --site "$SITE" --product jira --environment "$ENVIRONMENT"
else
    forge install --site "$SITE" --product jira --environment "$ENVIRONMENT"
fi
unset FORGE_API_TOKEN FORGE_EMAIL
printf '%s\n' 'Forge field deployed. Continue with Jira preparation and the bus configuration wizard.'

#!/bin/sh
# Run as the Atlassian administrator, not the Linux root account.
set -eu
SOURCE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
[ "$(id -u)" -ne 0 ] || { echo 'Run this step as a normal user with Atlassian developer access.' >&2; exit 1; }
command -v node >/dev/null || { echo 'Install Node.js 22 LTS and npm first.' >&2; exit 1; }
command -v forge >/dev/null || { echo 'Install Forge CLI: npm install -g @forge/cli' >&2; exit 1; }
DEST=${ALGOSEC_FORGE_WORKDIR:-"$HOME/algosec-jira-forge"}
if [ ! -d "$DEST" ]; then
    mkdir -m 700 "$DEST"
    cp -R "$SOURCE/forge/." "$DEST/"
fi
cd "$DEST"
npm ci
printf '%s\n' 'Use your developer/admin account for Forge; the bus service account is separate.'
forge login
if ! [ -f .algosec-registered ]; then
    # Registration explicitly prompts for Developer Space/terms, updates this copy only.
    forge register
    touch .algosec-registered
fi
printf 'Jira site hostname (example: your-tenant.atlassian.net): '
read -r SITE
case "$SITE" in *[!a-zA-Z0-9.-]*|'') echo 'Invalid hostname' >&2; exit 1;; esac
printf 'Environment [production]: '
read -r ENVIRONMENT
ENVIRONMENT=${ENVIRONMENT:-production}
case "$ENVIRONMENT" in production|staging|development) ;; *) echo 'Invalid environment' >&2; exit 1;; esac
forge deploy --environment "$ENVIRONMENT"
printf 'Existing installation on this site/environment? [y/N]: '
read -r UPGRADE
if [ "$UPGRADE" = y ]; then
    forge install --upgrade --site "$SITE" --product jira --environment "$ENVIRONMENT"
else
    forge install --site "$SITE" --product jira --environment "$ENVIRONMENT"
fi
printf '%s\n' 'Now add the AlgoSec field to Network Access, mark Required, configure workflow using the guide, then rerun the Linux setup wizard.'

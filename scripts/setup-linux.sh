#!/bin/sh
# Run only from the verified bundle; do not start polling before wizard validation.
set -eu
umask 077
SOURCE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
[ "$(uname -s)" = Linux ] || { echo 'Installation requires Linux; use --extract to inspect on other systems.' >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || { echo 'Run installer with sudo.' >&2; exit 1; }
[ -f "$SOURCE/scripts/setup_wizard.py" ] || { echo 'Incomplete installer: missing wizard.' >&2; exit 1; }
CONNECTOR_WHEEL=${ALGOSEC_CONNECTOR_SOURCE:-}
case "$CONNECTOR_WHEEL" in
    /*/algosec_host_mcp-*-py3-none-any.whl) ;;
    *) echo 'Set --connector-wheel to an authorized universal algosec_host_mcp wheel.' >&2; exit 1;;
esac
[ -f "$CONNECTOR_WHEEL" ] && [ ! -L "$CONNECTOR_WHEEL" ] || { echo 'Connector wheel must be a regular file, not a symlink.' >&2; exit 1; }
command -v systemctl >/dev/null 2>&1 || { echo 'systemd is required.' >&2; exit 1; }
export ALGOSEC_SETUP_WIZARD=1
# Validate the manager, its version and Python before interrupting an existing bus.
sh "$SOURCE/scripts/install.sh" --preflight
# Stop scheduled writers during reconfiguration. A failed setup deliberately leaves them stopped.
for unit in algosec-jira-bus.timer algosec-jira-bus-reconcile.timer; do
    if systemctl cat "$unit" >/dev/null 2>&1; then
        systemctl disable --now "$unit"
    fi
done
for unit in algosec-jira-bus.service algosec-jira-bus-reconcile.service; do
    if systemctl cat "$unit" >/dev/null 2>&1; then
        systemctl stop "$unit"
    fi
done
sh "$SOURCE/scripts/install.sh"
# Keep the verified source for resumable setup and Forge deployment after temporary extraction disappears.
install -d -m 0700 /opt/algosec-jira-bus/setup-source
PERSIST=/opt/algosec-jira-bus/setup-source
# Keep an already registered customer app; never overwrite it with the template id.
if [ -f "$PERSIST/forge/manifest.yml" ]; then
    cp "$PERSIST/forge/manifest.yml" "$PERSIST/.forge-manifest.saved"
    trap 'mv "$PERSIST/.forge-manifest.saved" "$PERSIST/forge/manifest.yml"' 0
fi
cp -R "$SOURCE/." "$PERSIST/"
if [ -f "$PERSIST/.forge-manifest.saved" ]; then
    mv "$PERSIST/.forge-manifest.saved" "$PERSIST/forge/manifest.yml"
    trap - 0
fi
exec /opt/algosec-jira-bus/venv/bin/python /opt/algosec-jira-bus/setup-source/scripts/setup_wizard.py --source /opt/algosec-jira-bus/setup-source "$@"

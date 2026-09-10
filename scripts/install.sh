#!/bin/sh
# Install the Jira <-> FireFlow bus as a systemd service.
#
# Deliberately /bin/sh and POSIX: this has to run on Rocky 8 and on CentOS 7 without
# assuming which shell, python or package manager is present.
#
# It is idempotent. Run it again after an upgrade and it replaces the code and the units
# and leaves your configuration and your secrets exactly as they were. It NEVER overwrites
# /etc/algosec-jira-bus/bus.json or secrets.env, and it never enables writing: a fresh
# install polls in dry run until a person sets "apply": true in the configuration.
#
# The mode lives in the configuration and not in the unit precisely because this script
# replaces the unit. A mode kept in ExecStart would survive until the first upgrade and
# then quietly revert, and a bus back in dry run looks just like a bus with nothing to do.
#
#   ./scripts/install.sh                 install/upgrade from verified bundled source
#   ./scripts/install.sh --uninstall     remove everything except config, secrets and state
#   ./scripts/install.sh --purge         remove those too
#
# After installing:
#   1. edit /etc/algosec-jira-bus/bus.json          (algosec-jira-bus fields helps)
#   2. put the two secrets in /etc/algosec-jira-bus/secrets.env
#   3. algosec-jira-bus --config ... doctor         until it reports no failures
#   4. systemctl start algosec-jira-bus.service     one dry-run pass; read the output
#   5. only then set "apply": true in bus.json -- NOT in the unit, which an upgrade replaces
set -eu
umask 077

NAME=algosec-jira-bus
PREFIX=/opt/$NAME
CONFIG_DIR=/etc/$NAME
STATE_DIR=/var/lib/$NAME
UNIT_DIR=/etc/systemd/system
SOURCE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root: this creates a system account and writes to $UNIT_DIR"
[ -f "$SOURCE/pyproject.toml" ] || die "run this from a checkout of the bus repository"
# --- which systemd is this, and therefore which units ------------------------------------
# Not cosmetic. On systemd 219 the modern unit is broken rather than merely weaker:
# StateDirectory is unknown so the state directory is never created, and ProtectSystem=strict
# fails to parse and is silently ignored, so the file promises protection it never applies.
systemd_version() { systemctl --version 2>/dev/null | awk 'NR==1{print $2; exit}'; }
SYSTEMD=$(systemd_version || true)
# A systemctl binary alone (for example inside a regular Docker container) is not a
# running service manager. Refuse before creating users, directories or changing units.
systemctl show --property=Version --value >/dev/null 2>&1 \
    || die "systemd must be running and reachable; a systemctl executable alone is insufficient"
case "$SYSTEMD" in
    ''|*[!0-9]*) die "could not read the systemd version; refusing to guess which units to use" ;;
esac
if [ "$SYSTEMD" -ge 235 ]; then
    VARIANT=""      # packaging/systemd/<name>.service
    say "systemd $SYSTEMD: using the modern units"
else
    VARIANT=".centos7"
    say "systemd $SYSTEMD: using the CentOS 7 units (StateDirectory and ProtectSystem=strict"
    say "  are not available on this release; see the header of the unit for what that costs)"
fi

# --- uninstall ---------------------------------------------------------------------------
if [ "${1:-}" = "--uninstall" ] || [ "${1:-}" = "--purge" ]; then
    for unit in $NAME.timer $NAME-reconcile.timer; do
        systemctl disable --now "$unit" 2>/dev/null || true
    done
    rm -f "$UNIT_DIR/$NAME.service" "$UNIT_DIR/$NAME.timer" \
          "$UNIT_DIR/$NAME-reconcile.service" "$UNIT_DIR/$NAME-reconcile.timer"
    systemctl daemon-reload
    rm -rf "$PREFIX"
    if [ "${1:-}" = "--purge" ]; then
        # Config, secrets and state are the only things here a person cannot regenerate,
        # so removing them is a separate, explicit decision.
        rm -rf "$CONFIG_DIR" "$STATE_DIR"
        userdel "$NAME" 2>/dev/null || true
        groupdel "$NAME" 2>/dev/null || true
        say "removed, including configuration, secrets and state"
    else
        say "removed. Configuration, secrets and state kept in $CONFIG_DIR and $STATE_DIR"
        say "  (use --purge to remove those too)"
    fi
    exit 0
fi

# --- an interpreter new enough for the package -------------------------------------------
# The package needs 3.11. Rocky 8 ships one; CentOS 7's system python is 3.7, and there is
# no compiler on the appliances, so there the answer is a standalone build under /opt --
# which this script will not fetch for you, because downloading and trusting a runtime is a
# decision an operator should make deliberately.
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3 "$PREFIX/python/bin/python3"; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info>=(3,11) else 1)' 2>/dev/null; then
            PYTHON=$(command -v "$candidate"); break
        fi
    fi
done
[ -n "$PYTHON" ] || die "no python 3.11 or newer found.
  Rocky 8:   dnf install -y python3.11
  CentOS 7:  install a standalone build (python-build-standalone) under $PREFIX/python
             and run this again; there is no compiler on these appliances."
say "interpreter: $PYTHON ($("$PYTHON" -c 'import sys;print(".".join(map(str,sys.version_info[:3])))'))"

check_preserved_config() {
    [ -f "$CONFIG_DIR/bus.json" ] || return 0
    ACCOUNT_UID=$(id -u "$NAME" 2>/dev/null) \
        || die "existing configuration found but service account $NAME is missing"
    PYTHONPATH="$SOURCE" "$PYTHON" - "$CONFIG_DIR/bus.json" "$ACCOUNT_UID" <<'PY'
import sys
from algosec_jira_bus.config import private_json
from scripts.setup_wizard import validate_secret_references

try:
    validate_secret_references(private_json(sys.argv[1], int(sys.argv[2])))
except (OSError, ValueError) as error:
    print('Existing configuration is not upgradeable: ' + str(error), file=sys.stderr)
    raise SystemExit(1)
PY
}

# Used by the bundled bootstrap before stopping any existing polling services.
if [ "${1:-}" = "--preflight" ]; then
    "$PYTHON" -c 'import venv' 2>/dev/null \
        || die "Python venv support unavailable; install the matching Python venv package"
    check_preserved_config \
        || die "convert legacy secret references before upgrading; running services were not stopped"
    say "preflight passed; no system changes made"
    exit 0
fi

# --- account and directories --------------------------------------------------------------
if ! getent group "$NAME" >/dev/null 2>&1; then groupadd --system "$NAME"; fi
if ! getent passwd "$NAME" >/dev/null 2>&1; then
    useradd --system --gid "$NAME" --home-dir "$PREFIX" --shell /sbin/nologin \
            --comment "AlgoSec FireFlow to Jira bus" "$NAME"
fi
install -d -m 0755 "$PREFIX"
install -d -m 0700 -o "$NAME" -g "$NAME" "$CONFIG_DIR"
install -d -m 0700 -o "$NAME" -g "$NAME" "$STATE_DIR"

# Repeat the guard immediately before replacing application files, in case the
# configuration changed after preflight.
check_preserved_config \
    || die "convert legacy secret references before upgrading; installed application files were not changed"

# --- the code -----------------------------------------------------------------------------
if [ ! -x "$PREFIX/venv/bin/python" ]; then
    "$PYTHON" -m venv --without-pip "$PREFIX/venv"
fi
# Runtime is standard-library-only. Copy the verified bundle source and create the command
# locally, so installation never downloads or executes package-index content as root.
rm -rf "$PREFIX/app.new"
install -d -m 0755 "$PREFIX/app.new"
cp -R "$SOURCE/algosec_jira_bus" "$PREFIX/app.new/"
chmod -R a+rX "$PREFIX/app.new"
rm -rf "$PREFIX/app.old"
if [ -d "$PREFIX/app" ]; then mv "$PREFIX/app" "$PREFIX/app.old"; fi
mv "$PREFIX/app.new" "$PREFIX/app"
rm -rf "$PREFIX/app.old"
cat > "$PREFIX/venv/bin/$NAME" <<'SH'
#!/bin/sh
PYTHONPATH=/opt/algosec-jira-bus/app exec /opt/algosec-jira-bus/venv/bin/python -m algosec_jira_bus.bus "$@"
SH
chmod 0755 "$PREFIX/venv/bin/$NAME"
# The docs the units point at, so Documentation= is not a dangling path.
install -d -m 0755 "$PREFIX/docs"
for doc in "$SOURCE"/docs/*.md; do install -m 0644 "$doc" "$PREFIX/docs/"; done

# --- configuration, only if there is none yet ----------------------------------------------
if [ ! -f "$CONFIG_DIR/bus.json" ]; then
    install -m 0600 -o "$NAME" -g "$NAME" "$SOURCE/examples/jira-sync.json" "$CONFIG_DIR/bus.json"
    say "wrote a starting configuration to $CONFIG_DIR/bus.json -- every id in it is a"
    say "  placeholder and it will not work until you replace them"
else
    say "keeping the existing $CONFIG_DIR/bus.json"
fi
if [ ! -f "$CONFIG_DIR/secrets.env" ]; then
    umask 077
    cat > "$CONFIG_DIR/secrets.env" <<'ENV'
# Values for the env: references in bus.json. This file is read by systemd, not by a shell:
# write NAME=value with no quotes and no export.
#
# The bus resolves these env: references at runtime. Keep this file private and readable
# only by the dedicated service account.
#JIRA_API_TOKEN=
#ASMS_API_PASSWORD=
ENV
    chown "$NAME:$NAME" "$CONFIG_DIR/secrets.env"
    chmod 0600 "$CONFIG_DIR/secrets.env"
    say "wrote an empty $CONFIG_DIR/secrets.env (0600)"
else
    say "keeping the existing $CONFIG_DIR/secrets.env"
fi

# --- units ----------------------------------------------------------------------------------
for unit in "$NAME" "$NAME-reconcile"; do
    install -m 0644 "$SOURCE/packaging/systemd/${unit}${VARIANT}.service" "$UNIT_DIR/${unit}.service"
    install -m 0644 "$SOURCE/packaging/systemd/${unit}.timer" "$UNIT_DIR/${unit}.timer"
done
systemctl daemon-reload
# Enabled, not started: the timers will fire, and every pass is a dry run until somebody
# sets "apply": true in bus.json on purpose.
if [ "${ALGOSEC_SETUP_WIZARD:-0}" != 1 ]; then
    systemctl enable "$NAME.timer" "$NAME-reconcile.timer" >/dev/null
fi

say ""
say "installed. The bus is in DRY RUN: it will report what it would do and change nothing."
say ""
say "  1. edit    $CONFIG_DIR/bus.json"
say "             $PREFIX/venv/bin/$NAME --config $CONFIG_DIR/bus.json fields"
say "             lists your real Jira field ids"
say "  2. secrets $CONFIG_DIR/secrets.env"
say "  3. check   $PREFIX/venv/bin/$NAME --config $CONFIG_DIR/bus.json doctor"
say "             run it until it reports no failures"
say "  4. try     systemctl start $NAME.service && journalctl -u $NAME.service -n 50"
say "  5. only then set \"apply\": true in $CONFIG_DIR/bus.json"
say "             (in the configuration, not the unit -- an upgrade replaces the unit)"
